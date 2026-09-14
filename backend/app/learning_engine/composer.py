"""V6 Note Composer V2: assemble learning content from understanding and cognition.

The composer is deterministic. Models produce LessonUnderstanding/CognitiveMap; this
module decides which optional blocks are justified, attaches claim/source provenance,
and emits the single structured document used by HTML and PDF.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .. import database as db
from .evidence_v2 import get_claims, get_evidence_report
from .understanding import get_lesson_understanding

COMPOSER_SCHEMA_VERSION = "v6-note-composer.1"


def _unique(values):
    return list(dict.fromkeys(str(v).strip() for v in values if str(v).strip()))


def _refs(claim: dict) -> list[dict]:
    return [{
        "source_id": s.get("source_id"),
        "chunk_id": None,
        "quote": s.get("bound_quote") or "",
        "locator": s.get("locator") or "",
        "source_type": s.get("source_kind") or "course_source",
    } for s in claim.get("sources") or [] if s.get("binding_status") == "bound"]


def _block(block_type: str, title: str, content: str, claim: dict,
           selection_reason: str) -> dict:
    return {
        "type": block_type,
        "title": title,
        "content": content,
        "items": [], "rows": [],
        "source_refs": _refs(claim),
        "claim_type": claim.get("claim_type") or "",
        "claim_key": claim.get("claim_key") or "",
        "selection_reason": selection_reason,
    }


def _title(run_id: int) -> str:
    row = db.fetch_one(
        "SELECT COALESCE(l.title,c.title,co.name,'课堂笔记') AS title "
        "FROM workflow_runs w LEFT JOIN lessons l ON l.id=w.lesson_id "
        "LEFT JOIN chapters c ON c.id=w.chapter_id LEFT JOIN courses co ON co.id=w.course_id "
        "WHERE w.id=?", (int(run_id),))
    return str((row or {}).get("title") or "课堂笔记")


def compose_document(run_id: int) -> dict[str, Any]:
    from .learning_loop import sync_knowledge_units
    sync_knowledge_units(run_id)
    lesson = get_lesson_understanding(run_id) or {}
    structured = lesson.get("structured") or {}
    claims = get_claims(run_id)
    report = get_evidence_report(run_id) or {}
    by_key = {c.get("claim_key"): c for c in claims}
    sections: list[dict] = []
    audit: list[dict] = []

    def add_section(title: str, blocks: list[dict]):
        if blocks:
            sections.append({"title": title, "blocks": blocks})
            for b in blocks:
                audit.append({"claim_key": b.get("claim_key"), "block_type": b["type"],
                              "selection_reason": b.get("selection_reason")})

    # Claims are projected in a stable order. Structure labels let us choose blocks only
    # when the upstream analysis actually produced that structure.
    core, definitions, formulae, derivations, examples, cognition, supplements = ([] for _ in range(7))
    for claim in claims:
        key = claim.get("claim_key") or ""
        text = claim.get("claim_text") or ""
        producer = claim.get("producer_node") or ""
        if claim.get("claim_type") == "ai_explanation":
            supplements.append(_block("ai_explanation", "换一种方式理解", text, claim,
                                      f"CognitiveMap {key} requests an explanation"))
        elif producer.startswith("cognitive_map"):
            lower = text.lower()
            if "pitfall" in key or "易错" in text or "混淆" in text:
                typ, title = "pitfall", "易错与混淆"
            elif "emphasis" in key or "重点" in text or "注意" in text:
                typ, title = "notice", "课堂提醒"
            elif "missing_step" in key or "跳步" in text:
                typ, title = "derivation", "补全推理"
            elif "relation" in key or "区别" in lower or "辨析" in text:
                typ, title = "comparison", "概念辨析"
            else:
                typ, title = "key_point", "学习重点"
            cognition.append(_block(typ, title, text, claim,
                                    f"CognitiveMap item {key} supports {typ}"))
        elif ":definition:" in key:
            definitions.append(_block("definition", "定义", text, claim,
                                      f"LessonUnderstanding structure {key}"))
        elif ":formula:" in key:
            formulae.append(_block("formula", "公式", text, claim,
                                   f"LessonUnderstanding structure {key}"))
        elif ":derivation:" in key:
            derivations.append(_block("derivation", "推导", text, claim,
                                      f"LessonUnderstanding structure {key}"))
        elif ":example:" in key:
            examples.append(_block("example", "课堂例析", text, claim,
                                   f"LessonUnderstanding structure {key}"))
        elif ":ku" in key or producer == "lesson_understanding":
            core.append(_block("paragraph", "", text, claim,
                               f"LessonUnderstanding knowledge unit {key}"))

    add_section("本课脉络", core)
    add_section("概念与定义", definitions)
    add_section("公式与推导", formulae + derivations)
    add_section("课堂例析", examples)
    add_section("学习加工", cognition)
    add_section("教学补充", supplements)
    if not sections:
        sections = [{"title": "正文", "blocks": [{
            "type": "paragraph", "title": "", "content": "本次课堂没有形成可发布内容。",
            "items": [], "rows": [], "source_refs": [], "claim_type": "",
            "claim_key": "", "selection_reason": "no_publishable_claims",
        }]}]

    topics = _unique(structured.get("topics") or [])
    title = _title(run_id)
    document = {
        "title": title,
        "subtitle": "结构化课堂笔记",
        "deck": "；".join(topics[:6]) or "依据课堂全部唯一材料整理",
        "sections": sections,
        "sources": [],
    }
    # Keep exactly the renderer's canonical serialization so HTML/PDF artifact and
    # note revision report the same content hash.
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True)
    content_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    body = "\n\n".join(
        (b.get("title") + "\n" if b.get("title") else "") + (b.get("content") or "")
        for s in sections for b in s["blocks"])
    return {"title": title, "body": body, "document": document,
            "content_hash": content_hash, "schema_version": COMPOSER_SCHEMA_VERSION,
            "selection_audit": audit, "evidence_gate": report.get("gate", "failed"),
            "claim_count": len(claims)}


def review_composition(run_id: int, composition: dict) -> dict:
    issues = []
    doc = composition.get("document") or {}
    blocks = [b for s in doc.get("sections") or [] for b in s.get("blocks") or []]
    if not blocks or composition.get("claim_count", 0) == 0:
        issues.append("no_publishable_claim")
    if composition.get("evidence_gate") != "passed":
        issues.append("evidence_gate_not_passed")
    for block in blocks:
        if not block.get("selection_reason"):
            issues.append("block_without_selection_reason")
        if block.get("claim_type") in ("classroom_fact", "classroom_paraphrase") \
                and not block.get("source_refs"):
            issues.append("classroom_block_without_source")
        if block.get("claim_type") == "ai_explanation" and block.get("type") != "ai_explanation":
            issues.append("ai_explanation_not_visibly_marked")
    return {"passed": not issues, "status": "passed" if not issues else "revision_required",
            "issues": _unique(issues), "score": 1.0 if not issues else 0.0,
            "review_method": "deterministic_structure_and_evidence_guard"}
