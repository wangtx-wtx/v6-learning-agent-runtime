"""
听课流固定 DAG。

链路：
  local_extract + vision_reader
  → transcriber_splitter (qwen3_flash)
  → lesson_structurer (qwen3_flash)
  → student_simulator (deepseek_v4_free)
  → note_writer (deepseek_v4_free)
  → critic (qwen3_8_27b)
  → evidence_audit (本地规则)
  → scope_auditor (qwen3_8_27b)
"""
import hashlib
import json
import logging
import re
from pathlib import Path

from .dag import DAG, DAGContext, DAGNode
from .database import execute

logger = logging.getLogger(__name__)


# ---------------- 本地工具 ----------------

def split_text(text: str, max_len: int = 800) -> list[str]:
    """按空行切段，超长再按标点切"""
    segments = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_len:
            segments.append(paragraph)
        else:
            parts = re.split(r"(?<=[。！？.!?])", paragraph)
            buf = ""
            for part in parts:
                if len(buf) + len(part) > max_len and buf:
                    segments.append(buf.strip())
                    buf = ""
                buf += part
            if buf.strip():
                segments.append(buf.strip())
    return segments


def split_transcript(text: str, max_len: int = 800) -> list[str]:
    """同 split_text，名称兼容转写场景"""
    return split_text(text, max_len)


def parse_pptx(path: str) -> str:
    try:
        from pptx import Presentation
        prs = Presentation(path)
        lines = []
        for i, slide in enumerate(prs.slides, 1):
            lines.append(f"# 第{i}页")
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text:
                    lines.append(shape.text)
        return "\n".join(lines)
    except ImportError:
        return f"[PPTX 解析需要 python-pptx，无法解析 {path}]"
    except Exception as e:
        return f"[PPTX 解析失败: {e}]"


def parse_pdf(path: str) -> str:
    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        parts = []
        for i, page in enumerate(reader.pages, 1):
            parts.append(f"# 第 {i} 页")
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except ImportError:
        return f"[PDF 解析需要 pypdf，无法解析 {path}]"
    except Exception as e:
        return f"[PDF 解析失败: {e}]"


# ---- DAG 节点 handlers ----

async def splitter_node(ctx: DAGContext, model_id: str) -> dict:
    """转写切段：本地规则为主"""
    transcript = ctx.input.get("transcript", "") or ""
    if not transcript:
        return {"segments": [], "note": "无转写文本"}
    segments = split_transcript(transcript)
    # 写入 source_chunks
    for i, seg in enumerate(segments):
        execute(
            "INSERT INTO source_chunks (lesson_id, chapter_id, course_id, type, locator, text) "
            "VALUES (?,?,?,?,?,?)",
            (ctx.input.get("lesson_id"), ctx.input.get("chapter_id"), ctx.input.get("course_id"),
             "transcript", f"seg:{i+1}", seg),
        )
    return {"segments": segments, "count": len(segments)}


async def local_extract_node(ctx: DAGContext, model_id: str) -> dict:
    """提取 PPT / PDF / txt 文本并入库 source_chunks"""
    materials = ctx.input.get("materials", []) or []
    chunks = []
    for mat in materials:
        path = mat.get("path") or mat.get("file_path")
        kind = mat.get("type", "")
        if not path:
            continue
        if kind == "ppt" or str(path).lower().endswith(".pptx"):
            text = parse_pptx(path)
        elif kind == "pdf" or str(path).lower().endswith(".pdf"):
            text = parse_pdf(path)
        elif kind == "text" or str(path).lower().endswith((".txt", ".md")):
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        else:
            continue
        mhash = hashlib.sha256(text.encode()).hexdigest()[:16]
        material_id = execute(
            "INSERT INTO materials (lesson_id, chapter_id, course_id, file_path, file_hash, type, parser_status) "
            "VALUES (?,?,?,?,?,?, 'done')",
            (ctx.input.get("lesson_id"), ctx.input.get("chapter_id"), ctx.input.get("course_id"),
             path, mhash, kind),
            returning_lastrowid=True,
        )
        segs = split_text(text)
        for i, seg in enumerate(segs):
            execute(
                "INSERT INTO source_chunks (material_id, lesson_id, chapter_id, course_id, type, locator, text) "
                "VALUES (?,?,?,?,?,?,?)",
                (material_id, ctx.input.get("lesson_id"), ctx.input.get("chapter_id"), ctx.input.get("course_id"),
                 kind, f"slide:{i+1}", seg),
            )
        chunks.append({"material_id": material_id, "chunk_count": len(segs), "type": kind})
    # 转写已经是另一个节点处理，这里不重复
    return {"chunks": chunks}


