"""V6 explainable learning-feedback loop."""
from __future__ import annotations

import json
import re
from typing import Any

from .. import database as db


def _terms(text: str) -> set[str]:
    text = (text or "").lower()
    words = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    words.update(chinese[i:i + 2] for i in range(max(0, len(chinese) - 1)))
    return words


def sync_knowledge_units(run_id: int) -> list[int]:
    """Project the latest LessonUnderstanding into long-lived scoped KUs."""
    lesson = db.fetch_one("SELECT structured_json,domain_id,lesson_id FROM lesson_understandings WHERE run_id=?",
                          (int(run_id),))
    run = db.fetch_one("SELECT course_id,chapter_id,lesson_id FROM workflow_runs WHERE id=?",
                       (int(run_id),)) or {}
    if not lesson:
        return []
    try:
        structured = json.loads(lesson.get("structured_json") or "{}")
    except Exception:
        return []
    ids = []
    with db.transaction() as conn:
        for index, unit in enumerate(structured.get("knowledge_units") or [], 1):
            key = str(unit.get("id") or f"KU-{index:04d}")
            scope = (run.get("course_id"), run.get("chapter_id"),
                     run.get("lesson_id") or lesson.get("lesson_id"))
            conn.execute(
                "INSERT INTO knowledge_units (course_id,chapter_id,lesson_id,stable_key,topic,kind,summary,emphasis) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET topic=excluded.topic,kind=excluded.kind,"
                "summary=excluded.summary,emphasis=excluded.emphasis,updated_at=datetime('now','localtime')",
                scope + (key, unit.get("topic") or "", unit.get("kind") or "concept",
                         unit.get("summary") or "", float(unit.get("teacher_emphasis") or 0)))
            row = conn.execute("SELECT id FROM knowledge_units WHERE COALESCE(course_id,-1)=COALESCE(?,-1) "
                               "AND COALESCE(chapter_id,-1)=COALESCE(?,-1) AND COALESCE(lesson_id,-1)=COALESCE(?,-1) "
                               "AND stable_key=?", scope + (key,)).fetchone()
            ku_id = int(row["id"]); ids.append(ku_id)
            conn.execute("DELETE FROM knowledge_unit_sources WHERE knowledge_unit_id=?", (ku_id,))
            for sid in dict.fromkeys(unit.get("source_refs") or []):
                span = conn.execute("SELECT id FROM source_spans WHERE domain_id=? AND source_id=?",
                                    (lesson["domain_id"], str(sid))).fetchone()
                if span:
                    conn.execute("INSERT OR IGNORE INTO knowledge_unit_sources "
                                 "(knowledge_unit_id,source_span_id,relation) VALUES (?,?,'supports')",
                                 (ku_id, int(span["id"])))
    return ids


def _candidate_units(course_id, chapter_id, lesson_id) -> list[dict]:
    sql = "SELECT * FROM knowledge_units WHERE 1=1"
    params: list[Any] = []
    if lesson_id:
        sql += " AND lesson_id=?"; params.append(lesson_id)
    elif chapter_id:
        sql += " AND chapter_id=?"; params.append(chapter_id)
    elif course_id:
        sql += " AND course_id=?"; params.append(course_id)
    return [dict(r) for r in db.fetch_all(sql + " ORDER BY emphasis DESC,id", tuple(params))]


def match_units(text: str, *, course_id=None, chapter_id=None, lesson_id=None,
                explicit_points=None, limit: int = 1) -> list[dict]:
    query_terms = _terms(text + " " + " ".join(str(x) for x in (explicit_points or [])))
    scored = []
    candidates = _candidate_units(course_id, chapter_id, lesson_id)
    for unit in candidates:
        target = _terms(f"{unit.get('topic','')} {unit.get('summary','')}")
        score = len(query_terms & target) / max(1, len(query_terms | target))
        if score > 0 or len(candidates) == 1:
            scored.append((score, unit))
    scored.sort(key=lambda x: (-x[0], -float(x[1].get("emphasis") or 0), x[1]["id"]))
    return [{**u, "match_score": round(s, 6)} for s, u in scored[:limit]]


