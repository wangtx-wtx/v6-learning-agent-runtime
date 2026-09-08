"""
V5.4 材料解析管线（材料状态机）：
uploaded → queued → parsing → indexed → ready / failed

把材料文件解析成带稳定 locator 的 chunks（PDF 按页、PPT 按页、Markdown/文本按段、
图片/音频按占位说明），写入 source_chunks，并登记 FTS5 检索索引。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from .database import execute, fetch_all, fetch_one, insert

logger = logging.getLogger(__name__)


# ------------------------------- 文本切分 -------------------------------------
def split_segments(text: str, max_len: int = 900) -> list[str]:
    segs = []
    paragraphs = re.split(r"\n\s*\n", (text or "").strip())
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_len:
            segs.append(para)
            continue
        parts = re.split(r"(?<=[。！？.!?；;])", para)
        buf = ""
        for part in parts:
            if len(buf) + len(part) > max_len and buf:
                segs.append(buf.strip())
                buf = ""
            buf += part
        if buf.strip():
            segs.append(buf.strip())
    return segs


# ------------------------------- 文件解析 -------------------------------------
def _parse_pdf(path: str) -> list[tuple]:
    from pypdf import PdfReader
    reader = PdfReader(path)
    out = []
    for i, page in enumerate(reader.pages, 1):
        txt = page.extract_text() or ""
        if txt.strip():
            out.append((f"page:{i}", txt))
    return out


def _parse_pptx(path: str) -> list[tuple]:
    from pptx import Presentation
    prs = Presentation(path)
    out = []
    for i, slide in enumerate(prs.slides, 1):
        lines = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text:
                lines.append(shape.text)
        if lines:
            out.append((f"slide:{i}", "\n".join(lines)))
    return out


def _parse_text(path: str) -> list[tuple]:
    content = Path(path).read_text(encoding="utf-8", errors="ignore")
    return [("body", content)]


def _parse_docx(path: str) -> list[tuple]:
    try:
        import docx
        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        return [("docx", "\n".join(parts))]
    except Exception as e:
        return [("docx", f"[docx 解析失败: {e}]")]


# 图片/音频不在此解析：按类型矩阵进入 needs_ocr / transcribing（见 run_parse_material）


# ------------------------------- 取消支持 -------------------------------------
class ParseCancelled(BaseException):
    """解析被用户取消（方案 3.4）。继承 BaseException 防止被通用 except 吞掉。"""


def raise_if_cancelled(material_id: int) -> None:
    """分页/分段阶段取消检查：parse_tasks.cancel_requested=1 即中断。"""
    row = fetch_one(
        "SELECT cancel_requested FROM parse_tasks WHERE material_id=? "
        "ORDER BY id DESC LIMIT 1",
        (material_id,),
    )
    if row and row["cancel_requested"]:
        raise ParseCancelled(f"material {material_id} 解析已取消")


# ------------------------------- 主入口 ---------------------------------------
async def run_parse_material(material_id: int) -> dict:
    row = fetch_one(
        "SELECT id, file_path, type, kind, name, course_id, chapter_id, lesson_id "
        "FROM materials WHERE id=?",
        (material_id,),
    )
    if not row:
        raise RuntimeError(f"material {material_id} 不存在")
    raise_if_cancelled(material_id)                      # 取消检查：开始前
    execute(
        "UPDATE materials SET parser_status='parsing', status='parsing', updated_at=datetime('now','localtime') WHERE id=?",
        (material_id,),
    )
    path = row.get("file_path")
    if not path or not Path(path).exists():
        _fail(material_id, "文件不存在")
        raise RuntimeError(f"material {material_id} 文件不存在")

    kind = (row.get("kind") or row.get("type") or "").lower()
    suf = Path(path).suffix.lower()

    # 类型支持矩阵（方案 4.4）：图片/音频不产生占位 chunk、不伪装 ready
    if kind == "image" or suf in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        execute(
            "UPDATE materials SET parser_status='needs_ocr', status='needs_ocr', parse_error=NULL, "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (material_id,),
        )
        return {"material_id": material_id, "state": "needs_ocr", "chunk_count": 0}
    if kind == "audio" or suf in (".mp3", ".m4a", ".wav"):
        execute(
            "UPDATE materials SET parser_status='transcribing', status='transcribing', parse_error=NULL, "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (material_id,),
        )
        return {"material_id": material_id, "state": "transcribing", "chunk_count": 0}

    execute("DELETE FROM source_chunks WHERE material_id=?", (material_id,))

    located: list[tuple] = []
    if kind in ("pdf",) or suf == ".pdf":
        located = _parse_pdf(path)
    elif kind in ("ppt",) or suf in (".ppt", ".pptx"):
        located = _parse_pptx(path)
    elif kind in ("doc",) or suf in (".docx", ".doc"):
        located = _parse_docx(path)
    else:
        located = _parse_text(path)

    chunk_count = 0
    for locator, text in located:
        for seg in split_segments(text):
            if chunk_count and chunk_count % 50 == 0:
                raise_if_cancelled(material_id)          # 取消检查：每 50 段
            insert(
                "INSERT INTO source_chunks (material_id, lesson_id, chapter_id, course_id, type, locator, text, "
                " ocr_confidence, created_at) "
                "VALUES (?,?,?,?,?,?,?, 1.0, datetime('now','localtime'))",
                (material_id, row.get("lesson_id"), row.get("chapter_id"), row.get("course_id"),
                 kind or "text", locator, seg),
            )
            chunk_count += 1
    raise_if_cancelled(material_id)                      # 取消检查：索引/落 ready 前
    _index_material(material_id)
    execute(
        "UPDATE materials SET parser_status='ready', status='ready', parse_error=NULL, "
        "updated_at=datetime('now','localtime') WHERE id=?",
        (material_id,),
    )
    return {"material_id": material_id, "chunk_count": chunk_count}


def _fail(material_id: int, reason: str):
    execute(
        "UPDATE materials SET parser_status='failed', status='failed', parse_error=?, "
        "updated_at=datetime('now','localtime') WHERE id=?",
        (reason, material_id),
    )


def _index_material(material_id: int) -> None:
    """登记 FTS5 检索索引（FTS5 虚拟表不支持 UPSERT：先删后插幂等）。"""
    from .rag import fts_delete_chunk_ids, fts_index_chunks
    try:
        rows = fetch_all("SELECT id, text FROM source_chunks WHERE material_id=?", (material_id,))
        if not rows:
            return
        fts_delete_chunk_ids([r["id"] for r in rows])
        fts_index_chunks([(r["id"], r["text"] or "") for r in rows])
    except Exception as e:
        logger.warning(f"FTS 索引写入失败: {e}")