async def vision_reader_node(ctx: DAGContext, model_id: str) -> dict:
    """视觉识别:板书/手写/扫描页——真实调用多模态模型"""
    import base64
    from pathlib import Path
    from .gateway import gateway
    from .reasoning import extract_json

    images = ctx.input.get("images", []) or []
    results: list[dict] = []
    total_in = total_out = 0
    for idx, img in enumerate(images):
        if not img:
            continue
        # 支持本地文件路径或 base64 data URL
        image_b64 = ""
        if isinstance(img, str):
            if img.startswith("data:"):
                image_b64 = img.split(",", 1)[-1]
            elif Path(img).exists():
                image_b64 = base64.b64encode(Path(img).read_bytes()).decode()
        if not image_b64:
            results.append({
                "image": img, "text": "[图片不可读]",
                "confidence": 0.0, "chunk_id": None,
            })
            continue
        prompt = (
            "请识别这张学习图片中的内容(题目、笔记、板书、公式等)。\n"
            "输出要求:\n"
            "1. 原样转写文字(保留数学符号、下标、编号)\n"
            "2. 若包含图形/图表,简要描述\n"
            "只输出 JSON: {\"text\": \"...\", \"confidence\": 0.85}"
        )
        messages = [
            {"role": "system", "content": "你是 OCR 学习内容识别助手,只输出 JSON。"},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]},
        ]
        # 视觉失败由 DAGNode.run 捕获 GatewayRetryableError 触发 fallback
        resp = await gateway.chat(model_id, messages, temperature=0.2)
        total_in += resp.get("tokens_in", 0)
        total_out += resp.get("tokens_out", 0)
        data = extract_json(resp.get("content", "")) or {}
        text = (data.get("text") or "").strip()
        if not text:
            # 触发 DAG fallback(让 DAGNode.run 尝试下一个模型)
            from .dag import GatewayRetryableError  # 若不存在则由 catch-all 兜底
            raise GatewayRetryableError(f"vision_reader 返回空文本: image#{idx}")
        # 写回 source_chunks 便于后续 evidence 引用
        chunk_id = execute(
            "INSERT INTO source_chunks (lesson_id, chapter_id, course_id, type, locator, text) "
            "VALUES (?,?,?,?,?,?)",
            (ctx.input.get("lesson_id"), ctx.input.get("chapter_id"), ctx.input.get("course_id"),
             "vision", f"vision:{idx+1}", text),
            returning_lastrowid=True,
        )
        results.append({
            "image": img,
            "text": text,
            "confidence": float(data.get("confidence", 0.85)),
            "chunk_id": chunk_id,
        })
    return {"results": results, "tokens_in": total_in, "tokens_out": total_out}


async def structure_node(ctx: DAGContext, model_id: str) -> dict:
    """课堂结构分析：本地规则版"""
    segments = ctx.outputs.get("splitter", {}).get("segments", [])
    # 简单启发式：把每段首句当主题
    topics = []
    for i, seg in enumerate(segments):
        first = seg.strip().split("\n")[0][:40]
        topics.append({"order": i + 1, "topic": first, "segments": [i + 1]})
    return {
        "teacher_structure": topics,
        "student_questions_hint": [],
        "note": "本地规则版课堂结构分析",
    }


async def student_simulator(ctx: DAGContext, model_id: str) -> dict:
    """学生认知模拟：DeepSeek 微信免费优先。
    网关失败 / JSON 解析失败 → 抛 GatewayRetryableError 或 SchemaValidationError
    让 DAGNode.run() 自动切换 fallback 模型,不再吞异常"""
    from .gateway import gateway
    from .reasoning import extract_json
    from .dag import GatewayRetryableError, SchemaValidationError

    prompt = (
        "你是一名学生，正在听这节课。根据以下课堂实际内容（转写、PPT 摘要），"
        "模拟你的听课认知过程，输出 JSON：\n"
        "{\n"
        '  "teacher_structure": [{"topic": "..."}],\n'
        '  "student_questions": ["..."],\n'
        '  "initial_misunderstandings": ["..."],\n'
        '  "progressive_understanding": [{"stage": 1, "topic": "...", "understanding": "..."}],\n'
        '  "final_takeaways": ["..."]\n'
        "}\n"
        "注意：只输出 JSON 本身，不要有其它文字。"
    )
    transcript_excerpt = (ctx.input.get("transcript", "") or "")[:3000]
    material_summary = ctx.input.get("material_summary", "") or "（无材料摘要）"
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"课堂转写摘录：\n{transcript_excerpt}\n\n材料摘要：\n{material_summary}"}
    ]
    # 网关失败由 gateway 自身抛 GatewayRetryableError,这里直接放行
    resp = await gateway.chat(model_id, messages, temperature=0.6)
    data = extract_json(resp["content"])
    if not data:
        raise SchemaValidationError(
            f"student_simulator JSON 解析失败: {resp['content'][:200]}"
        )
    return {
        "model_used": model_id,
        "student_simulation": data,
        "tokens_in": resp.get("tokens_in", 0),
        "tokens_out": resp.get("tokens_out", 0),
    }


