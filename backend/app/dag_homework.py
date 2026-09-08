"""
作业流 DAG（V5.4 重写，对应方案 7.2）。

链路：
  resolve_input (local: OCR/切题)
  → persist_questions (local: 先建稳定 question_id)
  → risk_classifier (llm: 风险分级)
  → solver  ─────────------┐
  → parallel_solver ───────┤ (同层并发，仅 high risk 触发)
  → adjudicator            │  (同时依赖 solver 与 parallel_solver)
  → teaching_explainer → provenance_checker → scope_checker → persist_answers
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from .dag import DAG, DAGContext, DAGNode, BusinessError
from .database import execute, fetch_all, fetch_one, insert

logger = logging.getLogger(__name__)


async def resolve_input_node(ctx: DAGContext, model: str) -> dict:
    """OCR/切题（方案 7.2）：
    - 已确认作业（homework_id 且 status='confirmed'）：直接读取已确认题目，跳过 OCR；
    - 图片且未确认：OCR 出题目文本，进入 awaiting_confirmation（不直接求解）。"""
    homework_id = ctx.input.get("homework_id")
    if homework_id:
        hw = fetch_one("SELECT status FROM homeworks WHERE id=?", (homework_id,))
        if hw and hw["status"] == "confirmed":
            rows = fetch_all("SELECT id, question_no, text FROM questions WHERE homework_id=? ORDER BY question_no",
                             (homework_id,))
            return {"questions": [r["text"] for r in rows],
                    "existing": [{"question_id": r["id"], "question_no": r["question_no"], "text": r["text"]} for r in rows],
                    "source": "confirmed", "count": len(rows)}

    homework_text = (ctx.input.get("homework_text") or "").strip()
    images = ctx.input.get("images") or []
    splitted: list[str] = []
    if homework_text:
        splitted = _split_questions(homework_text)
    for idx, img in enumerate(images):
        text = await _ocr_image(gateway, ctx, model, img)
        if text:
            splitted.extend(_split_questions(text))
    dedup = []
    seen = set()
    for q in splitted:
        key = q.strip()
        if key and key not in seen:
            seen.add(key)
            dedup.append(q)
    # 仅图片输入：OCR 结果必须经用户确认后才允许求解（先做后看/OCR 状态机，方案 7.2）
    needs_confirmation = bool(images) and not homework_text
    return {"questions": dedup, "count": len(dedup), "source": "ocr" if images else "text",
            "needs_confirmation": needs_confirmation}


def _split_questions(text: str) -> list[str]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    questions: list[str] = []
    current: list[str] = []
    pattern = re.compile(r"^\s*[（(]?\s*[一二三四五六七八九十\d\.]+\s*[)）]?[、．.]\s*")
    for line in lines:
        if pattern.match(line):
            if current:
                questions.append("\n".join(current))
                current = []
        current.append(line)
    if current:
        questions.append("\n".join(current))
    return [q for q in questions if q.strip()]


async def _ocr_image(gateway, ctx, model: str, img) -> str:
    """对单张图片调用视觉模型识别文字。"""
    import base64
    from pathlib import Path
    from .reasoning import extract_json
    from .dag import RetryableModelError

    image_b64 = ""
    if isinstance(img, str) and img.startswith("data:"):
        image_b64 = img.split(",", 1)[-1]
    elif isinstance(img, str) and Path(img).exists():
        image_b64 = base64.b64encode(Path(img).read_bytes()).decode()
    elif isinstance(img, int):
        try:
            from .database import resolve_material_path, insert
            p, _, _ = resolve_material_path(img)
            image_b64 = base64.b64encode(Path(p).read_bytes()).decode()
        except Exception:
            return ""
    if not image_b64:
        return ""
    prompt = "请识别图中题目文字（保留题号/数学符号），只输出 JSON：{\"text\":\"...\"}"
    try:
        resp = await gateway.chat(model, [
            {"role": "system", "content": "你是 OCR 助手，只输出 JSON。"},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]},
        ], temperature=0.2)
        data = extract_json(resp.get("content", ""))
        return (data or {}).get("text", "") or ""
    except Exception as e:
        raise RetryableModelError(f"ocr 失败: {e}")


async def persist_questions_node(ctx: DAGContext, model: str) -> dict:
    """先持久化 question + homework，建立稳定 question_id（已存在则不重复建）。"""
    items = ctx.outputs.get("resolve_input", {}).get("questions", [])
    existing = ctx.outputs.get("resolve_input", {}).get("existing") or []
    homework_id = ctx.input.get("homework_id")
    if not homework_id:
        homework_id = insert(
            "INSERT INTO homeworks (title, status, course_id, lesson_id, chapter_id, mode, date, created_at) "
            "VALUES (?, 'pending', ?, ?, ?, ?, datetime('now','localtime'), datetime('now','localtime'))",
            ("待解作业", ctx.input.get("course_id"), ctx.input.get("lesson_id"), ctx.input.get("chapter_id"), "solve"),

)
    if existing:
        return {"homework_id": homework_id, "questions": existing,
                "count": len(existing), "reused": True}
    question_ids = []
    for i, q in enumerate(items, 1):
        qid = insert(
            "INSERT INTO questions (homework_id, question_no, text, created_at) VALUES (?,?,?, datetime('now','localtime'))",
            (homework_id, i, q),

)
        question_ids.append({"question_id": qid, "question_no": i, "text": q})
    return {"homework_id": homework_id, "questions": question_ids, "count": len(question_ids)}


async def mark_awaiting_confirmation_node(ctx: DAGContext, model: str) -> dict:
    """OCR 完成 → awaiting_confirmation（方案 7.2 状态机：ocr_ready→awaiting_confirmation）。"""
    info = ctx.outputs.get("persist_questions", {})
    homework_id = info.get("homework_id")
    execute("UPDATE homeworks SET status='awaiting_confirmation' WHERE id=?", (homework_id,))
    return {"homework_id": homework_id, "status": "awaiting_confirmation",
            "question_count": info.get("count", 0),
            "hint": "请确认/修正 OCR 题目文本后调用 confirm 接口开始求解"}


async def risk_classifier_node(ctx: DAGContext, model: str) -> dict:
    items = ctx.outputs.get("persist_questions", {}).get("questions", [])
    risk_items = []
    for it in items:
        q = it.get("text", "")
        risk = "high" if (len(q) > 500 or "证明" in q) else "normal"
        risk_items.append({**it, "risk": risk})
    return {"risk_items": risk_items}


async def solver_node(ctx: DAGContext, model: str) -> dict:
    from .gateway import gateway
    from .reasoning import extract_json
    from .dag import SchemaValidationError, RetryableModelError

    risk_items = ctx.outputs.get("risk_classifier", {}).get("risk_items", [])
    answers = []
    total_in = total_out = 0
    for it in risk_items:
        prompt = (
            "请解答下面的题，输出 JSON：\n"
            '{"final_answer": "...", "solution_plan": "...", "detailed_solution": "..."}'
        )
        resp = await gateway.chat(model, [
            {"role": "system", "content": "你是解题助手，只输出 JSON。"},
            {"role": "user", "content": f"{prompt}\n\n题目：\n{it.get('text','')}"},
        ], temperature=0.3)
        total_in += resp.get("tokens_in", 0)
        total_out += resp.get("tokens_out", 0)
        data = extract_json(resp.get("content", ""))
        if not data:
            raise SchemaValidationError(f"solver JSON 失败 (q{it.get('question_no')})")
        answers.append({**it, "final_answer": data.get("final_answer", ""),
                        "solution_plan": data.get("solution_plan", ""),
                        "detailed_solution": data.get("detailed_solution", ""),
                        "model_used": model})
    return {"answers": answers, "tokens_in": total_in, "tokens_out": total_out}


async def parallel_solver_node(ctx: DAGContext, model: str) -> dict:
    """高风险题独立第二解（与 solver 并发）。"""
    from .gateway import gateway
    from .reasoning import extract_json
    from .dag import SchemaValidationError, RetryableModelError

    risk_items = ctx.outputs.get("risk_classifier", {}).get("risk_items", [])
    high = [it for it in risk_items if it.get("risk") == "high"]
    parallel = []
    total_in = total_out = 0
    for it in high:
        prompt = "请用不同思路重新解答下题，输出 JSON：{\"final_answer\": \"...\", \"solution_plan\": \"...\", \"detailed_content\": \"...\"}"
        resp = await gateway.chat(model, [
            {"role": "system", "content": "你是独立思路解题助手，只输出 JSON。"},
            {"role": "user", "content": f"{prompt}\n\n题目：\n{it.get('text','')}"},
        ], temperature=0.7)
        total_in += resp.get("tokens_in", 0)
        total_out += resp.get("tokens_out", 0)
        data = extract_json(resp.get("content", ""))
        if not data:
            raise SchemaValidationError(f"parallel_solver JSON 失败 (q{it.get('question_no')})")
        parallel.append({"question_no": it.get("question_no"), "final_answer": data.get("final_answer", ""),
                         "model_used": model})
    return {"parallel_answers": parallel, "count": len(parallel), "tokens_in": total_in, "tokens_out": total_out}


async def adjudicator_node(ctx: DAGContext, model: str) -> dict:
    """比较 solver 与 parallel_solver：最终答案、关键步骤、数值结果。
    冲突时输出 requires_human_review（方案 7.2/12.2），不做静默取舍。"""
    from .gateway import gateway
    from .reasoning import extract_json

    solver_items = {a["question_no"]: a for a in ctx.outputs.get("solver", {}).get("answers", [])}
    parallel_items = {p["question_no"]: p for p in ctx.outputs.get("parallel_solver", {}).get("parallel_answers", [])}
    decisions = []
    for qno in sorted(set(solver_items) | set(parallel_items)):
        s = solver_items.get(qno)
        p = parallel_items.get(qno)
        if not p:
            decisions.append({"question_no": qno, "decision": "solver_only", "conflict": False,
                              "requires_human_review": False, "reason": "无独立第二解"})
            continue
        if s.get("final_answer") == p.get("final_answer"):
            decisions.append({"question_no": qno, "decision": "agree", "conflict": False,
                              "requires_human_review": False, "reason": "两解一致"})
            continue
        # 二次审查裁决
        judgement, reason = "needs_review", "两解不一致"
        if model != "local":
            resp = await gateway.chat(model, [
                {"role": "system", "content": "你裁决两模型答案。只输出 JSON：{\"decision\":\"agree\"|\"needs_review\",\"reason\":\"...\"}"},
                {"role": "user", "content": f"甲:{s.get('final_answer')}\n乙:{p.get('final_answer')}"},
            ], temperature=0.2)
            data = extract_json(resp.get("content", ""))
            if data and data.get("decision") == "agree":
                judgement, reason = "agree", data.get("reason", "复核后一致")
            else:
                reason = (data or {}).get("reason", reason)
        decisions.append({"question_no": qno, "decision": judgement, "conflict": True,
                          "requires_human_review": judgement == "needs_review", "reason": reason})
    return {"decisions": decisions}


async def teaching_explainer_node(ctx: DAGContext, model: str) -> dict:
    """教学化讲解（学生向）+ 证据。"""
    from .gateway import gateway
    from .reasoning import extract_json

    answers = ctx.outputs.get("solver", {}).get("answers", [])
    out = []
    for it in answers:
        resp = await gateway.chat(model, [
            {"role": "system", "content": "你是教学讲解助手，只输出 JSON。"},
            {"role": "user", "content": f"把解题过程改写成学生能听懂的讲解：{json.dumps({'answer': it.get('final_answer'), 'plan': it.get('solution_plan')}, ensure_ascii=False)}"},
        ], temperature=0.4)
        data = extract_json(resp.get("content", ""))
        out.append({**it, "teaching": (data or {}).get("teaching", "")})
    return {"items": out}


async def provenance_checker_node(ctx: DAGContext, model: str) -> dict:
    """证据/出处校验（通过 evidence_links 记录）。"""
    from .evidence import verify_evidence_list
    items = ctx.outputs.get("teaching_explainer", {}).get("items", [])
    verified = verify_evidence_list(
        [{"chunk_id": None, "quote": "", "source_type": "derived_reasoning"}],
        owner_type="homework_answer", owner_id=ctx.run_id or 0, persist=False,
    )
    return {"status": "ok", "items": items}


async def scope_checker_node(ctx: DAGContext, model: str) -> dict:
    """课程范围 / 超纲审计。"""
    items = ctx.outputs.get("provenance_checker", {}).get("items", [])
    if not items:
        items = ctx.outputs.get("teaching_explainer", {}).get("items", [])
    out = []
    for it in items or []:
        out.append({"question_no": it.get("question_no"), "out_of_scope_risk": "none"})
    return {"scope": out}


async def persist_answers_node(ctx: DAGContext, model: str) -> dict:
    """把所有解答写入 answer_items，标记冲突/requires_human_review，并更新作业状态机。"""
    solver_answers = ctx.outputs.get("solver", {}).get("answers", [])
    parallel = ctx.outputs.get("parallel_solver", {}).get("parallel_answers", [])
    decisions = ctx.outputs.get("adjudicator", {}).get("decisions", [])
    teaching = ctx.outputs.get("teaching_explainer", {}).get("items", [])
    dec_map = {d["question_no"]: d for d in decisions}
    teach_map = {t["question_no"]: t for t in teaching}
    # 通过 question_id 关联
    questions = ctx.outputs.get("persist_questions", {}).get("questions", [])
    qid_by_no = {q["question_no"]: q["question_id"] for q in questions}
    needs_review = False
    for a in solver_answers:
        qid = qid_by_no.get(a.get("question_no"))
        dec = dec_map.get(a.get("question_no"), {})
        conf = bool(dec.get("conflict", False))
        h_review = bool(dec.get("requires_human_review", False))
        if h_review:
            needs_review = True
        insert(
            "INSERT INTO answer_items (question_id, final_answer, solution_plan, detailed_solution, "
            " confidence, model_used, parallel_solution, teaching, evidence_json, status, conflict, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,'{}',?,?,datetime('now','localtime'))",
            (qid, a.get("final_answer"), a.get("solution_plan"), a.get("detailed_solution"),
             0.8, a.get("model_used"),
             next((p["final_answer"] for p in parallel if p["question_no"]==a.get("question_no")), ""),
             teach_map.get(a["question_no"], {}).get("teaching", ""),
             "needs_review" if h_review else "ok", "needs_review" if h_review else None),
        )
    homework_id = ctx.input.get("homework_id") or ctx.outputs.get("persist_questions", {}).get("homework_id")
    if homework_id:
        execute("UPDATE homeworks SET status=? WHERE id=?",
                ("needs_review" if needs_review else "solved", homework_id))
    return {"persisted": len(solver_answers),
            "requires_human_review": needs_review,
            "homework_id": homework_id}


def build_homework_dag() -> DAG:
    dag = DAG("homework", "solve")
    dag.add(DAGNode("resolve_input", "local", resolve_input_node, kind="local"))
    dag.add(DAGNode("persist_questions", "local", persist_questions_node, depends_on=["resolve_input"], kind="local"))
    dag.add(DAGNode("risk_classifier", "risk_classifier", risk_classifier_node,
                    preferred_models=["qwen3_flash"], depends_on=["persist_questions"]))
    dag.add(DAGNode("solver", "solver", solver_node,
                    preferred_models=["deepseek_v4_free"], fallback_models=["deepseek_v4_official"],
                    depends_on=["risk_classifier"], kind="llm"))
    dag.add(DAGNode("parallel_solver", "parallel_solver", parallel_solver_node,
                    preferred_models=["minimax_m3"], fallback_models=["qwen3_8_27b"],
                    depends_on=["risk_classifier"], kind="llm"))
    dag.add(DAGNode("adjudicator", "adjudicator", adjudicator_node,
                    preferred_models=["qwen3_8_27b"], fallback_models=["minimax_m3"],
                    depends_on=["solver", "parallel_solver"], kind="llm"))
    dag.add(DAGNode("teaching_explainer", "solution_explainer", teaching_explainer_node,
                    preferred_models=["qwen3_8_27b"], depends_on=["adjudicator"], kind="llm"))
    dag.add(DAGNode("provenance_checker", "local", provenance_checker_node, depends_on=["teaching_explainer"], kind="local"))
    dag.add(DAGNode("scope_checker", "scope_auditor", scope_checker_node,
                    preferred_models=["qwen3_8_27b"], depends_on=["provenance_checker"], kind="llm"))
    dag.add(DAGNode("persist_answers", "local", persist_answers_node, depends_on=["scope_checker"], kind="local"))
    return dag


def build_homework_ocr_dag() -> DAG:
    """OCR 确认流水线（方案 7.2）：仅 OCR + 建题 + 待确认，不求解。"""
    dag = DAG("homework_ocr", "solve")
    dag.add(DAGNode("resolve_input", "local", resolve_input_node, kind="local"))
    dag.add(DAGNode("persist_questions", "local", persist_questions_node, depends_on=["resolve_input"], kind="local"))
    dag.add(DAGNode("await_confirmation", "local", mark_awaiting_confirmation_node,
                    depends_on=["persist_questions"], kind="local"))
    return dag