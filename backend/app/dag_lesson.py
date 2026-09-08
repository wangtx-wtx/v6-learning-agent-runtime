"""
听课流 DAG（V5.4 重写，对应方案 7.1）。

链路：
  resolve_materials (local: ID→安全路径、课程归属校验)
  → parse (local: 解析材料 + 生成 source_chunks)
  → retrieve_context (local: RAG 候选 chunk)
  → lesson_outline (llm: 课堂大纲)
  → note_writer (llm: 只能引用候选 chunk)
  → evidence (local: 校验引用，含 derived_reasoning 容忍)
  → critic (llm: 质量审查)
  → persist_note (local: 写 notes；证据失败→draft)
  → obsidian_sync (local: 可选，失败仅生成待重试 sync_job)
"""
from __future__ import annotations

import json
import logging

from .dag import DAG, DAGContext, DAGNode
from .database import execute, fetch_all, fetch_one, insert
from .material_parser import run_parse_material, split_segments

logger = logging.getLogger(__name__)


async def resolve_materials_node(ctx: DAGContext, model: str) -> dict:
    from .database import resolve_materials, insert
    from .dag import BusinessError

    course_id = ctx.input.get("course_id")
    material_ids = ctx.input.get("material_ids") or []
    if not material_ids:
        if (ctx.input.get("transcript") or "").strip():
            return {"materials": [], "source": "transcript_only"}
        raise BusinessError("听课流需要至少一个 material_ids 或 transcript")
    resolved = resolve_materials(material_ids)
    for m in resolved:
        row = fetch_one("SELECT course_id FROM materials WHERE id=?", (m["id"],))
        if row and row.get("course_id") and course_id and row["course_id"] != course_id:
            raise BusinessError(f"材料 {m['id']} 不属于课程 {course_id}")
    return {"materials": resolved, "course_id": course_id}


async def parse_materials_node(ctx: DAGContext, model: str) -> dict:
    materials = ctx.outputs.get("resolve_materials", {}).get("materials", [])
    chunks = []
    total = 0
    for m in materials:
        try:
            await run_parse_material(m["id"])
            segs = fetch_all("SELECT id, locator FROM source_chunks WHERE material_id=? ORDER BY id", (m["id"],))
            chunks.extend({"material_id": m["id"], "chunk_id": s["id"], "locator": s["locator"]} for s in segs)
        except Exception as e:
            logger.warning(f"材料解析失败 {m.get('id')}: {e}")
    transcript = (ctx.input.get("transcript") or "").strip()
    if transcript:
        for i, seg in enumerate(split_segments(transcript, 600), 1):
            cid = insert(
                "INSERT INTO source_chunks (lesson_id, chapter_id, course_id, type, locator, text, created_at) "
                "VALUES (?,?,?, 'transcript', ?, ?, datetime('now','localtime'))",
                (ctx.input.get("lesson_id"), ctx.input.get("chapter_id"), ctx.input.get("course_id"),
                 f"seg:{i}", seg),

)
            chunks.append({"material_id": None, "chunk_id": cid, "locator": f"seg:{i}"})
            total += 1
    return {"chunks": chunks, "count": len(chunks), "materials": [m.get("id") for m in materials]}


async def retrieve_context_node(ctx: DAGContext, model: str) -> dict:
    """检索候选（方案 10.2）：本轮运行解析出的 chunk 优先，RAG 全局候选补充。"""
    from .rag import retrieve_chunks
    prompt_text = (ctx.input.get("retrieve_query") or ctx.input.get("transcript") or "课堂主要内容")[:800]

    candidates, seen = [], set()

    # 1) 本轮运行产生的 chunk（current_run_chunk_ids）最高优先
    run_chunks = ctx.outputs.get("parse", {}).get("chunks", [])
    run_ids = [c["chunk_id"] for c in run_chunks if c.get("chunk_id")]
    if run_ids:
        q = ",".join("?" * len(run_ids))
        rows = fetch_all(
            f"SELECT id, locator, text, material_id FROM source_chunks WHERE id IN ({q}) ORDER BY id",
            tuple(run_ids),
        )
        # 简单相关性排序：与检索词的字符重叠率（无需额外模型）
        def _overlap(r):
            t = set(r["text"] or "")
            k = set(prompt_text)
            return len(t & k) / max(len(t), 1)
        rows = sorted(rows, key=_overlap, reverse=True)[:12]
        for r in rows:
            if r["id"] not in seen:
                candidates.append({"chunk_id": r["id"], "locator": r["locator"] or "",
                                   "text": r["text"] or "", "source": "run_parsed"})
                seen.add(r["id"])

    # 2) 全局 RAG 候选补充（hybrid 检索 + retrieval_runs 落库）
    try:
        results = await retrieve_chunks(
            prompt_text,
            chapter_id=ctx.input.get("chapter_id"), lesson_id=ctx.input.get("lesson_id"),
            top_k=8, run_id=ctx.run_id,
            mode=(ctx.input.get("retrieval_mode") or "hybrid"),
        )
    except Exception as e:
        logger.warning(f"retrieve_chunks: {e}")
        results = []
    for r in results:
        if r.get("chunk_id") not in seen:
            candidates.append({"chunk_id": r.get("chunk_id"), "locator": r.get("locator", ""),
                               "text": r.get("text", ""), "source": "rag"})
            seen.add(r.get("chunk_id"))

    return {"candidates": candidates, "count": len(candidates),
            "run_chunk_ids": run_ids, "run_chunks_used": sum(1 for c in candidates if c["source"] == "run_parsed")}


