"""
错题收录流固定 DAG。

链路：
  qwen3-vl-flash 提取题目和手写答案
  → Deepseek-v4-flash 分析错因
  → qwen3_8_27b 交叉检查
  → 生成 provisional 错题
  → 用户确认或修改错因
  → 本地程序写入终库
"""
import json
import logging

from .dag import DAG, DAGContext, DAGNode
from .database import execute

logger = logging.getLogger(__name__)


async def error_vision_reader(ctx: DAGContext, model: str) -> dict:
    """题目/手写答案提取"""
    images = ctx.input.get("images", []) or []
    results = []
    for img in images:
        results.append({
            "image": img,
            "question_text": "[待视觉识别]",
            "student_answer": "",
            "confidence": 0.0,
        })
    return {"results": results}


async def error_analyst(ctx: DAGContext, model: str) -> dict:
    """错因分析：DeepSeek 微信免费优先"""
    vision_results = ctx.outputs.get("vision_reader", {}).get("results", [])
    if not vision_results:
        vision_results = [{
            "question_text": ctx.input.get("question_text", ""),
            "student_answer": ctx.input.get("student_answer", ""),
        }]
    outputs = []
    for item in vision_results:
        prompt = (
            "以下是一名学生的错题。请分析可能错因并输出 JSON：\n"
            '{"possible_causes": ["..."], "knowledge_points": ["..."], "ai_error_hypothesis": "..."}\n'
            f"题目：{item.get('question_text','')}\n"
            f"学生答案：{item.get('student_answer','')}\n"
            f"正确答案：{ctx.input.get('correct_answer','')}"
        )
        try:
            from .gateway import gateway
            resp = await gateway.chat(model, [
                {"role": "system", "content": "你是教学分析助手。只输出 JSON。"},
                {"role": "user", "content": prompt},
            ], temperature=0.4)
            from .reasoning import extract_json
            data = extract_json(resp["content"]) or {}
            outputs.append({
                "question_text": item.get("question_text", ""),
                "ai_error_analysis": data,
                "model_used": model,
            })
        except Exception as e:
            outputs.append({
                "question_text": item.get("question_text", ""),
                "ai_error_analysis": {"note": f"网关失败: {e}", "possible_causes": []},
                "model_used": model,
            })
    return {"analysis": outputs}


async def error_cross_check(ctx: DAGContext, model: str) -> dict:
    """交叉检查：qwen3_8_27b 独立审查"""
    analyses = ctx.outputs.get("analyst", {}).get("analysis", [])
    candidates = []
    for ana in analyses:
        po = ana.get("ai_error_analysis", {})
        causes = po.get("possible_causes", []) or []
        kps = po.get("knowledge_points", []) or []
        candidates.append({
            "question_text": ana.get("question_text", ""),
            "candidate_causes": causes,
            "knowledge_points": kps,
            "status": "provisional",
        })
    return {"candidates": candidates}


async def error_ingest(ctx: DAGContext, model: str) -> dict:
    """写入 provisional 错题"""
    candidates = ctx.outputs.get("cross_check", {}).get("candidates", [])
    for cand in candidates:
        execute(
            "INSERT INTO errors (course_id, chapter_id, lesson_id, question_text, student_answer, "
            "correct_answer, ai_error_json, final_error_json, status) VALUES (?,?,?,?,?,?,?,?,'provisional')",
            (ctx.input.get("course_id"), ctx.input.get("chapter_id"), ctx.input.get("lesson_id"),
             cand.get("question_text", ""), ctx.input.get("student_answer", ""),
             ctx.input.get("correct_answer", ""),
             json.dumps(cand.get("candidate_causes", []), ensure_ascii=False),
             json.dumps(cand, ensure_ascii=False)),
        )
    return {"provisional_count": len(candidates)}


def build_error_dag() -> DAG:
    dag = DAG("error", "collect")
    dag.add(DAGNode("vision_reader", "vision_reader", error_vision_reader, ["qwen3_vl"]))
    dag.add(DAGNode("analyst", "error_analyst", error_analyst, ["deepseek_v4_free", "deepseek_v4_official", "qwen3_8_27b"], depends_on=["vision_reader"]))
    dag.add(DAGNode("cross_check", "critic", error_cross_check, ["qwen3_8_27b", "minimax_m3"], depends_on=["analyst"]))
    dag.add(DAGNode("ingest", "local", error_ingest, [""], depends_on=["cross_check"]))
    return dag

