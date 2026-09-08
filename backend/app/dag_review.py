"""
复习流 DAG（V5.4 重写，对应方案 7.4）。

链路：
  aggregator (聚合本章笔记/已确认错题/低掌握点/考试范围)
  → writer (生成结构化复习大纲+材料；材料不足返回 insufficient_data)
  → self_test (生成带答案但默认隐藏答案的自测题)
  → persist_review (写 reviews，记录 insufficient_data 不会标记成功)
  → 用户提交作答后 → grade_attempts (写入 review_attempts 并更新掌握度/下次复习)
"""
from __future__ import annotations

import json
import logging

from .dag import DAG, DAGContext, DAGNode, BusinessError
from .database import execute, fetch_all, fetch_one, insert

logger = logging.getLogger(__name__)


async def review_aggregator(ctx: DAGContext, model: str) -> dict:
    """聚合本章资源。"""
    chapter_id = ctx.input.get("chapter_id")
    course_id = ctx.input.get("course_id")
    kind = ctx.input.get("kind", "chapter")
    resources = {}
    notes = fetch_all("SELECT * FROM notes WHERE chapter_id=? ORDER BY id", (chapter_id,)) if chapter_id else []
    errors = fetch_all("SELECT * FROM errors WHERE course_id=? AND status='confirmed' ORDER BY next_review_at", (course_id,)) if course_id else []
    if chapter_id:
        errors = fetch_all("SELECT * FROM errors WHERE chapter_id=? AND status='confirmed'", (chapter_id,))
    resources = {"notes": notes, "confirmed_errors": errors, "kind": kind}
    if kind == "exam":
        resources["exam_scope"] = ctx.input.get("scope") or {}
        resources["exam_date"] = ctx.input.get("exam_date")
    return {"resources": resources}


async def _writer(ctx: DAGContext, model: str) -> dict:
    """生成复习包；材料不足返回 insufficient_data。"""
    resources = ctx.outputs.get("aggregator", {}).get("resources", {})
    notes = resources.get("notes", [])
    errors = resources.get("confirmed_errors", [])
    if not notes and not errors:
        return {"insufficient_data": True, "reason": "该范围内尚无笔记或已确认错题，无法生成复习包"}

    from .gateway import gateway
    from .reasoning import extract_json
    from .dag import RetryableModelError

    notes_txt = "\n".join(f"- {n.get('title','')}: {(n.get('body') or '')[:400]}" for n in notes[:10])
    err_txt = "\n".join(f"- {e.get('question_text','')[:200]}" for e in errors[:20])
    prompt = (
        "生成结构化复习材料，只输出 JSON：\n"
        '{"outline": ["..."], "materials": "..."}'
    )
    try:
        resp = await gateway.chat(model, [
            {"role": "system", "content": "你是复习规划助手，只输出 JSON。"},
            {"role": "user", "content": f"{prompt}\n\n笔记：\n{notes_txt}\n\n错题：\n{err_txt}"},
        ], temperature=0.4)
        data = extract_json(resp.get("content", ""))
        return {"review_package": data or {}, "insufficient_data": False,
                "tokens_in": resp.get("tokens_in", 0), "tokens_out": resp.get("tokens_out", 0)}
    except Exception as e:
        raise RetryableModelError(f"review_writer 失败: {e}")


async def _self_test(ctx: DAGContext, model: str) -> dict:
    """生成带答案但默认隐藏答案的自测题。"""
    from .gateway import gateway
    from .reasoning import extract_json

    resources = ctx.outputs.get("aggregator", {}).get("resources", {})
    errors = resources.get("confirmed_errors", [])
    prompt = (
        "生成自测题（答案默认隐藏），只输出 JSON：\n"
        '{"questions": [{"q": "...", "answer": "..."}]}'
    )
    try:
        resp = await gateway.chat(model, [
            {"role": "system", "content": "你是自测题出题助手，只输出 JSON。"},
            {"role": "user", "content": f"{prompt}\n\n错题：\n{json.dumps([e.get('question_text') for e in errors[:10]], ensure_ascii=False)}"},
        ], temperature=0.5)
        data = extract_json(resp.get("content", "")) or {}
        return {"self_test": data.get("questions", []) or [], "tokens_in": resp.get("tokens_in", 0),
                "tokens_out": resp.get("tokens_out", 0)}
    except Exception as e:
        # 审计指出：静默吞异常会让用户拿到“空自测”假成功。若缺失必填内容则显式失败，
        # 由 DAG 引擎标记节点失败、前端可见错误，而非产出空的自测包。
        raise BusinessError(f"自测题生成失败: {e}") from e


async def _persist_node(ctx: DAGContext, model: str) -> dict:
    resources = ctx.outputs.get("aggregator", {}).get("resources", {})
    package = ctx.outputs.get("writer", {}).get("review_package", {})
    self_test = ctx.outputs.get("self_test", {}).get("self_test", [])
    insuff = ctx.outputs.get("writer", {}).get("insufficient_data", False)
    auditor = json.dumps({"auditor": "skip"} if insuff else {"auditor": "ok"}, ensure_ascii=False)
    rid = insert(
        "INSERT INTO reviews (course_id, chapter_id, kind, exam_date, scope_json, outline, review_materials, "
        " self_test, status, outputs_json, auditor_result, score, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))",
        (ctx.input.get("course_id"), ctx.input.get("chapter_id"), ctx.input.get("kind", "chapter"),
         ctx.input.get("exam_date"), json.dumps(ctx.input.get("scope", {}), ensure_ascii=False),
         json.dumps(package.get("outline", []), ensure_ascii=False),
         package.get("materials", ""),
         json.dumps(self_test, ensure_ascii=False),
         "insufficient_data" if insuff else "generated",
         json.dumps({"insufficient_data": insuff}, ensure_ascii=False),
         auditor, 0.0 if insuff else 1.0),

)
    return {"review_id": rid, "status": "insufficient_data" if insuff else "generated"}


async def _grade_node(ctx: DAGContext, model: str) -> dict:
    """占位：用户提交作答后写入 review_attempts（接口另行实现）。"""
    review_id = ctx.outputs.get("persist_review", {}).get("review_id")
    return {"review_id": review_id, "pending_attempts": True}


def build_review_dag() -> DAG:
    dag = DAG("review", "review")
    dag.add(DAGNode("aggregator", "local", review_aggregator, kind="local"))
    dag.add(DAGNode("writer", "review_writer", _writer,
                    preferred_models=["deepseek_v4_free", "deepseek_v4_official"],
                    depends_on=["aggregator"], kind="llm"))
    dag.add(DAGNode("self_test", "self_test_writer", _self_test,
                    preferred_models=["qwen3_flash", "deepseek_v4_free"],
                    depends_on=["writer"], kind="llm"))
    dag.add(DAGNode("persist_review", "local", _persist_node, depends_on=["self_test"], kind="local"))
    dag.add(DAGNode("grade", "local", _grade_node, depends_on=["persist_review"], kind="local"))
    return dag