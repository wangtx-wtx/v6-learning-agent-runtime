"""
证据审计：校验 chunk_id / quote / locator。
模型只能引用 RAG 返回的 chunk_id。
"""
import json
from typing import Optional

from .database import execute, query


def verify_chunk_exists(chunk_id) -> bool:
    if not chunk_id:
        return False
    row = query("SELECT id FROM source_chunks WHERE id=?", (chunk_id,), one=True)
    return row is not None


def verify_quote_in_chunk(chunk_id, quote: str) -> bool:
    if not chunk_id or not quote:
        return False
    row = query("SELECT text FROM source_chunks WHERE id=?", (chunk_id,), one=True)
    if not row:
        return False
    text = row.get("text") or ""
    quote_norm = "".join(quote.split()).strip()
    text_norm = "".join(text.split())
    return quote_norm in text_norm


def verify_evidence_list(evidence_list: list[dict], owner_type: str = "unknown", owner_id: int = 0,
                         persist: bool = True) -> list[dict]:
    results = []
    for ev in evidence_list:
        chunk_id = ev.get("chunk_id") or ev.get("chunkId")
        quote = ev.get("quote", "") or ""
        locator = ev.get("locator", "") or ""
        exists = verify_chunk_exists(chunk_id)
        quote_ok = verify_quote_in_chunk(chunk_id, quote) if exists else False
        verified = exists and quote_ok
        item = dict(ev)
        item.update({
            "chunk_exists": exists,
            "quote_matches": quote_ok,
            "verified": verified,
        })
        results.append(item)
        if persist:
            execute(
                "INSERT INTO evidence (owner_type, owner_id, chunk_id, quote, locator, confidence, verified) "
                "VALUES (?,?,?,?,?,?,?)",
                (ev.get("owner_type", owner_type), ev.get("owner_id", owner_id), chunk_id,
                 quote, locator, ev.get("confidence", 0.0), 1 if verified else 0),
            )
    return results


def audit_evidence_block(owner_type: str, owner_id: int, block: dict) -> dict:
    ev_list = block.get("evidence", []) if isinstance(block, dict) else block or []
    if not ev_list:
        return {"passed": True, "failed_items": [], "verified_items": [], "note": "无证据要求"}
    verified = verify_evidence_list(ev_list, owner_type=owner_type, owner_id=owner_id)
    failed = [v for v in verified if not v.get("verified")]
    return {
        "passed": len(failed) == 0,
        "failed_items": failed,
        "verified_items": verified,
    }
