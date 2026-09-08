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

from .database import execute, fetch_all, fetch_one

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


def _parse_image(path: str) -> list[tuple]:
    return [("image", f"[图片材料待视觉识别: {Path(path).name}]")]


def _parse_audio(path: str) -> list[tuple]:
    return [("audio", f"[音频材料待转写: {Path(path).name}]")]


# ------------------------------- 主入口 ---------------------------------------
async def run_parse_material(material_id: int) -> dict:
    row = fetch_one(
        "SELECT id, file_path, type, kind, name, course_id, chapter_id, lesson_id "
        "FROM materials WHERE id=?",
        (material_id,),
    )
    if not row:
        raise RuntimeError(f"material {material_id} 不存在")
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
    execute("DELETE FROM source_chunks WHERE material_id=?", (material_id,))

    located: list[tuple] = []
    if kind in ("pdf",) or suf == ".pdf":
        located = _parse_pdf(path)
    elif kind in ("ppt",) or suf in (".ppt", ".pptx"):
        located = _parse_pptx(path)
    elif kind in ("doc",) or suf in (".docx", ".doc"):
        located = _parse_docx(path)
    elif kind in ("image",) or suf in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        located = _parse_image(path)
    elif kind in ("audio",) or suf in (".mp3", ".m4a", ".wav"):
        located = _parse_audio(path)
    else:
        located = _parse_text(path)

    chunk_count = 0
    for locator, text in located:
        for seg in split_segments(text):
            execute(
                "INSERT INTO source_chunks (material_id, lesson_id, chapter_id, course_id, type, locator, text, "
                " ocr_confidence, created_at) "
                "VALUES (?,?,?,?,?,?,?, 1.0, datetime('now','localtime'))",
                (material_id, row.get("lesson_id"), row.get("chapter_id"), row.get("course_id"),
                 kind or "text", locator, seg),
            )
            chunk_count += 1
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
    """登记 FTS5 检索索引。"""
    from .rag import init_fts
    try:
        init_fts()
    except Exception as e:
        logger.warning(f"init_fts: {e}")
    try:
        rows = fetch_all("SELECT id, text FROM source_chunks WHERE material_id=?", (material_id,))
        for r in rows:
            execute(
                "INSERT INTO chunks_fts (rowid, text) VALUES (?, ?) "
                "ON CONFLICT(rowid) DO UPDATE SET text=excluded.text",
                (r["id"], r.get("text") or ""),
            )
    except Exception as e:
        logger.warning(f"FTS 索引写入失败: {e}")