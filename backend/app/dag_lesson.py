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

V6 Learning Engine Phase 1（V6_LEARNING_ENGINE != off 时生效）在链路前插入
材料完整性五步，并在末尾追加覆盖审计：

  resolve_material_domain → parse → normalize_materials → deduplicate_materials
  → build_source_map → plan_coverage → retrieve_context → ... → coverage_audit

Phase 1 刻意**不删除**旧节点：旧链仍然产出正式笔记，V6 只建立材料域、
Source Map、覆盖账本与门禁。低覆盖时该运行不得写 completed（见
learning_engine.coverage.get_degradation_reason 与 worker 的 degraded 终态）。
"""
from __future__ import annotations

import json
import logging

from . import database as _db
from .dag import BusinessError, DAG, DAGContext, DAGNode, RetryableModelError, RunCancelledError
from .database import execute, fetch_all, fetch_one, insert, resolve_materials
from .learning_engine.normalize import transcript_to_located_texts
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


def _material_chunks_ready(material_id: int) -> bool:
    """材料是否已解析完成且确实有 chunk（可跳过重复解析）。

    只认 ``parser_status='ready'`` **且** 已有 source_chunks 的材料：解析中是
    未完成、失败是无产物、needs_ocr/transcribing 是类型不支持，都必须走真实
    解析路径或被显式记账。
    """
    row = fetch_one("SELECT parser_status, status FROM materials WHERE id=?", (material_id,))
    if not row:
        return False
    status = str(row.get("parser_status") or row.get("status") or "").lower()
    if status != "ready":
        return False
    count = fetch_one(
        "SELECT COUNT(*) AS n FROM source_chunks WHERE material_id=?", (material_id,))
    return bool(count and int(count["n"] or 0) > 0)


async def parse_materials_node(ctx: DAGContext, model: str) -> dict:
    """解析材料 → source_chunks。

    V6 路径（缺陷 1 修复）：材料来自**冻结的 Material Domain**（经
    ``resolve_domain_materials`` 的既有安全路径校验），而不是依赖已被移除的
    ``resolve_materials`` 节点输出 —— 否则 shadow/on 模式下上传材料永远不会
    进入解析，Source Map 只剩占位 span。
    V5 off 路径：保持原有 ``resolve_materials`` 输出不变。
    """
    from .learning_engine import config as v6cfg

    if v6cfg.engine_enabled():
        materials = resolve_domain_materials(ctx)
    else:
        materials = ctx.outputs.get("resolve_materials", {}).get("materials", [])

    chunks = []
    total = 0
    parse_failures: list[dict] = []
    for m in materials:
        try:
            # 「已解析且已有 chunk」的材料不重复解析：重解析会先 DELETE 掉既有
            # source_chunks，一旦解析器失败就会把**已经有效的解析产物**变成
            # 空材料（进而被记成 required 材料无 canonical span）。
            if _material_chunks_ready(m["id"]):
                segs = fetch_all(
                    "SELECT id, locator FROM source_chunks WHERE material_id=? ORDER BY id",
                    (m["id"],))
                chunks.extend({"material_id": m["id"], "chunk_id": s["id"],
                               "locator": s["locator"]} for s in segs)
                continue
            await run_parse_material(m["id"])
            segs = fetch_all("SELECT id, locator FROM source_chunks WHERE material_id=? ORDER BY id", (m["id"],))
            chunks.extend({"material_id": m["id"], "chunk_id": s["id"], "locator": s["locator"]} for s in segs)
        except Exception as e:
            # 解析失败必须显式记账（不允许静默跳过）：domain 侧会把无 chunk 的
            # required 材料记为 failed/unsupported，覆盖门禁因此阻断 completed。
            logger.warning(f"材料解析失败 {m.get('id')}: {e}")
            parse_failures.append({"material_id": m.get("id"), "error": str(e)[:300]})
    transcript = (ctx.input.get("transcript") or "").strip()
    if transcript and not v6cfg.engine_enabled():
        # V5 路径：inline 转写切成 chunk 供旧链检索。
        # V6 路径由 build_source_map 统一物化 inline 转写 chunks（单一真相源），
        # 避免同一段转写被写两次、口径分叉。
        for i, seg in enumerate(split_segments(transcript, 600), 1):
            cid = insert(
                "INSERT INTO source_chunks (lesson_id, chapter_id, course_id, type, locator, text, created_at) "
                "VALUES (?,?,?, 'transcript', ?, ?, datetime('now','localtime'))",
                (ctx.input.get("lesson_id"), ctx.input.get("chapter_id"), ctx.input.get("course_id"),
                 f"seg:{i}", seg),

)
            chunks.append({"material_id": None, "chunk_id": cid, "locator": f"seg:{i}"})
            total += 1
    return {"chunks": chunks, "count": len(chunks),
            "materials": [m.get("id") for m in materials],
            "parse_failures": parse_failures}


#: V6 模式旧链候选窗口上限（与 V5 的 ``[:12]`` 口径一致，便于对照）。
V6_LEGACY_CANDIDATE_LIMIT = 12
#: V6 模式补充候选上限（对齐 V5 ``retrieve_chunks(top_k=8)``）。
V6_LEGACY_TOPUP_LIMIT = 8


def _v6_domain_chunks(domain_id: int) -> list[dict]:
    """当前 frozen Material Domain 的 canonical 非噪声内容，映射回 source_chunks。

    这是 V6 模式下旧链候选的**唯一**合法来源。刻意从 ``source_spans``（Source
    Map，域的唯一真相源）读取而不是直接查 ``source_chunks``：

    * Source Map 已经完成归属校验与噪声/重复判定，文本是规范化后的正文；
    * 读取不再依赖「inline 转写是否已被 build_source_map 物化」这一时序假设
      （DAG 里 ``retrieve_context`` 与 ``build_source_map`` 处于同一拓扑层，
      直接查 ``source_chunks`` 会在物化完成前读到空表，让旧链拿到 0 个候选）。

    返回结构与旧链一致：``chunk_id`` 为对应 ``source_chunks.id``（可为 ``None``，
    表示该 span 没有 chunk 载体，此时用 span 自身的 ``id`` 作为稳定排序键）。
    """
    rows = fetch_all(
        "SELECT s.id AS span_id, s.source_id, s.source_chunk_id AS chunk_id, "
        " s.locator, s.text, s.ordinal "
        "FROM source_spans s WHERE s.domain_id=? AND s.span_state='included' "
        "ORDER BY s.ordinal, s.id", (int(domain_id),))
    return [dict(r) for r in rows]


def _v6_legacy_candidates(ctx: DAGContext, prompt_text: str) -> dict:
    """V6（shadow/on）模式的旧链候选：**只取当前 Material Domain 自己的内容**。

    修复（Phase 2/3 Final Closeout 缺陷 1）：早期实现对本课时做 lesson_id 全局
    RAG 检索，会把**同一课时历史 run**（乃至任意历史课堂）的 source_chunks 当作
    本轮材料喂给 note_writer/lesson_outline。两个 run 使用相同时间戳但正文不同
    （T000001 在两个 domain 里都存在）时，Run B 会拿到 Run A 的课堂内容 —— 既是
    跨 run 串课，也直接违背「材料域即真相源」。

    现在的口径：
      1. 候选 = 当前 frozen Material Domain 的 canonical 非噪声 span（含本 run
         inline 转写物化出的 span）；
      2. 按与检索词的字符重叠率排序后取 ``V6_LEGACY_CANDIDATE_LIMIT`` 条，其余显式
         记账为未选中（绝不静默丢弃）；
      3. **不做** lesson_id 全局 RAG；跨课时/跨课堂检索若要保留，必须是独立的
         显式 RAG/复习功能，而不是旧笔记链的隐式输入。
    """
    domain_id = getattr(ctx, "domain_id", None)
    domain_rows = _v6_domain_chunks(int(domain_id)) if domain_id else []

    def _overlap(row: dict) -> float:
        text = set(row.get("text") or "")
        return len(text & set(prompt_text)) / max(len(text), 1)

    ranked = sorted(domain_rows, key=lambda r: (-_overlap(r), int(r["ordinal"])))
    candidates: list[dict] = []
    seen: set[int] = set()
    for row in ranked[:V6_LEGACY_CANDIDATE_LIMIT]:
        key = int(row["ordinal"])
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"chunk_id": row.get("chunk_id"), "source_id": row["source_id"],
                           "locator": row.get("locator") or "",
                           "text": row.get("text") or "", "source": "domain"})
    # 域内还有内容但没进候选窗口：显式补充（旧链候选窗口有上限，必须留痕）。
    remaining = [r for r in domain_rows if int(r["ordinal"]) not in seen]
    for row in remaining[:V6_LEGACY_TOPUP_LIMIT]:
        seen.add(int(row["ordinal"]))
        candidates.append({"chunk_id": row.get("chunk_id"), "source_id": row["source_id"],
                           "locator": row.get("locator") or "",
                           "text": row.get("text") or "", "source": "domain_topup"})
    not_selected = [int(r["ordinal"]) for r in domain_rows if int(r["ordinal"]) not in seen]
    return {
        "candidates": candidates,
        "domain_chunk_total": len(domain_rows),
        "domain_chunk_used": len(seen),
        "domain_chunk_not_selected": not_selected,
        "domain_id": int(domain_id) if domain_id else None,
        "global_rag_used": False,
    }


async def retrieve_context_node(ctx: DAGContext, model: str) -> dict:
    """检索候选（方案 10.2）：本轮运行解析出的 chunk 优先，RAG 全局候选补充。

    V6（shadow/on）：候选**严格限制在当前冻结 Material Domain 内**，不使用
    lesson_id 全局 RAG（缺陷 1：跨 run 串课）。
    V5（off）：保持原有「本轮 chunk + 全局 RAG」行为不变。
    """
    from .learning_engine import config as v6cfg

    prompt_text = (ctx.input.get("retrieve_query") or ctx.input.get("transcript") or "课堂主要内容")[:800]

    if v6cfg.engine_enabled() and getattr(ctx, "domain_id", None):
        scoped = _v6_legacy_candidates(ctx, prompt_text)
        run_chunks = ctx.outputs.get("parse", {}).get("chunks", [])
        run_ids = {c["chunk_id"] for c in run_chunks if c.get("chunk_id")}
        return {
            **scoped,
            "count": len(scoped["candidates"]),
            "run_chunk_ids": sorted(run_ids),
            "run_chunks_used": sum(1 for c in scoped["candidates"]
                                   if c.get("chunk_id") in run_ids),
            "note": "V6：旧链候选只取当前 Material Domain 的 Source Map，禁用 lesson 级全局 RAG",
        }

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

    # 2) 全局 RAG 候选补充（hybrid 检索 + retrieval_runs 落库）—— 仅 V5 路径
    from .rag import retrieve_chunks
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
            "run_chunk_ids": run_ids, "run_chunks_used": sum(1 for c in candidates if c["source"] == "run_parsed"),
            "global_rag_used": True}


async def lesson_outline_node(ctx: DAGContext, model: str) -> dict:
    """课堂大纲（真实模型调用，prompt 外置 + schema 绑定）。

    材料来源：优先用 ``retrieve_context`` 的候选（V6 下即当前 Material Domain 的
    Source Map，V5 下即本轮 chunk + RAG）。早期实现只读 ``parse.chunks``，在 V6
    路径下上传材料/inline 转写的 chunk 不在 ``parse`` 输出里，导致旧链大纲在
    shadow/on 模式下拿到「（无材料）」。
    """
    from .gateway import gateway
    from .integrations.prompts import render_prompt
    from .integrations.schemas import LessonOutlineOut, parse_model_output
    from .dag import RetryableModelError

    candidates = ctx.outputs.get("retrieve_context", {}).get("candidates", [])
    rows = [{"id": c.get("chunk_id"), "source_id": c.get("source_id"),
             "locator": c.get("locator") or "", "text": c.get("text") or ""}
            for c in candidates]
    ids = [r["id"] for r in rows if r["id"]]
    if not rows:
        chunks = ctx.outputs.get("parse", {}).get("chunks", [])
        ids = [c["chunk_id"] for c in chunks if c.get("chunk_id")]
        if ids:
            q = ",".join("?" * len(ids))
            rows = [dict(r) for r in fetch_all(
                f"SELECT id, locator, text FROM source_chunks WHERE id IN ({q})", tuple(ids))]
    material_blocks = "\n".join(
        f"CHUNK#{r['id']}: {(r.get('text') or '')[:300]}" for r in rows[:20]) or "（无材料）"
    try:
        p = render_prompt("lesson/lesson_outline", "v1", material_blocks=material_blocks)
        resp = await gateway.chat(model, [
            {"role": "system", "content": p["text"]},
            {"role": "user", "content": f"材料片段：\n{material_blocks}"},
        ], contract="lesson/lesson_outline", temperature=0.3)
        data = parse_model_output(LessonOutlineOut, resp.get("content", ""), "lesson_outline")
        return {"outline": data.get("outline", []),
                "source_chunk_ids": ids[:20],
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
        ], contract="lesson/note_writer", temperature=0.35)
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
        ], contract="lesson/critic", temperature=0.2)
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
        # Sync is an independent dimension. It must never overwrite the note's
        # evidence/review status.
        execute("UPDATE notes SET markdown_path=? WHERE id=?", (rel, note_id))
        try:
            from .learning_engine.quality import ensure_quality_state
            if ctx.run_id:
                ensure_quality_state(ctx.run_id, note_id=note_id, sync_status="synced")
        except Exception:
            pass
        return {"synced": True, "path": rel}
    except Exception as e:
        insert(
            "INSERT INTO sync_jobs (target, asset_id, status, retries, last_error, synced_at) "
            "VALUES ('obsidian', ?, 'pending', 0, ?, NULL)",
            (note_id, str(e)[:300]),
        )
        try:
            from .learning_engine.quality import ensure_quality_state
            if ctx.run_id:
                ensure_quality_state(ctx.run_id, note_id=note_id, sync_status="failed")
        except Exception:
            pass
        return {"synced": False, "note_id": note_id, "queued_retry": True}


async def render_document_node(ctx: DAGContext, model: str) -> dict:
    from .document_artifacts import artifact_public, normalize_document, render_artifact
    composed = ctx.outputs.get("compose_learning_content", {}) or {}
    note = composed or ctx.outputs.get("note_writer", {}).get("note", {}) or {}
    note_id = ctx.outputs.get("persist_note", {}).get("note_id")
    doc = normalize_document(note.get("document"), title=note.get("title", "听课笔记"),
                             body=note.get("body", ""), kind="lesson_note")
    try:
        from .learning_engine.quality import ensure_quality_state
        if ctx.run_id:
            ensure_quality_state(ctx.run_id, note_id=note_id, publication_status="rendering")
        artifact = artifact_public(render_artifact(owner_type="note", owner_id=note_id, document=doc))
        if ctx.run_id:
            pub = "rendered" if artifact.get("status") == "ready" else "partial"
            ensure_quality_state(ctx.run_id, note_id=note_id, publication_status=pub)
        return {"artifact": artifact, "content_hash": note.get("content_hash")}
    except Exception:
        try:
            if ctx.run_id:
                ensure_quality_state(ctx.run_id, note_id=note_id, publication_status="failed")
        except Exception:
            pass
        raise


# ===========================================================================
# V6 Learning Engine Phase 1 节点（01–05 + 覆盖审计）
#
# flag=off 时这些节点不进入 DAG，行为与原 V5 完全一致。
# ===========================================================================
async def resolve_material_domain_node(ctx: DAGContext, model: str) -> dict:
    """01 resolve_material_domain：冻结 Material Domain（幂等、归属校验）。"""
    from .learning_engine import config as v6cfg
    from .learning_engine.domain import freeze_material_domain

    if not v6cfg.engine_enabled():
        return {"enabled": False, "skipped": True}
    domain = freeze_material_domain(
        run_id=ctx.run_id,
        course_id=ctx.input.get("course_id"),
        chapter_id=ctx.input.get("chapter_id"),
        lesson_id=ctx.input.get("lesson_id"),
        material_ids=ctx.input.get("material_ids") or [],
        transcript=ctx.input.get("transcript") or "",
    )
    ctx.domain_id = int(domain["id"])
    return {
        "enabled": True,
        "domain_id": int(domain["id"]),
        "domain_hash": domain["domain_hash"],
        "state": domain["state"],
        "engine_version": domain.get("engine_version"),
        "item_count": len(domain["items"]),
        "items": [{"id": int(i["id"]), "material_id": i["material_id"], "state": i["state"],
                   "reason_code": i["reason_code"]} for i in domain["items"]],
    }


def resolve_domain_materials(ctx: DAGContext) -> list[dict]:
    """从 frozen Material Domain 取出材料并通过既有安全路径校验（缺陷 1 修复）。

    V6 路径删除了 ``resolve_materials`` 节点，但 ``parse_materials_node`` 仍从
    ``ctx.outputs['resolve_materials']`` 读材料，导致 shadow/on 模式下
    ``material_ids`` 完全不进入解析 —— 上传材料永远不会生成 source_chunks。

    这里从**冻结域**读取 material_id，再走 ``database.resolve_materials``
    （= ``resolve_material_path``：uploads 根目录越权校验 + 文件存在校验），
    因此既修好了链路，也不放宽任何路径安全边界。
    """
    from .learning_engine.domain import get_domain
    from .dag import BusinessError

    domain_id = getattr(ctx, "domain_id", None)
    if not domain_id:
        return []
    domain = get_domain(int(domain_id))
    if not domain:
        raise BusinessError(f"Material Domain {domain_id} 不存在")
    material_ids = [int(i["material_id"]) for i in domain.get("items") or []
                    if i.get("material_id") is not None
                    and i.get("state") in ("included", "duplicate")]
    return resolve_materials(material_ids) if material_ids else []


async def normalize_materials_node(ctx: DAGContext, model: str) -> dict:
    """02 normalize_materials：确认规范化输入就绪（解析产物 + 每项终态）。

    规范化本身在冻结 domain 时完成（字符/token/哈希）；本节点的职责是把
    「材料是否可规范化」变成显式终态：解析没有产出 chunk 的材料必须在
    Source Map 中得到 ``failed``/``excluded_with_reason`` 记账，而不是被跳过。
    """
    from .learning_engine import config as v6cfg

    if not v6cfg.engine_enabled():
        return {"enabled": False, "skipped": True}
    domain_id = getattr(ctx, "domain_id", None)
    if not domain_id:
        return {"enabled": True, "domain_id": None, "skipped": True,
                "note": "domain 未建立（flag=off 或上游失败）"}
    rows = fetch_all(
        "SELECT i.id, i.state, i.source_kind, i.material_id, i.reason_code, "
        " (SELECT COUNT(*) FROM source_chunks c WHERE c.material_id = i.material_id) AS chunk_count "
        "FROM material_domain_items i WHERE i.domain_id=? ORDER BY i.ordinal, i.id",
        (domain_id,),
    )
    no_content = [r["id"] for r in rows
                  if r["state"] == "included" and int(r["chunk_count"] or 0) == 0
                  and r["material_id"] is not None]
    return {"enabled": True, "domain_id": domain_id, "item_count": len(rows),
            "items_without_chunks": no_content}


async def deduplicate_materials_node(ctx: DAGContext, model: str) -> dict:
    """03 deduplicate_materials：报告域内去重结果（不物理删除任何材料）。"""
    from .learning_engine import config as v6cfg

    if not v6cfg.engine_enabled():
        return {"enabled": False, "skipped": True}
    domain_id = getattr(ctx, "domain_id", None)
    if not domain_id:
        return {"enabled": True, "skipped": True}
    rows = fetch_all(
        "SELECT state, COUNT(*) AS n FROM material_domain_items WHERE domain_id=? "
        "GROUP BY state", (domain_id,),
    )
    counts = {r["state"]: int(r["n"]) for r in rows}
    dupes = fetch_all(
        "SELECT id, material_id, duplicate_of_item_id, reason_code FROM material_domain_items "
        "WHERE domain_id=? AND state='duplicate' ORDER BY ordinal", (domain_id,),
    )
    return {"enabled": True, "domain_id": domain_id, "state_counts": counts,
            "duplicate_items": [dict(r) for r in dupes],
            "physical_deletions": 0}


async def build_source_map_node(ctx: DAGContext, model: str) -> dict:
    """04 build_source_map：分配稳定 Source ID 并持久化 Source Map。"""
    from .learning_engine import config as v6cfg
    from .learning_engine.domain import build_source_map

    if not v6cfg.engine_enabled():
        return {"enabled": False, "skipped": True}
    domain_id = getattr(ctx, "domain_id", None)
    if not domain_id:
        return {"enabled": True, "skipped": True}
    summary = build_source_map(int(domain_id))
    return {"enabled": True, "domain_id": int(domain_id), **summary}


async def plan_coverage_node(ctx: DAGContext, model: str) -> dict:
    """05 plan_coverage：确定性覆盖计划（只分段，不做相关性淘汰）。"""
    from .learning_engine import config as v6cfg
    from .learning_engine.coverage import build_coverage_plan, mark_map_ledger

    if not v6cfg.engine_enabled():
        return {"enabled": False, "skipped": True}
    domain_id = getattr(ctx, "domain_id", None)
    if not domain_id:
        return {"enabled": True, "skipped": True}
    ledger_counts = mark_map_ledger(int(domain_id), ctx.run_id)
    plan = build_coverage_plan(int(domain_id))
    plan["map_ledger_counts"] = ledger_counts
    return {"enabled": True, **plan}


async def plan_segment_boundaries_node(ctx: DAGContext, model: str) -> dict:
    """05.5 plan_segment_boundaries：分段边界规划（可选优化，失败软降级）。

    让一个长上下文模型先读完全部材料，标出知识点边界与建议切点，供节点 06
    在保证「每段至少一个完整知识点」的前提下装箱。

    本节点**不是必需链路**：任何失败都转成 ``status='fallback'``，由
    ``segment_lesson`` 用结构边界兜底，绝不中断整个 run。取消例外 ——
    取消必须穿透，否则 run 会停在 running。

    返回结构见 ``boundaries.plan_segment_boundaries``。
    """
    from .learning_engine import config as v6cfg
    from .learning_engine.boundaries import plan_segment_boundaries

    if not v6cfg.engine_enabled():
        return {"enabled": False, "skipped": True}
    domain_id = getattr(ctx, "domain_id", None)
    if not domain_id:
        return {"enabled": True, "skipped": True, "status": "fallback"}

    # 上界必须与节点 06 装箱用的是同一个值（都由首选理解模型派生）。
    planning_model = ctx.resolve_model("segment_understanding") or None
    try:
        result = await plan_segment_boundaries(
            int(domain_id), int(ctx.run_id or 0), model or "",
            planning_model_profile_id=planning_model)
    except BaseException as exc:  # noqa: BLE001
        import asyncio

        from .dag import RunCancelledError
        if isinstance(exc, (asyncio.CancelledError, RunCancelledError)):
            raise                       # 取消必须穿透
        if not isinstance(exc, Exception):
            raise                       # KeyboardInterrupt 等不吞
        logger.warning("分段边界规划失败，回退结构边界: %s", exc)
        result = {"status": "fallback", "detail": str(exc)[:300],
                  "segment_count": 0, "ku_count": 0, "breaks": []}
    return {"enabled": True, "domain_id": int(domain_id), **result}


async def segment_lesson_node(ctx: DAGContext, model: str) -> dict:
    """06 segment_lesson：把全部 canonical 非噪声 span 分成 primary segments。"""
    from .learning_engine import config as v6cfg
    from .learning_engine.segment import (
        SegmentationError, fit_segment_plan_to_budget, persist_segments, plan_segments,
        record_segment_ledger,
    )

    if not v6cfg.engine_enabled():
        return {"enabled": False, "skipped": True}
    domain_id = getattr(ctx, "domain_id", None)
    if not domain_id:
        return {"enabled": True, "skipped": True, "segment_count": 0}
    # 按当前首选理解模型的真实上下文规划；旧实现固定按 32K 默认档案，导致长上下文
    # 模型也被不必要地拆成多次调用。fallback 候选会在节点 07 重新核对并按需重建。
    planning_model = ctx.resolve_model("segment_understanding") or None
    # 上游语义建议（节点 05.5）：仅当校验通过时才采用；否则传 None 走结构边界
    # 回退。建议只决定「优先在哪切」，最终仍受预算硬约束与 fit_segment_plan_to_budget。
    boundary = ctx.outputs.get("plan_segment_boundaries") or {}
    preferred = None
    if boundary.get("status") == "succeeded":
        preferred = [int(b) for b in (boundary.get("breaks") or [])] or None
    try:
        plan = plan_segments(int(domain_id), model_profile_id=planning_model,
                             preferred_breaks=preferred)
    except SegmentationError as e:
        # 单个 span 超过上下文预算：不允许丢弃材料，直接失败
        raise BusinessError(f"分段失败：{e}")
    # 最终消息的 Source ID 清单和 segment_total 可能让 prompt 比初步估算略大。
    # 自动移除 overlap / 继续拆分 primary，保证不丢材料，也不因微小估算差整单失败。
    try:
        plan, budget_check = fit_segment_plan_to_budget(int(domain_id), plan)
    except SegmentationError as e:
        raise BusinessError(f"分段预算修复失败：{e}")
    segments = persist_segments(int(domain_id), ctx.run_id, plan)
    pending = record_segment_ledger(int(domain_id), ctx.run_id)
    total_primary = sum(int(s.get("primary_span_count") or 0) for s in segments)
    return {
        "enabled": True,
        "domain_id": int(domain_id),
        "strategy": plan["strategy"],
        "segment_count": len(segments),
        "primary_span_total": total_primary,
        "input_budget_tokens": plan["budget"],
        "planning_model_profile_id": planning_model,
        "boundary_status": boundary.get("status", "absent"),
        "preferred_breaks_used": len(preferred or []),
        "total_tokens": plan["total_tokens"],
        "unassigned_count": len(plan["unassigned_source_ids"]),
        "ledger_pending": pending.get("pending", 0),
        "overlap_trimmed": plan.get("overlap_trimmed", 0),
        "budget_repairs": plan.get("budget_repairs", 0),
        "budget_check": {k: v for k, v in budget_check.items() if k != "segments"},
        "segments": [{"segment_id": s["id"], "ordinal": s["ordinal"],
                      "input_hash": s["input_hash"],
                      "primary_span_count": s["primary_span_count"],
                      "overlap_span_count": s["overlap_span_count"]} for s in segments],
    }


async def understand_segments_node(ctx: DAGContext, model: str) -> dict:
    """07 understand_segments：逐段全量理解（并发、可重试、不跳段）。

    ``model`` 是 DAG 当前**候选模型**，必须透传到每次 segment 调用，否则模型
    fallback 会退化为「始终用路由首选模型重试」。
    """
    from .learning_engine import config as v6cfg
    from .learning_engine.segment import (
        SegmentationError, fit_segment_plan_to_budget, persist_segments, plan_segments,
        record_segment_ledger,
    )
    from .learning_engine.understanding import run_understand_segments

    if not v6cfg.engine_enabled():
        return {"enabled": False, "skipped": True}
    domain_id = getattr(ctx, "domain_id", None)
    if not domain_id:
        return {"enabled": True, "skipped": True, "segment_count": 0, "failed": 0}
    # 域内没有任何 canonical 非噪声 span（例如全部材料解析失败）：无内容可理解。
    # 这不是「成功」，也不是「崩溃」—— 必须让覆盖审计继续跑并把运行判为
    # failed（required_items_without_canonical_span > 0），而不是在 DAG 中途抛错
    # 导致连覆盖报告都写不出来。
    try:
        # 每个 fallback 候选都必须用自己的上下文窗口复核。较小模型会自动得到更多
        # segment；较大模型可合并为更少调用。方案变化会使旧 segment 产物失效，
        # 但 primary Source Span 始终完整保留。
        candidate_plan = plan_segments(int(domain_id), model_profile_id=model or None)
        candidate_plan, _ = fit_segment_plan_to_budget(int(domain_id), candidate_plan)
        persist_segments(int(domain_id), ctx.run_id, candidate_plan)
        record_segment_ledger(int(domain_id), ctx.run_id)
        result = await run_understand_segments(ctx, int(domain_id), ctx.run_id, model_id=model)
    except RunCancelledError:
        raise
    except SegmentationError as e:
        raise RetryableModelError(f"候选模型 {model} 无法安全容纳分段: {e}")
    except Exception as e:
        raise RetryableModelError(f"understand_segments 失败: {e}")
    if result.get("segment_count", 0) == 0:
        return {"enabled": True, **result, "no_canonical_spans": True}
    if not result.get("all_succeeded"):
        # 不得用「跳过失败 segment」换取成功：整节点显式失败，覆盖门禁据此阻断。
        raise RetryableModelError(
            f"understand_segments 未成功完成全部 segment（候选模型 {model}）："
            f"{result.get('failed')} 个失败 / 共 {result.get('segment_count')}"
        )
    return {"enabled": True, **result}


async def merge_understanding_node(ctx: DAGContext, model: str) -> dict:
    """08 merge_lesson_understanding：消费全部 segment，产出全局理解。"""
    from .learning_engine import config as v6cfg
    from .learning_engine.understanding import run_merge_lesson_understanding

    if not v6cfg.engine_enabled():
        return {"enabled": False, "skipped": True}
    domain_id = getattr(ctx, "domain_id", None)
    if not domain_id:
        return {"enabled": True, "skipped": True}
    # 没有 canonical span 时没有可合并的内容：仍写一份 status=partial 的空理解，
    # 让 API 与覆盖审计口径一致（不伪装 succeeded）。
    seg_count = len(fetch_all(
        "SELECT id FROM lesson_segments WHERE domain_id=?", (domain_id,)))
    if seg_count == 0:
        from .learning_engine.understanding import record_empty_understanding
        record_empty_understanding(int(domain_id), ctx.run_id, ctx.input.get("lesson_id"))
        return {"enabled": True, "status": "partial", "segment_count": 0,
                "consumed_segment_count": 0, "knowledge_unit_count": 0,
                "note": "无 canonical span，未产生 segment"}
    result = await run_merge_lesson_understanding(
        ctx, int(domain_id), ctx.run_id, ctx.input.get("lesson_id"))
    return {"enabled": True, **result}


async def student_simulator_node(ctx: DAGContext, model: str) -> dict:
    """09 student_simulator：学生在课堂内容上可能的认知加工（Phase 3）。

    只产出 CognitiveMap**中间产物**；不写最终笔记、不修改 LessonUnderstanding。
    """
    from .learning_engine import config as v6cfg
    from .learning_engine.cognition import run_student_simulator

    if not v6cfg.engine_enabled():
        return {"enabled": False, "skipped": True}
    domain_id = getattr(ctx, "domain_id", None)
    if not domain_id:
        return {"enabled": True, "skipped": True, "status": "empty", "item_count": 0}
    result = await run_student_simulator(
        ctx, int(domain_id), ctx.run_id, ctx.input.get("lesson_id"), model_id=model)
    return {"enabled": True, **result}


async def bind_evidence_node(ctx: DAGContext, model: str) -> dict:
    """11 bind_evidence：Evidence V2 确定性绑定（Phase 4）。

    本节点是 **local** 节点，**不调用模型**：模型只负责选 Source ID，原文一律由
    程序从 ``source_spans`` 读取并绑定（``bound_quote`` 只可能来自数据库）。
    """
    from .learning_engine import config as v6cfg
    from .learning_engine.evidence_v2 import run_bind_evidence

    if not v6cfg.engine_enabled():
        return {"enabled": False, "skipped": True}
    domain_id = getattr(ctx, "domain_id", None)
    if not domain_id:
        return {"enabled": True, "skipped": True, "gate": "failed",
                "degradation_reason": "Material Domain 未建立"}
    return run_bind_evidence(int(domain_id), ctx.run_id)


async def compose_learning_content_node(ctx: DAGContext, model: str) -> dict:
    """10/Phase 5: assemble the official V6 structured note from audited inputs."""
    from .learning_engine.composer import compose_document
    from .learning_engine.quality import ensure_quality_state
    ensure_quality_state(ctx.run_id, processing_status="running")
    result = compose_document(ctx.run_id)
    ensure_quality_state(ctx.run_id, processing_status="completed",
                         evidence_status=("passed" if result.get("evidence_gate") == "passed"
                                          else "failed"))
    return result


async def critic_v2_node(ctx: DAGContext, model: str) -> dict:
    """13/Phase 5: fail-closed structural/grounding review of the V6 composition."""
    from .learning_engine.composer import review_composition
    from .learning_engine.quality import ensure_quality_state
    result = review_composition(ctx.run_id, ctx.outputs.get("compose_learning_content") or {})
    ensure_quality_state(ctx.run_id,
                         review_status=("passed" if result["passed"] else "revision_required"),
                         degradation_reason=("; ".join(result["issues"]) if result["issues"] else None),
                         metrics_json={"composer_review": result})
    return {"quality_review": result, "issues": result["issues"], "revisions": 0}


async def persist_note_v2_node(ctx: DAGContext, model: str) -> dict:
    """14/Phase 5: persist the composed note and its immutable structured revision."""
    import hashlib
    from .learning_engine.quality import ensure_quality_state
    composition = ctx.outputs.get("compose_learning_content") or {}
    review = (ctx.outputs.get("critic_v2") or {}).get("quality_review") or {}
    evidence = ctx.outputs.get("bind_evidence") or {}
    status = "confirmed" if review.get("passed") and evidence.get("gate") == "passed" else "draft"
    note_id = insert(
        "INSERT INTO notes (lesson_id,chapter_id,course_id,title,body,status,version,evidence_json,model_used,created_at) "
        "VALUES (?,?,?,?,?,?,1,?,'v6_composer_v2',datetime('now','localtime'))",
        (ctx.input.get("lesson_id"), ctx.input.get("chapter_id"), ctx.input.get("course_id"),
         composition.get("title") or "听课笔记", composition.get("body") or "", status,
         json.dumps({"evidence_v2_run_id": ctx.run_id}, ensure_ascii=False)))
    doc = composition.get("document") or {}
    encoded = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    digest = composition.get("content_hash") or hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    insert("INSERT INTO note_revisions (note_id,run_id,revision,schema_version,structured_json,content_hash,"
           "evidence_status,coverage_status,review_status,selection_audit_json) VALUES (?,?,1,?,?,?,?,?,?,?)",
           (note_id, ctx.run_id, composition.get("schema_version") or "v6-note-composer.1", encoded,
            digest, evidence.get("gate") or "failed", "pending",
            "passed" if review.get("passed") else "revision_required",
            json.dumps(composition.get("selection_audit") or [], ensure_ascii=False)))
    # Associate every projected claim with the official note/block. This is the bridge
    # intentionally left NULL in Phase 4.
    block_map = {}
    for section in doc.get("sections") or []:
        for ordinal, block in enumerate(section.get("blocks") or [], 1):
            if block.get("claim_key"):
                block_map[block["claim_key"]] = f"{section.get('title','section')}:{ordinal}"
    for claim_key, block_id in block_map.items():
        execute("UPDATE content_claims SET note_id=?,block_id=?,updated_at=datetime('now','localtime') "
                "WHERE run_id=? AND claim_key=?", (note_id, block_id, ctx.run_id, claim_key))
    ensure_quality_state(ctx.run_id, note_id=note_id,
                         processing_status="completed",
                         evidence_status=evidence.get("gate") or "failed",
                         review_status="passed" if review.get("passed") else "revision_required")
    if ctx.input.get("lesson_id"):
        execute("UPDATE lessons SET status='note_ready' WHERE id=?", (ctx.input["lesson_id"],))
    if ctx.input.get("chapter_id"):
        execute("UPDATE chapters SET status='in_progress' WHERE id=?", (ctx.input["chapter_id"],))
    return {"note_id": note_id, "status": status, "revision": 1,
            "content_hash": digest, "critic_passed": bool(review.get("passed"))}


async def coverage_audit_node(ctx: DAGContext, model: str) -> dict:
    """12 coverage_audit（Phase 1 版）：写覆盖账本与报告，给出门禁结论。"""
    from .learning_engine import config as v6cfg
    from .learning_engine.coverage import record_coverage_audit_for_run

    if not v6cfg.engine_enabled():
        return {"enabled": False, "skipped": True}
    domain_id = getattr(ctx, "domain_id", None)
    if not domain_id:
        return {"enabled": True, "skipped": True, "gate": "failed",
                "degradation_reason": "Material Domain 未建立"}
    # 旧 V5 生成链**候选窗口**真实消费的 span：写入独立的 legacy_candidate stage。
    # Phase 2 起 semantic_processing_rate 只认 understand_segments/processed，
    # 因此旧链用量不再参与门禁，但仍完整保留账目用于对照（不允许用 V5 的
    # Top-K 笔记覆盖率冒充 V6 理解覆盖率）。
    used = set()
    retrieve_out = ctx.outputs.get("retrieve_context", {}) or {}
    for cand in retrieve_out.get("candidates", []) or []:
        chunk_id = cand.get("chunk_id")
        if chunk_id:
            used.add(int(chunk_id))
    outline_ids = (ctx.outputs.get("lesson_outline", {}) or {}).get("source_chunk_ids") or []
    used.update(int(c) for c in outline_ids)
    full = fetch_all(
        "SELECT id, source_id FROM source_spans WHERE domain_id=? AND span_state='included' "
        "ORDER BY ordinal, id", (domain_id,),
    )
    directly_covered = set()
    if used:
        q = ",".join("?" * len(used))
        for r in fetch_all(
            f"SELECT id FROM source_spans WHERE domain_id=? AND source_chunk_id IN ({q})",
            tuple([domain_id] + sorted(used)),
        ):
            directly_covered.add(int(r["id"]))

    from .learning_engine.coverage import (
        REASON_LEGACY_TOP_K, record_ledger_entry,
    )
    legacy_processed = legacy_not_used = 0
    for r in full:
        span_id = int(r["id"])
        if span_id in directly_covered:
            record_ledger_entry(
                run_id=ctx.run_id, domain_id=int(domain_id), source_span_id=span_id,
                source_id=r["source_id"], stage="legacy_candidate", outcome="processed",
            )
            legacy_processed += 1
        else:
            record_ledger_entry(
                run_id=ctx.run_id, domain_id=int(domain_id), source_span_id=span_id,
                source_id=r["source_id"], stage="legacy_candidate", outcome="not_used",
                reason_code=REASON_LEGACY_TOP_K,
                reason_detail="旧生成链候选窗口（运行内 chunk 上限 12 / RAG top_k=8）未选中",
            )
            legacy_not_used += 1
    report = record_coverage_audit_for_run(int(domain_id), int(ctx.run_id))
    # Phase 4：Evidence V2 结论。审计以 content_claims/claim_sources 为主；
    # legacy evidence（旧 ``evidence`` 节点 + evidence_links）只是兼容投影，
    # 绝不能覆盖 Evidence V2 的门禁结论。
    evidence = ctx.outputs.get("bind_evidence", {}) or {}
    evidence_gate = evidence.get("gate")
    evidence_failed = bool(evidence.get("enabled")) and evidence_gate == "failed"
    gate = report.gate.value
    degradation_reason = report.degradation_reason
    if evidence_failed:
        # 覆盖门禁通过但证据门禁失败时，运行整体仍不得视为成功。
        gate = "failed" if gate == "passed" else gate
        evidence_reason = evidence.get("degradation_reason") or "Evidence V2 门禁未通过"
        degradation_reason = (f"{degradation_reason}；{evidence_reason}"
                              if degradation_reason else evidence_reason)
    result = {
        "enabled": True,
        "domain_id": int(domain_id),
        "gate": gate,
        "degradation_reason": degradation_reason,
        "coverage_gate": report.gate.value,
        "silent_dropped": report.silent_dropped,
        "domain_accounting_rate": report.domain_accounting_rate,
        "semantic_processing_rate": report.semantic_processing_rate,
        "timeline_coverage_rate": report.timeline_coverage_rate,
        "ppt_pages_total": report.ppt_pages_total,
        "ppt_pages_processed": report.ppt_pages_processed,
        "issues": report.issues,
        "canonical_non_noise_spans": report.canonical_non_noise_spans,
        "processed_spans": report.processed_spans,
        "required_items_without_canonical_span": report.required_items_without_canonical_span,
        "required_spans_unprocessed": report.required_spans_unprocessed,
        "segment_count": report.segment_count,
        "segment_failed_count": report.segment_failed_count,
        "segments_consumed_by_merge": report.segments_consumed_by_merge,
        "legacy_candidate_rate": report.legacy_candidate_rate,
        "legacy_ledger": {"processed": legacy_processed, "not_used": legacy_not_used},
        "legacy_candidate_scope": {
            "domain_id": retrieve_out.get("domain_id"),
            "domain_chunk_total": retrieve_out.get("domain_chunk_total"),
            "domain_chunk_used": retrieve_out.get("domain_chunk_used"),
            "domain_chunk_not_selected": retrieve_out.get("domain_chunk_not_selected") or [],
            "global_rag_used": bool(retrieve_out.get("global_rag_used", False)),
        },
        # ---- Evidence V2（任务书 §八要求 coverage_audit 展示这些字段）----
        "evidence_enabled": bool(evidence.get("enabled")),
        "evidence_gate": evidence_gate,
        "evidence_degradation_reason": evidence.get("degradation_reason"),
        "claims_total": evidence.get("claims_total"),
        "required_claims": evidence.get("required_claims"),
        "bound_claims": evidence.get("bound_claims"),
        "failed_claims": evidence.get("failed_claims"),
        "not_required_claims": evidence.get("not_required_claims"),
        "critical_claim_evidence_rate": evidence.get("critical_claim_evidence_rate"),
        "critical_evidence_denominator": evidence.get("critical_evidence_denominator"),
        "invalid_source_refs": evidence.get("invalid_source_refs"),
        "cross_domain_refs": evidence.get("cross_domain_refs"),
        "unbound_source_refs": evidence.get("unbound_source_refs"),
        "unread_source_refs": evidence.get("unread_source_refs"),
        "claims_by_type": evidence.get("claims_by_type"),
        "legacy_evidence_projection_rows": evidence.get("legacy_projection_rows"),
    }
    # Phase 5 keeps each dimension independent: coverage cannot overwrite evidence,
    # review, publication or sync.
    try:
        from .learning_engine.quality import ensure_quality_state
        ensure_quality_state(
            ctx.run_id,
            coverage_status=("passed" if report.gate.value == "passed"
                             else "failed" if report.gate.value == "failed" else "degraded"),
            evidence_status=(evidence_gate if evidence_gate in
                             ("passed", "partial", "failed", "not_required") else "failed"),
            degradation_reason=degradation_reason,
        )
        note_id = (ctx.outputs.get("persist_note") or {}).get("note_id")
        if note_id:
            execute("UPDATE note_revisions SET coverage_status=? WHERE note_id=? AND revision=(SELECT MAX(revision) FROM note_revisions WHERE note_id=?)",
                    (("passed" if report.gate.value == "passed" else "degraded"), note_id, note_id))
    except Exception as exc:
        logger.warning("quality state update failed for run %s: %s", ctx.run_id, exc)
    return result


def build_lesson_dag() -> DAG:
    """装配听课 DAG。

    V6_LEARNING_ENGINE=off  ：原 V5 十节点链，行为不变。
    shadow 保留 legacy 出版；on 使用全量理解、认知、Evidence V2 和 Composer V2
    生成正式 HTML/PDF。两条出版链不会在同一 run 混写。
    """
    from .learning_engine import config as v6cfg

    dag = DAG("lesson", "attend")
    enabled = v6cfg.engine_enabled()
    v6_publish = v6cfg.real_notes_publish_enabled()
    if enabled:
        dag.add(DAGNode("resolve_material_domain", "local", resolve_material_domain_node,
                        kind="local"))
        dag.add(DAGNode("parse", "local", parse_materials_node,
                        depends_on=["resolve_material_domain"], kind="local"))
        dag.add(DAGNode("normalize_materials", "local", normalize_materials_node,
                        depends_on=["parse"], kind="local"))
        dag.add(DAGNode("deduplicate_materials", "local", deduplicate_materials_node,
                        depends_on=["normalize_materials"], kind="local"))
        dag.add(DAGNode("build_source_map", "local", build_source_map_node,
                        depends_on=["deduplicate_materials"], kind="local"))
        dag.add(DAGNode("plan_coverage", "local", plan_coverage_node,
                        depends_on=["build_source_map"], kind="local"))
        dag.add(DAGNode("plan_segment_boundaries", "segment_boundary_planner",
                        plan_segment_boundaries_node,
                        preferred_models=["qwen3_flash", "deepseek_v4_free"],
                        fallback_models=["glm_flash"],
                        depends_on=["plan_coverage"]))
        dag.add(DAGNode("segment_lesson", "local", segment_lesson_node,
                        depends_on=["plan_segment_boundaries"], kind="local"))
        dag.add(DAGNode("understand_segments", "segment_understanding", understand_segments_node,
                        preferred_models=["qwen3_flash", "deepseek_v4_free"],
                        fallback_models=["glm_flash"],
                        depends_on=["segment_lesson"]))
        dag.add(DAGNode("merge_understanding", "local", merge_understanding_node,
                        depends_on=["understand_segments"], kind="local"))
        # Phase 3：认知层。依赖合并后的全局理解，产出 CognitiveMap 中间产物。
        dag.add(DAGNode("student_simulator", "student_simulator", student_simulator_node,
                        preferred_models=["qwen3_flash", "deepseek_v4_free"],
                        fallback_models=["glm_flash"],
                        depends_on=["merge_understanding"]))
        # 旧链仍产出正式笔记（legacy publication），但候选必须来自本 run 的
        # Source Map。因此 retrieve_context 依赖 build_source_map，而不是 parse：
        # parse 只负责文件材料，inline 转写是在 build_source_map 里物化的，若与
        # build_source_map 同层并发，旧链会在物化前读到空的 Source Map。
        # Phase 4：Evidence V2（local，不调模型）。必须在 coverage_audit 之前，
        # 因为覆盖审计要展示 evidence gate，并据此阻止「内容链成功」的报告。
        dag.add(DAGNode("bind_evidence", "local", bind_evidence_node,
                        depends_on=["student_simulator"], kind="local"))
        retrieve_deps = ["build_source_map"]
        merge_deps = ["merge_understanding"]
        cognitive_deps = ["student_simulator"]
        evidence_deps = ["bind_evidence"]
    else:
        dag.add(DAGNode("resolve_materials", "local", resolve_materials_node, kind="local"))
        dag.add(DAGNode("parse", "local", parse_materials_node,
                        depends_on=["resolve_materials"], kind="local"))
        retrieve_deps = ["parse"]
        merge_deps = None
        cognitive_deps = None
        evidence_deps = None

    dag.add(DAGNode("retrieve_context", "local", retrieve_context_node,
                    depends_on=retrieve_deps, kind="local"))
    if v6_publish:
        dag.add(DAGNode("compose_learning_content", "local", compose_learning_content_node,
                        depends_on=["bind_evidence"], kind="local"))
        dag.add(DAGNode("critic_v2", "local", critic_v2_node,
                        depends_on=["compose_learning_content"], kind="local"))
        dag.add(DAGNode("persist_note", "local", persist_note_v2_node,
                        depends_on=["critic_v2"], kind="local"))
    else:
        dag.add(DAGNode("lesson_outline", "lesson_structurer", lesson_outline_node,
                        preferred_models=["qwen3_flash", "deepseek_v4_free"],
                        fallback_models=["glm_flash"],
                        depends_on=["retrieve_context"]))
        dag.add(DAGNode("note_writer", "note_writer", note_writer_node,
                        preferred_models=["deepseek_v4_free", "qwen3_flash"],
                        fallback_models=["glm_flash"],
                        depends_on=["lesson_outline"]))
        # Legacy compatibility is retained only in off/shadow publication.
        dag.add(DAGNode("evidence", "local", evidence_verifier_node,
                        depends_on=["note_writer"], kind="local"))
        dag.add(DAGNode("critic", "critic", critic_node,
                        preferred_models=["glm_flash", "qwen3_flash"], depends_on=["evidence"]))
        dag.add(DAGNode("persist_note", "local", persist_note_node,
                        depends_on=["critic"], kind="local"))
    if enabled:
        audit_deps = ["persist_note"] + merge_deps + cognitive_deps + evidence_deps
        dag.add(DAGNode("coverage_audit", "local", coverage_audit_node,
                        depends_on=audit_deps, kind="local"))
        dag.add(DAGNode("render_document", "local", render_document_node,
                        depends_on=["coverage_audit"], kind="local"))
    else:
        dag.add(DAGNode("render_document", "local", render_document_node,
                        depends_on=["persist_note"], kind="local"))
    dag.add(DAGNode("obsidian_sync", "local", obsidian_sync_node, depends_on=["render_document"], kind="local"))
    return dag