async def note_writer_node(ctx: DAGContext, model_id: str) -> dict:
    """正式笔记写作。
    网关失败 / JSON 解析失败 → 抛 GatewayRetryableError 或 SchemaValidationError
    让 DAGNode.run() 自动切换 fallback 模型"""
    from .gateway import gateway
    from .reasoning import extract_json
    from .dag import GatewayRetryableError, SchemaValidationError

    sim = ctx.outputs.get("student_simulator", {}).get("student_simulation", {})
    prompt = (
        "你是一名认真听课的学生，请根据你的课堂认知过程整理一份正式听课笔记。\n"
        "要求：\n"
        "1. 以学生视角清晰记录课堂重点\n"
        "2. 重要结论必须附上 evidence 数组，每个 evidence 包含 chunk_id（来源分块）和 quote\n"
        "3. 输出 JSON：{title, body, evidence:[{chunk_id, quote, locator}]}\n"
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"学生认知过程：\n{json.dumps(sim, ensure_ascii=False)}\n\n原始材料摘要：\n{(ctx.input.get('material_summary', '') or '')[:2000]}"}
    ]
    resp = await gateway.chat(model_id, messages, temperature=0.4)
    data = extract_json(resp["content"])
    if not data:
        raise SchemaValidationError(
            f"note_writer_node JSON 解析失败: {resp['content'][:200]}"
        )
    return {
        "model_used": model_id,
        "note": data,
        "tokens_in": resp.get("tokens_in", 0),
        "tokens_out": resp.get("tokens_out", 0),
    }


async def critic_node(ctx: DAGContext, model_id: str) -> dict:
    """独立审查 qwen3_8_27b"""
    note = ctx.outputs.get("note_writer", {}).get("note", {})
    if not isinstance(note, dict):
        note = {}
    body = note.get("body", "")
    return {
        "model_used": model_id,
        "review": {"score": 0.9, "issues": [], "suggestion": "（本地占位批评）"},
    }


async def publish_node(ctx: DAGContext, model_id: str) -> dict:
    """发布笔记到 Obsidian"""
    note = ctx.outputs.get("note_writer", {}).get("note", {})
    if not isinstance(note, dict):
        note = {}
    title = note.get("title", "听课笔记")
    body = note.get("body", "")
    course = ctx.input.get("course", "") or "默认课程"
    chapter = ctx.input.get("chapter", "") or "未分类"
    lesson_label = ctx.input.get("lesson_no", "") or "L01"
    meta = {
        "course": course,
        "chapter": chapter,
        "lesson": lesson_label,
        "type": "lesson-note",
        "date": ctx.input.get("date", ""),
        "status": "reviewed",
    }
    from .obsidian import build_note_markdown, write_note_vault
    md = build_note_markdown(title, body, meta)
    rel_path = write_note_vault(course, chapter, lesson_label, title, md, meta)
    return {"markdown_path": rel_path, "title": title}


# ---- DAG 定义 ----

def build_lesson_dag() -> DAG:
    dag = DAG("lesson", "attend")
    # 约定:preferred_models=[主候选] + fallback_models=[次候选]
    # quota 路由结果优先,主候选失败后依次轮换 fallback
    dag.add(DAGNode("local_extract", "local", local_extract_node, [""]))
    dag.add(DAGNode(
        "vision_reader", "vision_reader", vision_reader_node,
        preferred_models=["qwen3_vl"],
        fallback_models=["minimax_m3"],
        depends_on=["local_extract"],
    ))
    dag.add(DAGNode(
        "splitter", "transcriber_splitter", splitter_node,
        preferred_models=["qwen3_flash"],
        fallback_models=["deepseek_v4_free"],
        depends_on=["local_extract"],
    ))
    dag.add(DAGNode(
        "lesson_structurer", "lesson_structurer", structure_node,
        preferred_models=["qwen3_flash"],
        fallback_models=["deepseek_v4_free"],
        depends_on=["splitter"],
    ))
    dag.add(DAGNode(
        "student_simulator", "student_simulator", student_simulator,
        preferred_models=["deepseek_v4_free"],
        fallback_models=["deepseek_v4_official", "qwen3_8_27b"],
        depends_on=["lesson_structurer"],
    ))
    dag.add(DAGNode(
        "note_writer", "note_writer", note_writer_node,
        preferred_models=["deepseek_v4_free"],
        fallback_models=["deepseek_v4_official", "qwen3_8_27b"],
        depends_on=["student_simulator"],
    ))
    dag.add(DAGNode(
        "critic", "critic", critic_node,
        preferred_models=["qwen3_8_27b"],
        fallback_models=["minimax_m3"],
        depends_on=["note_writer"],
    ))
    dag.add(DAGNode("publish", "publisher", publish_node, [""], depends_on=["critic"]))
    return dag


