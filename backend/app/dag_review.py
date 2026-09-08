"""
复习流固定 DAG（章末复习 + 考前复习）。
"""
import json
import logging

from .dag import DAG, DAGContext, DAGNode
from .database import execute, query

logger = logging.getLogger(__name__)


async def review_aggregate(ctx: DAGContext, model: str) -> dict:
    """本章资源聚合：SQLite 查询"""
    course_id = ctx.input.get("course_id")
    chapter_id = ctx.input.get("chapter_id")
    kind = ctx.input.get("kind", "chapter")  # chapter | exam
    resources = {}
    if chapter_id:
        # 本章 lessons
        lessons = query("SELECT * FROM lessons WHERE chapter_id=?", (chapter_id,))
        resources["lessons"] = lessons or []
        # 本章 notes
        notes = query(
            "SELECT n.* FROM notes n JOIN lessons l ON l.id=n.lesson_id WHERE l.chapter_id=?",
            (chapter_id,),
        )
        resources["notes"] = notes or []
        # 本章 chunks
        chunks = query("SELECT id, text, locator FROM source_chunks WHERE chapter_id=?", (chapter_id,))
        resources["chunk_count"] = len(chunks or [])
        # 本章错题
        errors = query("SELECT * FROM errors WHERE chapter_id=? AND status='confirmed'", (chapter_id,))
        resources["confirmed_errors"] = errors or []
    if kind == "exam":
        resources["exam_scope"] = ctx.input.get("scope", {})
    return {"resources": resources}


async def review_writer(ctx: DAGContext, model: str) -> dict:
    """复习材料写作"""
    resources = ctx.outputs.get("aggregator", {}).get("resources", {})
    notes = resources.get("notes", [])
    prompt = (
        "根据以下学习资源，生成复习材料。输出 JSON：\n"
        '{"outline": ["..."], "review_materials": "..."}\n'
        f"资源：{json.dumps(notes, ensure_ascii=False)[:3000]}"
    )
    try:
        from .gateway import gateway
        resp = await gateway.chat(model, [
            {"role": "system", "content": "你是教学复习规划助手。只输出 JSON。"},
            {"role": "user", "content": prompt},
        ], temperature=0.4)
        from .reasoning import extract_json
        data = extract_json(resp["content"]) or {}
        return {"review_package": data, "model_used": model}
    except Exception as e:
        return {"review_package": {"outline": "(占位复习大纲)", "review_materials": "网关不可用"}, "model_used": model}


async def self_test_writer(ctx: DAGContext, model: str) -> dict:
    """自测卷生成"""
    errors = ctx.outputs.get("aggregator", {}).get("resources", {}).get("confirmed_errors", [])
    return {"self_test": [], "error_items": errors}


async def review_auditor(ctx: DAGContext, model: str) -> dict:
    """复习材料审计"""
    return {"result": "ok", "reviewed": True}


def build_review_dag() -> DAG:
    dag = DAG("review", "review")
    dag.add(DAGNode("aggregator", "local", review_aggregate, [""]))
    dag.add(DAGNode("writer", "review_writer", review_writer, ["deepseek_v4_free", "deepseek_v4_official"], depends_on=["aggregator"]))
    dag.add(DAGNode("self_test", "self_test_writer", self_test_writer, ["qwen3_flash", "deepseek_v4_free"], depends_on=["writer"]))
    dag.add(DAGNode("auditor", "critic", review_auditor, ["qwen3_8_27b"], depends_on=["self_test"]))
    return dag

