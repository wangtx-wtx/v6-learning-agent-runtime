"""
作业流固定 DAG。

链路：
  vision_reader → splitter(题目切分)
  → risk_classifier
  → solver (Deepseek 免费 / 官方)
  → parallel_solver (MiniMax, 仅 high risk)
  → solution_explainer (Qwen 3.8-27B)
  → evidence_auditor
  → scope_auditor
"""
import base64
import json
import logging
import re
from pathlib import Path

from .dag import DAG, DAGContext, DAGNode, GatewayRetryableError
from .database import execute

logger = logging.getLogger(__name__)


async def homework_vision_reader(ctx: DAGContext, model_id: str) -> dict:
    """作业图片识别:真实调用多模态模型"""
    from .gateway import gateway
    from .reasoning import extract_json

    images = ctx.input.get("images", []) or []
    results: list[dict] = []
    total_in = total_out = 0
    for idx, img in enumerate(images):
        if not img:
            continue
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
            "请识别这张作业图片中的题目内容。\n"
            "输出要求:\n"
            "1. 原样转写文字(保留数学符号、下标、题号)\n"
            "2. 若包含插图,简要说明\n"
            "只输出 JSON: {\"text\": \"...\", \"confidence\": 0.85}"
        )
        messages = [
            {"role": "system", "content": "你是作业题目 OCR 识别助手,只输出 JSON。"},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]},
        ]
        resp = await gateway.chat(model_id, messages, temperature=0.2)
        total_in += resp.get("tokens_in", 0)
        total_out += resp.get("tokens_out", 0)
        data = extract_json(resp.get("content", "")) or {}
        text = (data.get("text") or data.get("transcription") or "").strip()
        if not text:
            raise GatewayRetryableError(f"vision_reader 返回空文本: image#{idx}")
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
    if not results:
        return {"results": [], "error": "无可识别图片"}
    return {"results": results, "tokens_in": total_in, "tokens_out": total_out}


async def homework_splitter(ctx: DAGContext, model_id: str) -> dict:
    """切题：按题目编号规则切分"""
    text = ctx.input.get("homework_text", "") or ""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    questions = []
    current = []
    num_pattern = re.compile(r"^[（(]?\s*[一二三四五六七八九十\d\.]+\s*[)）]?\s*[、．.]")
    for line in lines:
        if num_pattern.match(line):
            if current:
                questions.append("\n".join(current))
                current = []
        current.append(line)
    if current:
        questions.append("\n".join(current))
    for i, q in enumerate(questions, 1):
        execute(
            "INSERT INTO questions (homework_id, question_no, text) VALUES (?,?,?)",
            (ctx.input.get("homework_id"), i, q),
        )
    return {"questions": questions, "count": len(questions)}


async def homework_risk_classifier(ctx: DAGContext, model_id: str) -> dict:
    """风险分级"""
    questions = ctx.outputs.get("splitter", {}).get("questions", [])
    items = []
    for i, q in enumerate(questions, 1):
        risk = {"risk": "normal", "reason": []}
        if len(q) > 500:
            risk = {"risk": "high", "reason": ["长题"]}
        elif "证明" in q:
            risk = {"risk": "high", "reason": ["证明题"]}
        items.append({"index": i, "text": q, **risk})
    return {"risk_items": items}


async def homework_solver(ctx: DAGContext, model: str) -> dict:
    """解题：DeepSeek 微信免费优先"""
    risk_items = ctx.outputs.get("risk_classifier", {}).get("risk_items", [])
    if not risk_items:
        return {"answers": [], "note": "no risk items"}

    from .gateway import gateway
    from .reasoning import extract_json
    from .dag import SchemaValidationError

    results: list[dict] = []
    for item in risk_items:
        prompt = (
            "请解答以下问题并输出 JSON：\n"
            '{"final_answer": "...", "solution_plan": "...", "detailed_solution": "..."}\n'
            f"题目：\n{item['text']}"
        )
        # 网关失败 → GatewayRetryableError(由 gateway 内部抛)
        resp = await gateway.chat(model, [
            {"role": "system", "content": "你是专业解题助手。只输出 JSON。"},
            {"role": "user", "content": prompt},
        ], temperature=0.3)
        data = extract_json(resp["content"])
        if not data:
            raise SchemaValidationError(
                f"homework_solver JSON 解析失败 (q{item['index']}): {resp['content'][:200]}"
            )
        results.append({
            "question_no": item["index"],
            "final_answer": data.get("final_answer", ""),
            "solution_plan": data.get("solution_plan", ""),
            "detailed_solution": data.get("detailed_solution", ""),
            "confidence": 0.8,
            "model_used": model,
            "tokens_in": resp.get("tokens_in", 0),
            "tokens_out": resp.get("tokens_out", 0),
        })
    return {"answers": results}


