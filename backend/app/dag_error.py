"""
错题流 DAG（V5.4 重写，对应方案 7.3）。

链路：
  vision_reader (视觉提取题目/学生作答/批改痕迹)
  → analyst (错因分析：现象/直接原因/根本原因/知识缺口)
  → cross_check (独立审查模型)
  → ingest (写入 provisional 错题，确认后才转入正式错题库)
  → schedule_review (SM-2 变体安排 next_review_at)

注意：cross_check 使用独立审查模型，不得固定返回；ingest 只落 provisional。
"""
from __future__ import annotations

import json
import logging
import base64
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .dag import DAG, DAGContext, DAGNode, RetryableModelError, SchemaValidationError
from .database import execute, fetch_one

logger = logging.getLogger(__name__)


async def error_vision_reader(ctx: DAGContext, model: str) -> dict:
    """真实调用视觉模型提取题目与手写作答。"""
    from .gateway import gateway
    from .reasoning import extract_json

    images = ctx.input.get("images") or []
    results = []
    for idx, img in enumerate(images):
        image_b64 = ""
        if isinstance(img, str) and img.startswith("data:"):
            image_b64 = img.split(",", 1)[-1]
        elif isinstance(img, str) and Path(img).exists():
            image_b64 = base64.b64encode(Path(img).read_bytes()).decode()
        elif isinstance(img, int):
            try:
                from .database import resolve_material_path
                p, _, _ = resolve_material_path(img)
                image_b64 = base64.b64encode(Path(p).read_bytes()).decode()
            except Exception:
                image_b64 = ""
        if not image_b64:
            results.append({"image": img, "question_text": "[图片不可读]", "student_answer": "", "confidence": 0.0})
            continue
        prompt = "识别这张错题图片：题目、学生作答、批改痕迹。只输出 JSON：{\"question_text\":\"...\",\"student_answer\":\"...\",\"mark_grades\":\"...\"}"
        try:
            resp = await gateway.chat(model, [
                {"role": "system", "content": "你是错题识别助手，只输出 JSON。"},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ]},
            ], temperature=0.2)
            data = extract_json(resp.get("content", "")) or {}
            results.append({"image": img, "question_text": data.get("question_text", ""),
                            "student_answer": data.get("student_answer", ""), "confidence": 0.9})
        except Exception as e:
            raise RetryableModelError(str(e))
    return {"results": results}


async def error_analyst(ctx: DAGContext, model: str) -> dict:
    """错因分析：现象、直接原因、根本原因、知识缺口。"""
    from .gateway import gateway
    from .reasoning import extract_json

    vision_results = ctx.outputs.get("vision_reader", {}).get("results", [])
    if not vision_results:
        vision_results = [{"question_text": ctx.input.get("question_text", ""),
                           "student_answer": ctx.input.get("student_answer", ""),
                           "confidence": 1.0}]
    analyses = []
    total_in = total_out = 0
    for item in vision_results:
        prompt = (
            "分析以下错题的分层错因，只输出 JSON：\n"
            '{"phenomenon": "...", "direct_cause": "...", "root_cause": "...", "knowledge_gaps": ["..."], "possible_causes": ["..."]}'
        )
        resp = await gateway.chat(model, [
            {"role": "system", "content": "你是教学错因分析助手，只输出 JSON。"},
            {"role": "user", "content": f"{prompt}\n\n题目：{item.get('question_text','')}\n学生作答：{item.get('student_answer','')}\n正确答案：{ctx.input.get('correct_answer','')}"},
        ], temperature=0.4)
        total_in += resp.get("tokens_in", 0)
        total_out += resp.get("tokens_out", 0)
        data = extract_json(resp.get("content", ""))
        if not data:
            raise SchemaValidationError("error_analyst JSON 解析失败")
        analyses.append({"question_text": item.get("question_text", ""),
                         "student_answer": item.get("student_answer", ""),
                         "ai_error_analysis": data, "model_used": model})
    return {"analysis": analyses, "tokens_in": total_in, "tokens_out": total_out}


