"""
V5.4 材料解析管线（材料状态机）：
uploaded → queued → parsing → indexed → ready / failed

把材料文件解析成带稳定 locator 的 chunks（PDF 按页、PPT 按页、Markdown/文本按段、
图片/音频按占位说明），写入 source_chunks，并登记 FTS5 检索索引。
"""
from __future__ import annotations

import asyncio
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


def parse_transcript_chunks(text: str) -> list[tuple[str, str]]:
    """把转写文本解析为 ``[(locator, text)]``，locator 为 ``HH:MM:SS-HH:MM:SS``。

    时间戳格式支持 ``[00:12:34]`` / ``00:12:34`` / ``[00:12:34-00:13:10]`` /
    ``12:34`` / ``[00:12:34 --> 00:13:10] 正文``。**解析不出时间时不伪造时间**：
    该 cue 的 locator 退回 ``cue:N``，Source Span 的 ``start_ms/end_ms`` 保持 NULL。

    实际切分逻辑与 ``learning_engine.domain`` 的 inline 转写路径共用
    ``normalize.transcript_to_located_texts``，保证两条路径口径一致；这也是
    Phase 2「转写时间轴覆盖」可核对的前提。
    """
    from .learning_engine.normalize import transcript_to_located_texts

    return transcript_to_located_texts(text)


def _parse_transcript_file(path: str) -> list[tuple]:
    content = Path(path).read_text(encoding="utf-8", errors="ignore")
    located = parse_transcript_chunks(content)
    if located:
        return located
    # 无时间戳：退回整篇正文（Source Span 无时间轴，但不丢内容）
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
        located = await asyncio.to_thread(_parse_pdf, path)
    elif kind in ("ppt",) or suf in (".ppt", ".pptx"):
        located = await asyncio.to_thread(_parse_pptx, path)
    elif kind in ("doc",) or suf in (".docx", ".doc"):
        located = await asyncio.to_thread(_parse_docx, path)
    elif kind in ("transcript", "audio") or suf in (".srt", ".vtt"):
        # 转写材料：按 cue 切分并保留稳定时间 locator。
        located = await asyncio.to_thread(_parse_transcript_file, path)
    else:
        located = await asyncio.to_thread(_parse_text, path)

    chunk_count = 0
    insert_sql = (
        "INSERT INTO source_chunks (material_id, lesson_id, chapter_id, course_id, type, locator, text, "
        " ocr_confidence, created_at) VALUES (?,?,?,?,?,?,?, 1.0, datetime('now','localtime'))"
    )
    batch: list[tuple] = []
    for locator, text in located:
        for seg in split_segments(text):
            if chunk_count and chunk_count % 50 == 0:
                raise_if_cancelled(material_id)          # 取消检查：每 50 段
            batch.append((material_id, row.get("lesson_id"), row.get("chapter_id"),
                          row.get("course_id"), kind or "text", locator, seg))
            chunk_count += 1
            # 旧实现每个 chunk 都新开 SQLite 连接并提交；批量提交显著降低长转写
            # 的连接和 fsync 开销，同时保持小批次取消响应。
            if len(batch) >= 250:
                from .database import executemany
                executemany(insert_sql, batch)
                batch.clear()
    if batch:
        from .database import executemany
        executemany(insert_sql, batch)
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
    """登记 FTS5 检索索引（FTS5 虚拟表不支持 UPSERT：先删后插幂等）。

    注意：``fts_*`` 的签名是 ``(conn, ...)``，``conn=None`` 表示内部开独立短连接。
    早期调用漏传 ``conn``，异常被下面的 except 吞成一条 warning，导致所有材料
    解析后 FTS 索引**静默未写入**（keyword_only 检索因此长期缺索引）。
    """
    from .rag import fts_delete_chunk_ids, fts_index_chunks
    try:
        rows = fetch_all("SELECT id, text FROM source_chunks WHERE material_id=?", (material_id,))
        if not rows:
            return
        fts_delete_chunk_ids(None, [r["id"] for r in rows])
        fts_index_chunks(None, [(r["id"], r["text"] or "") for r in rows])
    except Exception as e:
        logger.warning(f"FTS 索引写入失败: {e}")