async def homework_parallel_solver(ctx: DAGContext, model: str) -> dict:
    """高风险并行题：对 risk=high 的题目调 LLM 给出独立第二解，与 solver 对比。
    任一网关/JSON 失败 → 抛 GatewayRetryableError / SchemaValidationError 触发 DAGNode fallback"""
    from .gateway import gateway
    from .reasoning import extract_json
    from .dag import SchemaValidationError

    risk_items = ctx.outputs.get("risk_classifier", {}).get("risk_items", []) or []
    high_items = [it for it in risk_items if it.get("risk") == "high"]
    results: list[dict] = []
    total_in = total_out = 0
    for item in high_items:
        prompt = (
            "请用与主流解法**不同**的思路重新解答以下高难度题目,输出 JSON:\n"
            "{\"final_answer\": \"...\", \"solution_plan\": \"...\", "
            "\"detailed_content\": \"...\"}"
        )
        # 网关失败 → GatewayRetryableError(JSON 由 gateway 内部抛);JSON 解析失败 → SchemaValidationError
        resp = await gateway.chat(model, [
            {"role": "system", "content": "你是独立思路解题助手,只输出 JSON。"},
            {"role": "user", "content": f"{prompt}\n\n题目:\n{item.get('text','')}"},
        ], temperature=0.7)
        total_in += resp.get("tokens_in", 0)
        total_out += resp.get("tokens_out", 0)
        data = extract_json(resp.get("content", ""))
        if not data:
            raise SchemaValidationError(
                f"homework_parallel_solver JSON 解析失败 (q{item.get('index')}): "
                f"{resp.get('content','')[:200]}"
            )
        results.append({
            "question_no": item.get("index"),
            "final_answer": data.get("final_answer", ""),
            "solution_plan": data.get("solution_plan", ""),
            "detailed_content": data.get("detailed_content", ""),
            "confidence": 0.7,
            "model_used": model,
        })
    return {"parallel_answers": results, "tokens_in": total_in, "tokens_out": total_out}


async def homework_explainer(ctx: DAGContext, model: str) -> dict:
    """教学化改写：合并 solver + parallel_solver，调 LLM 做学生向讲解。
    任一网关/JSON 失败 → 抛 GatewayRetryableError / SchemaValidationError 触发 DAGNode fallback"""
    from .gateway import gateway
    from .reasoning import extract_json
    from .dag import SchemaValidationError

    solver_items = ctx.outputs.get("solver", {}).get("answers", []) or []
    parallel = ctx.outputs.get("parallel_solver", {}).get("parallel_answers", []) or []
    by_q: dict[int, dict] = {it.get("question_no"): it for it in solver_items}
    for pa in parallel:
        if pa.get("question_no") in by_q:
            by_q[pa["question_no"]]["parallel_solution"] = pa.get("final_answer", "")

    if not by_q:
        return {"reviewed_items": [], "tokens_in": 0, "tokens_out": 0}

    reviewed: list[dict] = []
    total_in = total_out = 0
    for qno in sorted(by_q):
        item = by_q[qno]
        prompt = (
            "请把以下解题过程改写为学生能听懂的教学讲解:\n"
            "1. 先讲思路(为什么这样想)\n"
            "2. 再按步骤展开\n"
            "3. 补充易错点提示\n"
            "4. 给出至少 1 条 evidence: {\"chunk_id\": int, \"quote\": \"原文摘录\", \"locator\": \"来源\"}\n"
            "只输出 JSON: {\"teaching\": \"...\", \"evidence\": [...]}"
        )
        resp = await gateway.chat(model, [
            {"role": "system", "content": "你是教学讲解助手,只输出 JSON。"},
            {"role": "user", "content": f"{prompt}\n\n原解题:\n{json.dumps(item, ensure_ascii=False)[:2000]}"},
        ], temperature=0.4)
        total_in += resp.get("tokens_in", 0)
        total_out += resp.get("tokens_out", 0)
        data = extract_json(resp.get("content", ""))
        if not data:
            raise SchemaValidationError(
                f"homework_explainer JSON 解析失败 (q{qno}): {resp.get('content','')[:200]}"
            )
        reviewed.append({
            **item,
            "teaching": data.get("teaching", ""),
            "evidence": data.get("evidence", []),
        })
    return {"reviewed_items": reviewed, "tokens_in": total_in, "tokens_out": total_out}


