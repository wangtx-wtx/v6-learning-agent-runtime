"""
文件 Blob 生命周期（V5.5 方案 4.1）。

- 相同 sha256 的物理文件只存一份（file_blobs，UNIQUE）；
- materials 是业务引用记录：去重上传 = 新建 material + 复用 blob，绝不修改旧材料归属；
- ref_count 记录引用数；material 删除时递减，归零进入 pending_delete，由 GC 物理删除。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from ..database import execute, fetch_one, insert, transaction

logger = logging.getLogger(__name__)


def find_blob_by_sha(sha256: str):
    return fetch_one("SELECT * FROM file_blobs WHERE sha256=?", (sha256,))


def acquire_blob(sha256: str, tmp_path: str | Path, suffix: str, mime: str | None,
                 size_bytes: int, storage_dir: Path) -> dict:
    """
    上传的统一 blob 获取入口（方案 4.1），处理全部状态：
    - live blob 存在 → 复用（ref+1），删除临时文件，不重复落盘；
    - pending_delete 且物理文件仍在 → 复活（gc_state='live'，ref=1）；
    - deleted / 文件已丢失 → 用本次临时文件重新落位，更新该行（同 sha256 即同内容实体）；
    - 不存在 → 原子落位新行（并发唯一冲突时转为更新复用）。
    返回 {blob_id, storage_path, reused}。
    """
    storage_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tmp_path)
    row = find_blob_by_sha(sha256)

    if row and row["gc_state"] == "live" and Path(row["storage_path"]).exists():
        tmp.unlink(missing_ok=True)
        add_blob_ref(int(row["id"]))
        return {"blob_id": int(row["id"]), "storage_path": row["storage_path"], "reused": True}

    # 需要落位物理文件（新 blob / 复活死 blob / 文件丢失重写）
    target = storage_dir / f"{uuid.uuid4().hex}{suffix}"
    tmp.replace(target)

    try:
        if row is None:
            blob_id = insert(
                "INSERT INTO file_blobs (sha256, storage_path, size_bytes, mime, ref_count) "
                "VALUES (?,?,?,?,'1')",
                (sha256, str(target), size_bytes, mime),
            )
            return {"blob_id": int(blob_id), "storage_path": str(target), "reused": False}
        # 复活/重写既有行
        execute(
            "UPDATE file_blobs SET storage_path=?, size_bytes=?, mime=?, ref_count=1, "
            " gc_state='live' WHERE id=?",
            (str(target), size_bytes, mime, row["id"]),
        )
        return {"blob_id": int(row["id"]), "storage_path": str(target), "reused": True}
    except Exception:
        # 并发上传同内容：唯一索引冲突 → 复用对方已写入的行
        Path(target).unlink(missing_ok=True)
        fresh = find_blob_by_sha(sha256)
        if not fresh:
            raise
        if not Path(fresh["storage_path"]).exists():
            raise
        tmp.unlink(missing_ok=True)
        return {"blob_id": int(fresh["id"]), "storage_path": fresh["storage_path"], "reused": True}


def add_blob_ref(blob_id: int) -> None:
    from ..database import execute
    execute("UPDATE file_blobs SET ref_count=ref_count+1 WHERE id=?", (blob_id,))


def release_material_blob(material_id: int) -> dict:
    """删除 material 前调用：读取其 blob 引用并释放（ref-1；归零 → pending_delete）。"""
    row = fetch_one("SELECT blob_id FROM materials WHERE id=?", (material_id,))
    if not row or not row["blob_id"]:
        return {"blob_id": None, "ref_count": 0, "pending_delete": False}
    return release_blob_ref(int(row["blob_id"]))


def release_blob_ref(blob_id: int) -> dict:
    """释放一个 blob 引用：ref_count-1；归零且 live → gc_state='pending_delete'。"""
    with transaction() as conn:
        conn.execute("UPDATE file_blobs SET ref_count=ref_count-1 WHERE id=?", (blob_id,))
        b = conn.execute("SELECT ref_count, gc_state FROM file_blobs WHERE id=?", (blob_id,)).fetchone()
        pending = False
        if b and (b["ref_count"] or 0) <= 0 and b["gc_state"] == "live":
            conn.execute(
                "UPDATE file_blobs SET gc_state='pending_delete' WHERE id=?", (blob_id,)
            )
            pending = True
    return {"blob_id": blob_id,
            "ref_count": int(b["ref_count"]) if b else 0,
            "pending_delete": pending}