async def lesson_outline_node(ctx: DAGContext, model: str) -> dict:
    """课堂大纲（真实模型调用，prompt 外置 + schema 绑定）。"""
    from .gateway import gateway
    from .integrations.prompts import render_prompt
    from .integrations.schemas import LessonOutlineOut, parse_model_output
    from .dag import RetryableModelError

    chunks = ctx.outputs.get("parse", {}).get("chunks", [])
    rows = []
    ids = [c["chunk_id"] for c in chunks if c.get("chunk_id")]
    if ids:
        q = ",".join("?" * len(ids))
        rows = fetch_all(f"SELECT id, text FROM source_chunks WHERE id IN ({q})", tuple(ids))
    material_blocks = "\n".join(f"CHUNK#{r['id']}: {(r['text'] or '')[:300]}" for r in rows[:20]) or "（无材料）"
    try:
        p = render_prompt("lesson/lesson_outline", "v1", material_blocks=material_blocks)
        resp = await gateway.chat(model, [
            {"role": "system", "content": p["text"]},
            {"role": "user", "content": f"材料片段：\n{material_blocks}"},
        ], temperature=0.3)
        data = parse_model_output(LessonOutlineOut, resp.get("content", ""), "lesson_outline")
        return {"outline": data.get("outline", []),
                "prompt_checksum": p["checksum"],
                "tokens_in": resp.get("tokens_in", 0), "tokens_out": resp.get("tokens_out", 0)}
    except Exception as e:
        raise RetryableModelError(f"lesson_outline 失败: {e}")


async def note_writer_node(ctx: DAGContext, model: str) -> dict:
    from .gateway import gateway
    from .integrations.prompts import render_prompt
    from .integrations.schemas import NoteWriterOut, parse_model_output
    from .dag import SchemaValidationError, RetryableModelError

    candidates = ctx.outputs.get("retrieve_context", {}).get("candidates", [])
    outline = ctx.outputs.get("lesson_outline", {}).get("outline", [])
    cand_block = "\n".join(
        f"CHUNK#{c['chunk_id']} ({c.get('locator','')}): {c['text'][:500]}" for c in candidates
    ) or "（无可用课程材料）"
    revise_hint = ""
    issues = ctx.input.get("_critic_issues") or []
    if issues:
        revise_hint = ("评审指出以下问题，请针对性修订笔记（其他内容保持稳定）：\n- "
                       + "\n- ".join(issues))
    else:
        revise_hint = "（首次撰写）"
    p = render_prompt("lesson/note_writer", "v1", revise_section=revise_hint,
                      outline_json=json.dumps(outline, ensure_ascii=False),
                      material_blocks=cand_block)
    try:
        resp = await gateway.chat(model, [
            {"role": "system", "content": p["text"]},
            {"role": "user", "content": f"材料候选：\n{cand_block}"},
        ], temperature=0.35)
        data = parse_model_output(NoteWriterOut, resp.get("content", ""), "note_writer")
        return {"note": data, "prompt_checksum": p["checksum"],
                "tokens_in": resp.get("tokens_in", 0), "tokens_out": resp.get("tokens_out", 0),
                "revised": bool(issues)}
    except (SchemaValidationError, RetryableModelError):
        raise
    except Exception as e:
        raise RetryableModelError(str(e))


