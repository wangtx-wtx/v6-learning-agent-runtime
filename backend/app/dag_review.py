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
    from .learning_engine.learning_loop import mastery_snapshot
    mastery = mastery_snapshot(course_id=course_id, chapter_id=chapter_id)
    resources = {"notes": notes, "confirmed_errors": errors, "kind": kind,
                 "knowledge_mastery": mastery,
                 "review_priority": [m["stable_key"] for m in mastery if float(m.get("mastery") or .5) < .7]}
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
    from .integrations.prompts import render_prompt
    from .integrations.schemas import ReviewWriterOut, parse_model_output
    from .dag import RetryableModelError

    notes_txt = "\n".join(f"- {n.get('title','')}: {(n.get('body') or '')[:400]}" for n in notes[:10])
    mastery = resources.get("knowledge_mastery") or []
    err_txt = "\n".join(f"- {e.get('question_text','')[:200]}" for e in errors[:20])
    mastery_txt = "\n".join(
        f"- {m.get('topic')}: mastery={float(m.get('mastery') or .5):.2f}; {m.get('explanation','')}"
        for m in mastery[:30])
    p = render_prompt("review/writer", "v1", notes_text=notes_txt, errors_text=err_txt)
    try:
        resp = await gateway.chat(model, [
            {"role": "system", "content": p["text"]},
            {"role": "user", "content": f"笔记：\n{notes_txt}\n\n错题：\n{err_txt}\n\n掌握度（由低到高）：\n{mastery_txt}"},
        ], contract="review/writer", temperature=0.4)
        data = parse_model_output(ReviewWriterOut, resp.get("content", ""), "review_writer")
        return {"review_package": data, "insufficient_data": False,
                "prompt_checksum": p["checksum"],
                "tokens_in": resp.get("tokens_in", 0), "tokens_out": resp.get("tokens_out", 0)}
    except Exception as e:
        raise RetryableModelError(f"review_writer 失败: {e}")


async def _self_test(ctx: DAGContext, model: str) -> dict:
    """生成带答案但默认隐藏答案的自测题（prompt 外置 + schema 绑定）。"""
    from .gateway import gateway
    from .integrations.prompts import render_prompt
    from .integrations.schemas import SelfTestOut, parse_model_output

    resources = ctx.outputs.get("aggregator", {}).get("resources", {})
    errors = resources.get("confirmed_errors", [])
    p = render_prompt("review/self_test", "v1",
                      errors_json=json.dumps([e.get("question_text") for e in errors[:10]], ensure_ascii=False))
    try:
        resp = await gateway.chat(model, [
            {"role": "system", "content": p["text"]},
            {"role": "user", "content": f"错题：\n{json.dumps([e.get('question_text') for e in errors[:10]], ensure_ascii=False)}"},
        ], contract="review/self_test", temperature=0.5)
        data = parse_model_output(SelfTestOut, resp.get("content", ""), "self_test")
        return {"self_test": data.get("questions", []), "prompt_checksum": p["checksum"],
                "tokens_in": resp.get("tokens_in", 0),
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
    # 稳定题号：LLM 可能不返回 question_no，落库前统一补齐（方案 5.3）
    for i, q in enumerate(self_test, 1):
        if isinstance(q, dict) and not q.get("question_no"):
            q["question_no"] = str(i)
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


async def _render_document_node(ctx: DAGContext, model: str) -> dict:
    from .document_artifacts import artifact_public, normalize_document, render_artifact
    persisted = ctx.outputs.get("persist_review", {})
    if persisted.get("status") == "insufficient_data":
        return {"artifact": None, "status": "skipped", "reason": "insufficient_data"}
    package = ctx.outputs.get("writer", {}).get("review_package", {}) or {}
    doc = normalize_document(package.get("document"), title="复习讲义", body=package.get("materials", ""),
                             outline=package.get("outline", []), kind="review_handout")
    artifact = render_artifact(owner_type="review", owner_id=persisted["review_id"], document=doc)
    return {"artifact": artifact_public(artifact)}


# 复习作答（grade）不再是工作流节点：用户作答走独立接口
# POST /api/reviews/{id}/attempts + POST /api/reviews/{id}/complete（方案 5.3/5.4）。


def build_review_dag() -> DAG:
    dag = DAG("review", "review")
    dag.add(DAGNode("aggregator", "local", review_aggregator, kind="local"))
    dag.add(DAGNode("writer", "review_writer", _writer,
                    preferred_models=["deepseek_v4_free", "glm_flash"],
                    depends_on=["aggregator"], kind="llm"))
    dag.add(DAGNode("self_test", "self_test_writer", _self_test,
                    preferred_models=["qwen3_flash", "deepseek_v4_free"],
                    depends_on=["writer"], kind="llm"))
    dag.add(DAGNode("persist_review", "local", _persist_node, depends_on=["self_test"], kind="local"))
    dag.add(DAGNode("render_document", "local", _render_document_node, depends_on=["persist_review"], kind="local"))
    return dag