def _recompute(knowledge_unit_id: int, reason: str, feedback_link_id=None) -> dict:
    old = db.fetch_one("SELECT * FROM knowledge_mastery WHERE knowledge_unit_id=?",
                       (knowledge_unit_id,))
    before, confidence_before = (float(old["mastery"]), float(old["confidence"])) if old else (0.5, 0.0)
    rows = db.fetch_all("SELECT weight,source_type,source_id FROM learning_feedback_links "
                        "WHERE knowledge_unit_id=? AND active=1 ORDER BY id", (knowledge_unit_id,))
    after = max(0.0, min(1.0, 0.5 + sum(float(r["weight"] or 0) for r in rows)))
    confidence_after = min(1.0, len(rows) * 0.2)
    ku = db.fetch_one("SELECT course_id,chapter_id FROM knowledge_units WHERE id=?",
                      (knowledge_unit_id,)) or {}
    explanation = "; ".join(f"{r['source_type']}#{r['source_id']}:{float(r['weight']):+.2f}" for r in rows)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO knowledge_mastery (knowledge_unit_id,course_id,chapter_id,mastery,confidence,signal_count,updated_from,explanation) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(knowledge_unit_id) DO UPDATE SET mastery=excluded.mastery,"
            "confidence=excluded.confidence,signal_count=excluded.signal_count,updated_from=excluded.updated_from,"
            "explanation=excluded.explanation,updated_at=datetime('now','localtime')",
            (knowledge_unit_id, ku.get("course_id"), ku.get("chapter_id"), after,
             confidence_after, len(rows), reason, explanation))
        conn.execute("INSERT INTO mastery_events (knowledge_unit_id,feedback_link_id,mastery_before,mastery_after,"
                     "confidence_before,confidence_after,reason) VALUES (?,?,?,?,?,?,?)",
                     (knowledge_unit_id, feedback_link_id, before, after,
                      confidence_before, confidence_after, reason))
    return {"knowledge_unit_id": knowledge_unit_id, "mastery_before": before,
            "mastery_after": after, "confidence": confidence_after, "reason": reason}


def record_feedback(*, source_type: str, source_id: int, relation: str, weight: float,
                    text: str, course_id=None, chapter_id=None, lesson_id=None,
                    explicit_points=None, evidence=None,
                    knowledge_unit_id: int | None = None) -> list[dict]:
    if knowledge_unit_id is not None:
        row = db.fetch_one("SELECT * FROM knowledge_units WHERE id=? AND status='active'",
                           (int(knowledge_unit_id),))
        units = [{**dict(row), "match_score": 1.0}] if row else []
    else:
        units = match_units(text, course_id=course_id, chapter_id=chapter_id,
                            lesson_id=lesson_id, explicit_points=explicit_points)
    results = []
    for unit in units:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO learning_feedback_links (knowledge_unit_id,source_type,source_id,relation,weight,evidence_json,active,updated_at) "
                "VALUES (?,?,?,?,?,?,1,datetime('now','localtime')) "
                "ON CONFLICT(knowledge_unit_id,source_type,source_id,relation) DO UPDATE SET "
                "weight=excluded.weight,evidence_json=excluded.evidence_json,active=1,updated_at=datetime('now','localtime')",
                (unit["id"], source_type, int(source_id), relation, float(weight),
                 json.dumps(evidence or {"match_score": unit["match_score"]}, ensure_ascii=False)))
            link = conn.execute("SELECT id FROM learning_feedback_links WHERE knowledge_unit_id=? AND source_type=? "
                                "AND source_id=? AND relation=?", (unit["id"], source_type, int(source_id), relation)).fetchone()
        results.append(_recompute(int(unit["id"]), f"{source_type}:{relation}", int(link["id"])))
    return results


def deactivate_feedback(source_type: str, source_id: int) -> list[dict]:
    rows = db.fetch_all("SELECT DISTINCT knowledge_unit_id FROM learning_feedback_links "
                        "WHERE source_type=? AND source_id=? AND active=1", (source_type, int(source_id)))
    db.execute("UPDATE learning_feedback_links SET active=0,updated_at=datetime('now','localtime') "
               "WHERE source_type=? AND source_id=?", (source_type, int(source_id)))
    return [_recompute(int(r["knowledge_unit_id"]), f"{source_type}:deactivated") for r in rows]


def source_trace_for_error(error_id: int) -> dict:
    units = db.fetch_all(
        "SELECT ku.id,ku.stable_key,ku.topic,km.mastery,km.confidence FROM learning_feedback_links l "
        "JOIN knowledge_units ku ON ku.id=l.knowledge_unit_id LEFT JOIN knowledge_mastery km ON km.knowledge_unit_id=ku.id "
        "WHERE l.source_type='error' AND l.source_id=? AND l.active=1 ORDER BY km.mastery,ku.id", (int(error_id),))
    out = []
    for unit in units:
        sources = db.fetch_all(
            "SELECT ss.source_id,ss.locator,ss.start_ms,ss.end_ms,ss.page_no,ss.slide_no,ss.text "
            "FROM knowledge_unit_sources kus JOIN source_spans ss ON ss.id=kus.source_span_id "
            "WHERE kus.knowledge_unit_id=? ORDER BY ss.ordinal LIMIT 12", (unit["id"],))
        out.append({**dict(unit), "classroom_sources": [dict(s) for s in sources]})
    return {"error_id": int(error_id), "knowledge_units": out}


def mastery_snapshot(course_id=None, chapter_id=None) -> list[dict]:
    sql = "SELECT km.*,ku.stable_key,ku.topic,ku.summary FROM knowledge_mastery km JOIN knowledge_units ku ON ku.id=km.knowledge_unit_id WHERE 1=1"
    params = []
    if chapter_id:
        sql += " AND km.chapter_id=?"; params.append(chapter_id)
    elif course_id:
        sql += " AND km.course_id=?"; params.append(course_id)
    return [dict(r) for r in db.fetch_all(sql + " ORDER BY km.mastery ASC,km.confidence DESC,ku.id", tuple(params))]