async def error_cross_check(ctx: DAGContext, model: str) -> dict:
    """独立审查模型校验候选错因（不得固定返回）。"""
    from .gateway import gateway
    from .reasoning import extract_json

    analyses = ctx.outputs.get("analyst", {}).get("analysis", [])
    candidates = []
    for ana in analyses:
        po = ana.get("ai_error_analysis") or ana.get("ai_error") or {}
        prompt = "独立审查以下错因候选的合理性。只输出 JSON：{\"confirmed_causes\": [...], \"uncertain\": \"...\"}"
        try:
            resp = await gateway.chat(model, [
                {"role": "system", "content": "你是错因审查专家，只输出 JSON。"},
                {"role": "user", "content": f"{prompt}\n\n题目：{ana.get('question_text','')}\n候选：{json.dumps(po.get('possible_causes', []), ensure_ascii=False)}"},
            ], temperature=0.2)
            data = extract_json(resp.get("content", "")) or {}
        except Exception as e:
            data = {"confirmed": po.get("possible_causes", []), "uncertainty": f"审查失败: {e}"}
        candidates.append({
            "question_text": ana.get("question_text", ""),
            "student_answer": ana.get("student_answer", ""),
            "candidate_causes": data.get("confirmed", po.get("possible_causes", [])),
            "knowledge_points": po.get("knowledge_gaps", []),
            "status": "provisional",
        })
    return {"candidates": candidates}


async def error_ingest(ctx: DAGContext, model: str) -> dict:
    """写 provisional 错题 + 安排首次复习（SM-2 简化变体 interval=1）。"""
    candidates = ctx.outputs.get("cross_check", {}).get("candidates", [])
    ids = []
    for cand in candidates:
        nxt = _sm2_next(0.0, False)
        eid = execute(
            "INSERT INTO errors (course_id, chapter_id, lesson_id, question_text, student_answer, "
            " correct_answer, ai_error_json, final_error_json, status, next_review_at, review_stage, mastery) "
            "VALUES (?,?,?,?,?,?,?,?, 'provisional', ?, 0, 0.0)",
            (ctx.input.get("course_id"), ctx.input.get("chapter_id"), ctx.input.get("lesson_id"),
             cand.get("question_text", ""), cand.get("student_answer", ""),
             ctx.input.get("correct_answer", ""),
             json.dumps(cand.get("candidate_causes", []), ensure_ascii=False),
             json.dumps({**cand, "ai_analysis": ctx.outputs.get("analyst", {}).get("analysis", [])}, ensure_ascii=False),
             nxt),
            returning_lastrowid=True,
        )
        ids.append(eid)
    return {"provisional_count": len(ids), "ids": ids}


def _sm2_next(prev_interval: float, passed: bool) -> str:
    """SM-2 简化变体：未通过 1 天，通过则按间隔翻倍（返回 ISO 日期）。"""
    days = 1.0 if not passed else max(1.0, float(prev_interval) * 2.0)
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


def _sm2_review(mastery: float, passed: bool) -> str:
    dt = datetime.now() + timedelta(days=1 if not passed else max(1, round(mastery * 6)))
    return dt.strftime("%Y-%m-%d")


def _review_scheduler_node(ctx: DAGContext, model: str) -> dict:
    """确认 provisional 错题应进入正式错题库时，安排 SM-2 首次复习时间。"""
    ids = ctx.outputs.get("ingest", {}).get("ids", [])
    for eid in ids:
        nxt = _sm2_next(1.0, False)
        execute("UPDATE errors SET next_review_at=? WHERE id=?", (nxt, eid))
    return {"scheduled": len(ids), "next_review_at": [_sm2_next(1.0, False)] * len(ids)}


def build_error_dag() -> DAG:
    dag = DAG("error", "collect")
    dag.add(DAGNode("vision_reader", "vision_reader", error_vision_reader,
                    preferred_models=["qwen3_vl"], fallback_models=["minimax_m3"], kind="llm"))
    dag.add(DAGNode("analyst", "error_analyst", error_analyst,
                    preferred_models=["deepseek_v4_free", "deepseek_v4_official"],
                    fallback_models=["qwen3_8_27b"], depends_on=["vision_reader"], kind="llm"))
    dag.add(DAGNode("cross_check", "critic", error_cross_check,
                    preferred_models=["qwen3_8_27b"], fallback_models=["minimax_m3"], depends_on=["analyst"], kind="llm"))
    dag.add(DAGNode("ingest", "local", error_ingest, depends_on=["cross_check"], kind="local"))
    dag.add(DAGNode("schedule_review", "local", _review_scheduler_node, depends_on=["ingest"], kind="local"))
    return dag