async def evidence_verifier_node(ctx: DAGContext, model: str) -> dict:
    from .evidence import verify_evidence_list
    note = ctx.outputs.get("note_writer", {}).get("note", {}) or {}
    ev_list = note.get("evidence", []) or []
    verified = verify_evidence_list(
        ev_list, owner_type="note", owner_id=ctx.run_id or 0, persist=True,
        model=ctx.outputs.get("note_writer", {}).get("model_used", ""),
        prompt_version="note_writer.v1",
    )
    # 证据为空不得自动确认（方案 10.3）：必须至少有一条通过校验的证据
    ok = bool(ev_list) and all(v.get("verified", False) for v in verified)
    return {"verified": verified, "all_verified": ok, "note": note,
            "evidence_empty": not ev_list}


CRITIC_SCHEMA_HINT = (
    '只输出 JSON：{"score": <0-1 小数>, "passed": <true|false>, '
    '"issues": ["具体问题1", ...]}。issues 为空数组表示通过。'
)


async def _critic_llm_call(ctx: DAGContext, model: str, ev: dict) -> dict:
    """真实 critic 模型调用（prompt 外置 + schema 绑定），失败退化为保守结论。"""
    from .gateway import gateway
    from .integrations.prompts import render_prompt
    from .integrations.schemas import CriticOut, parse_model_output

    note = ev.get("note", {}) or {}
    verified = ev.get("verified", [])
    ev_ok = ev.get("all_verified", False)
    ev_block = "\n".join(
        f"- [{'通过' if v.get('verified') else '未通过'}] {v.get('quote', '')[:80]}" for v in verified
    ) or "（无证据）"
    try:
        p = render_prompt("lesson/critic", "v1",
                          note_title=note.get("title", ""),
                          note_body=(note.get("body") or "")[:2000],
                          evidence_ok=str(ev_ok),
                          evidence_blocks=ev_block)
        resp = await gateway.chat(model, [
            {"role": "system", "content": p["text"]},
            {"role": "user", "content": f"笔记标题：{note.get('title','')}\n证据校验：{ev_ok}"},
        ], temperature=0.2)
        data = parse_model_output(CriticOut, resp.get("content", ""), "critic")
        issues = [str(x) for x in data.get("issues", [])][:8]
        return {"issues": issues,
                "quality_review": {"score": float(data.get("score", 0.6 if not ev_ok else 0.8)),
                                   "passed": bool(data.get("passed", False)) and ev_ok,
                                   "issues": issues},
                "tokens_in": resp.get("tokens_in", 0), "tokens_out": resp.get("tokens_out", 0),
                "prompt_checksum": p["checksum"],
                "model_used": model}
    except Exception as e:
        logger.warning(f"critic 模型调用失败，退化为证据校验结论: {e}")
        return {"issues": [] if ev_ok else ["critic 调用失败，且存在未通过的证据"],
                "quality_review": {"score": 0.8 if ev_ok else 0.4,
                                   "passed": ev_ok,
                                   "issues": [] if ev_ok else ["critic 调用失败"]},
                "tokens_in": 0, "tokens_out": 0, "model_used": "fallback"}


async def critic_node(ctx: DAGContext, model: str) -> dict:
    """真实 critic 审查 + 修订闭环（最多修订 1 次，方案 10.4）。"""
    ev = ctx.outputs.get("evidence") or await evidence_verifier_node(ctx, model)
    ctx.set_output("evidence", ev)
    result = await _critic_llm_call(ctx, model, ev)
    revisions = 0
    total_in = result.get("tokens_in", 0)
    total_out = result.get("tokens_out", 0)
    if not result["quality_review"]["passed"] and result["issues"]:
        # 修订闭环（max 1）：带问题重写笔记 → 重新校验证据 → 重新审查
        ctx.input["_critic_issues"] = result["issues"]
        note_out = await note_writer_node(ctx, model)
        ctx.set_output("note_writer", note_out)
        ev2 = await evidence_verifier_node(ctx, model)
        ctx.set_output("evidence", ev2)
        result = await _critic_llm_call(ctx, model, ev2)
        total_in += note_out.get("tokens_in", 0) + result.get("tokens_in", 0)
        total_out += note_out.get("tokens_out", 0) + result.get("tokens_out", 0)
        revisions = 1
    result["revisions"] = revisions
    result["tokens_in"] = total_in
    result["tokens_out"] = total_out
    return result


