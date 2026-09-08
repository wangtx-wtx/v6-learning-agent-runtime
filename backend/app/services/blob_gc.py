"""
物理存储 GC（V5.5 方案 4.2）。

删除顺序（方案要求）：evidence_links → FTS 行 → source_chunks → parse_tasks →
materials → blob 引用减一 →（归零）unlink 物理文件。
任何一步失败落 blob_gc_tasks 供重试，绝不静默丢数据。
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..database import execute, fetch_all, fetch_one, insert, transaction

logger = logging.getLogger(__name__)


def delete_material_record(material_id: int) -> dict:
    """删除单个材料记录：按依赖顺序清理，返回 GC 摘要。"""
    mat = fetch_one("SELECT id, blob_id, file_path FROM materials WHERE id=?", (material_id,))
    if not mat:
        raise LookupError(f"material {material_id} 不存在")
    blob_id = mat["blob_id"]  # 先捕获：材料行删除后无法再读

    chunk_ids = [r["id"] for r in fetch_all(
        "SELECT id FROM source_chunks WHERE material_id=?", (material_id,))]

    with transaction() as conn:
        if chunk_ids:
            q = ",".join("?" * len(chunk_ids))
            conn.execute(f"DELETE FROM evidence_links WHERE chunk_id IN ({q})", chunk_ids)
        # FTS 行（contentless 虚拟表无外键，需手动清理）
        from ..rag import fts_delete_chunk_ids
        fts_delete_chunk_ids(None, chunk_ids)
        conn.execute("DELETE FROM source_chunks WHERE material_id=?", (material_id,))
        conn.execute("DELETE FROM parse_tasks WHERE material_id=?", (material_id,))
        conn.execute("DELETE FROM materials WHERE id=?", (material_id,))

    if blob_id:
        from .blob import release_blob_ref
        ref = release_blob_ref(int(blob_id))
    else:
        ref = {"blob_id": None, "ref_count": 0, "pending_delete": False}
    return {"material_id": material_id, "chunks_removed": len(chunk_ids), **ref}


def release_material_blob(material_id: int) -> dict:
    from .blob import release_material_blob as _rel
    return _rel(material_id)


def gc_blob_now(blob_id: int) -> dict:
    """
    尝试物理删除一个 pending_delete blob：ref 复查 → unlink → gc_state='deleted'。
    失败 → 记入 blob_gc_tasks（reason='gc_failed'），等待重试。
    """
    b = fetch_one("SELECT * FROM file_blobs WHERE id=?", (blob_id,))
    if not b:
        return {"blob_id": blob_id, "result": "missing"}
    if b["gc_state"] == "deleted":
        return {"blob_id": blob_id, "result": "already_deleted"}
    if (b["ref_count"] or 0) > 0:
        return {"blob_id": blob_id, "result": "still_referenced"}

    path = Path(b["storage_path"])
    try:
        if path.exists():
            path.unlink()
        execute("UPDATE file_blobs SET gc_state='deleted' WHERE id=?", (blob_id,))
        return {"blob_id": blob_id, "result": "deleted"}
    except Exception as e:
        insert(
            "INSERT INTO blob_gc_tasks (blob_id, storage_path, reason, error) VALUES (?,?,?,?)",
            (blob_id, str(path), "gc_failed", str(e)[:500]),
        )
        logger.warning("blob %s 物理删除失败: %s", blob_id, e)
        return {"blob_id": blob_id, "result": "failed", "error": str(e)[:200]}


def run_pending_gc() -> dict:
    """执行一轮 GC：所有 pending_delete blob + blob_gc_tasks 里的失败重试。"""
    deleted = failed = retried = 0
    pend = fetch_all(
        "SELECT id FROM file_blobs WHERE gc_state='pending_delete' AND ref_count<=0"
    )
    for r in pend:
        res = gc_blob_now(r["id"])
        if res["result"] == "deleted":
            deleted += 1
        elif res["result"] == "failed":
            failed += 1

    tasks = fetch_all(
        "SELECT id, blob_id FROM blob_gc_tasks WHERE finished_at IS NULL"
    )
    for t in tasks:
        res = gc_blob_now(t["blob_id"])
        if res["result"] == "deleted":
            execute("UPDATE blob_gc_tasks SET finished_at=datetime('now','localtime'), "
                    "error=NULL WHERE id=?", (t["id"],))
            retried += 1
        else:
            execute("UPDATE blob_gc_tasks SET error=? WHERE id=?",
                    (f"retry: {res.get('error', res['result'])}", t["id"]))
            failed += 1
    return {"pending_scanned": len(pend), "tasks_scanned": len(tasks),
            "deleted": deleted, "retried_ok": retried, "failed": failed}


def storage_audit() -> dict:
    """存储审计（方案 4.3 GET /api/admin/storage/audit）。"""
    from ..config import UPLOAD_DIR
    blobs = fetch_all("SELECT id, sha256, storage_path, size_bytes, ref_count, gc_state "
                      "FROM file_blobs")
    referenced_paths = set()
    missing_files, orphan_db_rows = [], []
    live = pending = deleted = 0
    total_bytes = 0
    zero_ref_live = []
    for b in blobs:
        st = b["gc_state"]
        if st == "live":
            live += 1
            total_bytes += b["size_bytes"] or 0
            if (b["ref_count"] or 0) <= 0:
                zero_ref_live.append(b["id"])
        elif st == "pending_delete":
            pending += 1
        else:
            deleted += 1
        p = Path(b["storage_path"]) if b["storage_path"] else None
        if p and p.exists():
            referenced_paths.add(str(p.resolve()))
        else:
            missing_files.append({"blob_id": b["id"], "path": b["storage_path"]})

    # 磁盘上有但 DB 无登记的孤儿文件
    if UPLOAD_DIR.exists():
        for f in UPLOAD_DIR.iterdir():
            if f.is_file() and str(f.resolve()) not in referenced_paths:
                if f.name.startswith("up_"):
                    continue  # 上传临时文件，单独统计
                orphan_db_rows.append(f.name)

    n_materials = fetch_one("SELECT COUNT(*) AS c FROM materials")["c"]
    n_orphan_materials = fetch_one(
        "SELECT COUNT(*) AS c FROM materials WHERE blob_id IS NULL")["c"]
    tmp_files = [f.name for f in UPLOAD_DIR.glob("up_*")] if UPLOAD_DIR.exists() else []
    gc_tasks_pending = fetch_one(
        "SELECT COUNT(*) AS c FROM blob_gc_tasks WHERE finished_at IS NULL")["c"]

    return {
        "blobs": {"live": live, "pending_delete": pending, "deleted": deleted,
                  "total_bytes_live": total_bytes},
        "materials": {"total": n_materials, "without_blob": n_orphan_materials},
        "anomalies": {
            "blobs_missing_physical_file": missing_files,
            "files_without_blob_record": orphan_db_rows,
            "live_blobs_with_zero_ref": zero_ref_live,
            "upload_tmp_files": tmp_files,
            "gc_tasks_pending": gc_tasks_pending,
        },
    }


def cleanup_tmp_files(max_age_hours: float = 24.0) -> int:
    """清理 UPLOAD_DIR 中超龄的 up_* 临时文件（上传中断/进程崩溃残留）。

    无论 max_age 多小，5 分钟内修改过的文件一律保护（可能有正在进行的上传）。
    """
    import time
    from ..config import UPLOAD_DIR
    if not UPLOAD_DIR.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    cutoff = min(cutoff, time.time() - 300)  # 保护活跃上传
    removed = 0
    for f in UPLOAD_DIR.glob("up_*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except Exception as e:
            logger.warning("清理临时文件失败 %s: %s", f, e)
    return removed