async def homework_evidence(ctx: DAGContext, model: str) -> dict:
    """证据硬 Gate:每条 evidence 的 chunk_id 必须真实存在，quote 必须出现在 chunk.text 中"""
    from .database import query

    items = ctx.outputs.get("explainer", {}).get("reviewed_items", []) or []
    checked: list[dict] = []
    for item in items:
        evidence_list = item.get("evidence", []) or []
        verified: list[dict] = []
        for ev in evidence_list:
            chunk_id = ev.get("chunk_id")
            quote = (ev.get("quote") or "").strip()
            row = query("SELECT id, text FROM source_chunks WHERE id=?", (chunk_id,), one=True)
            if not row:
                verified.append({**ev, "verified": False, "reason": "chunk_id not found"})
                continue
            chunk_text = (row.get("text") or "")
            if quote and quote in chunk_text:
                verified.append({**ev, "verified": True})
            else:
                verified.append({**ev, "verified": False, "reason": "quote not in chunk.text"})
        ok_count = sum(1 for v in verified if v.get("verified"))
        if not verified or ok_count == 0:
            item["evidence_status"] = "blocked"
            item["block_reason"] = "缺少通过校验的证据,无法确认答案来源"
        else:
            item["evidence_status"] = "ok"
        item["evidence"] = verified
        checked.append(item)
    overall = "ok" if all(it.get("evidence_status") == "ok" for it in checked) else "partial"
    return {"evidence_status": overall, "items": checked}


async def homework_scope(ctx: DAGContext, model: str) -> dict:
    """超纲审计：LLM 判断每题是否超出课程范围"""
    from .gateway import gateway
    from .reasoning import extract_json

    items = ctx.outputs.get("evidence_checker", {}).get("items", []) or []
    scope_text = (ctx.input.get("scope_text") or "")[:2000]
    out: list[dict] = []
    for item in items:
        if item.get("evidence_status") == "blocked":
            # blocked 的题目直接视为可能超纲
            out.append({
                "question_no": item.get("question_no"),
                "reason": "缺少 evidence,无法核实是否在课程范围内",
            })
            continue
        if not scope_text:
            continue  # 无 syllabus 时不判断
        prompt = (
            "请判断以下题目是否超出【课程范围】。\n"
            "只输出 JSON: {\"out_of_scope\": bool, \"reason\": \"...\"}"
        )
        try:
            resp = await gateway.chat(model, [
                {"role": "system", "content": "你是教学审计员,只输出 JSON。"},
                {"role": "user", "content": f"{prompt}\n\n【课程范围】\n{scope_text}\n\n【题目】\n{item.get('text','')}"},
            ], temperature=0.2)
            data = extract_json(resp.get("content", "")) or {}
        except Exception:
            continue
        if data.get("out_of_scope"):
            out.append({
                "question_no": item.get("question_no"),
                "reason": data.get("reason", ""),
            })
    return {"out_of_scope": out}


def build_homework_dag() -> DAG:
    dag = DAG("homework", "solve")
    dag.add(DAGNode(
        "vision_reader", "vision_reader", homework_vision_reader,
        preferred_models=["qwen3_vl"],
        fallback_models=[],
        depends_on=[],
    ))
    dag.add(DAGNode(
        "splitter", "transcriber_splitter", homework_splitter,
        preferred_models=["qwen3_flash"],
        fallback_models=["deepseek_v4_free"],
        depends_on=["vision_reader"],
    ))
    dag.add(DAGNode(
        "risk_classifier", "risk_classifier", homework_risk_classifier,
        preferred_models=["qwen3_flash"],
        fallback_models=["deepseek_v4_free"],
        depends_on=["splitter"],
    ))
    dag.add(DAGNode(
        "solver", "solver", homework_solver,
        preferred_models=["deepseek_v4_free"],
        fallback_models=["deepseek_v4_official"],
        depends_on=["risk_classifier"],
    ))
    dag.add(DAGNode(
        "parallel_solver", "parallel_solver", homework_parallel_solver,
        preferred_models=["minimax_m3"],
        fallback_models=["qwen3_8_27b"],
        depends_on=["risk_classifier"],
    ))
    dag.add(DAGNode(
        "explainer", "solution_explainer", homework_explainer,
        preferred_models=["qwen3_8_27b"],
        fallback_models=["deepseek_v4_official"],
        depends_on=["solver"],
    ))
    dag.add(DAGNode(
        "evidence_checker", "evidence_auditor", homework_evidence,
        preferred_models=["qwen3_8_27b"],
        fallback_models=[],
        depends_on=["explainer"],
    ))
    dag.add(DAGNode(
        "scope_checker", "scope_auditor", homework_scope,
        preferred_models=["qwen3_8_27b"],
        fallback_models=[],
        depends_on=["evidence_checker"],
    ))
    return dag