async def persist_note_node(ctx: DAGContext, model: str) -> dict:
    note = ctx.outputs.get("note_writer", {}).get("note", {}) or {}
    ev_out = ctx.outputs.get("evidence", {}) or {}
    ev_ok = ev_out.get("all_verified", False)
    critic = ctx.outputs.get("critic", {}) or {}
    critic_passed = critic.get("quality_review", {}).get("passed", False)
    ev_empty = ev_out.get("evidence_empty", False)
    if ev_ok and critic_passed:
        status = "confirmed"
    elif ev_empty:
        status = "draft"     # 证据为空：不得自动确认（方案 10.3）
    else:
        status = "draft"
    note_id = insert(
        "INSERT INTO notes (lesson_id, chapter_id, course_id, title, body, status, version, evidence_json, model_used, created_at) "
        "VALUES (?,?,?,?,?,?, 1, ?, ?, datetime('now','localtime'))",
        (ctx.input.get("lesson_id"), ctx.input.get("chapter_id"), ctx.input.get("course_id"),
         note.get("title", "听课笔记"), note.get("body", ""),
         status,
         json.dumps(note.get("evidence", []), ensure_ascii=False),
         ctx.outputs.get("note_writer", {}).get("model_used", "")),

)
    if ctx.input.get("lesson_id"):
        execute("UPDATE lessons SET status='note_ready' WHERE id=?", (ctx.input["lesson_id"],))
    if ctx.input.get("chapter_id"):
        execute("UPDATE chapters SET status='in_progress' WHERE id=?", (ctx.input["chapter_id"],))
    return {"note_id": note_id, "status": status,
            "critic_passed": critic_passed, "evidence_empty": ev_empty}


async def obsidian_sync_node(ctx: DAGContext, model: str) -> dict:
    """按真实课程/章节/课时路径写 vault（方案 9.1/9.2），失败仅入重试队列。"""
    note_id = ctx.outputs.get("persist_note", {}).get("note_id")
    row = fetch_one("SELECT * FROM notes WHERE id=?", (note_id,)) if note_id else None
    if not row:
        return {"status": "skipped", "reason": "no note"}
    try:
        from .obsidian import write_note_vault, resolve_note_paths
        paths = resolve_note_paths(row.get("course_id"), row.get("chapter_id"), row.get("lesson_id"))
        meta = {
            "course": paths["course"], "chapter": paths["chapter"],
            "lesson": paths["lesson"] or None,
            "type": "lesson-note", "status": row.get("status", "draft"),
            "note_id": note_id,
        }
        md = f"# {row.get('title','') or '听课笔记'}\n\n{row.get('body','') or ''}\n"
        rel = write_note_vault(paths["course"], paths["chapter"], paths["lesson"],
                               row.get("title") or "听课笔记", md, meta,
                               stable_id=f"note-{note_id}")
        execute("UPDATE notes SET markdown_path=?, status='synced' WHERE id=?", (rel, note_id))
        return {"synced": True, "path": rel}
    except Exception as e:
        insert(
            "INSERT INTO sync_jobs (target, asset_id, status, retries, last_error, synced_at) "
            "VALUES ('obsidian', ?, 'pending', 0, ?, NULL)",
            (note_id, str(e)[:300]),
        )
        return {"synced": False, "note_id": note_id, "queued_retry": True}


def build_lesson_dag() -> DAG:
    dag = DAG("lesson", "attend")
    dag.add(DAGNode("resolve_materials", "local", resolve_materials_node, kind="local"))
    dag.add(DAGNode("parse", "local", parse_materials_node, depends_on=["resolve_materials"], kind="local"))
    dag.add(DAGNode("retrieve_context", "local", retrieve_context_node, depends_on=["parse"], kind="local"))
    dag.add(DAGNode("lesson_outline", "lesson_structurer", lesson_outline_node,
                    preferred_models=["qwen3_flash", "deepseek_v4_free"],
                    fallback_models=["deepseek_v4_official"],
                    depends_on=["retrieve_context"]))
    dag.add(DAGNode("note_writer", "note_writer", note_writer_node,
                    preferred_models=["deepseek_v4_free", "deepseek_v4_official"],
                    fallback_models=["qwen3_8_27b"],
                    depends_on=["lesson_outline"]))
    dag.add(DAGNode("evidence", "local", evidence_verifier_node, depends_on=["note_writer"], kind="local"))
    dag.add(DAGNode("critic", "critic", critic_node,
                    preferred_models=["qwen3_8_27b"], depends_on=["evidence"]))
    dag.add(DAGNode("persist_note", "local", persist_note_node, depends_on=["critic"], kind="local"))
    dag.add(DAGNode("obsidian_sync", "local", obsidian_sync_node, depends_on=["persist_note"], kind="local"))
    return dag