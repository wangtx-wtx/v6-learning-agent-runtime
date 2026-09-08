"""
证据审计（V5.4）：校验 chunk_id / quote / locator，写入 evidence_links。

较 V5.3 的增强：
- 校验不再只做完全相等的字符串包含，支持 Unicode/空白归一化、数学符号归一化、OCR 字符容错。
- 证据类型区分 course_source / user_input / derived_reasoning / external_source。
- 写入统一 evidence_links，并记录生成模型与 prompt 版本。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

from .database import execute, query_one

logger = logging.getLogger(__name__)

# 数学符号等价替换（LaTeX/Unicode → 归一化空间）
_MATH_NORM = {
    "\\frac": " / ", "\\times": "*", "\\cdot": "*", "\\div": "/",
    "\\sum": "sum", "\\int": "int", "\\sqrt": "sqrt",
    "−": "-", "×": "*", "÷": "/", "π": "pi", "α": "alpha",
    "β": "beta", "γ": "gamma", "Δ": "delta", "θ": "theta", "λ": "lambda",
    "∞": "inf", "≤": "<=", "≥": ">=", "≠": "!=", "≈": "~", "±": "+-",
}
# OCR 常见字符混淆对（每类各尝试替换一次）
_OCR_PAIRS = [("0", "O"), ("1", "l"), ("c", "e"), ("a", "e"), ("，", ","), ("。", "."), ("；", ";")]


def _normalize_math(s: str) -> str:
    out = s
    for k, v in _MATH_NORM.items():
        out = out.replace(k, v)
    return out


def normalize_text(text: str) -> str:
    """Unicode NFC + 去空白 + 数学符号归一 + 小写。"""
    s = unicodedata.normalize("NFC", text or "")
    s = _normalize_math(s)
    s = re.sub(r"[\s\u3000]+", "", s)  # 去所有空白（含全角空格）
    return s.lower()


def _fuzzy_contains(haystack: str, needle: str) -> bool:
    if normalize_text(needle) in normalize_text(haystack):
        return True
    for a, b in _OCR_PAIRS:
        alt = needle.replace(a, b) if a in needle else needle.replace(b, a)
        if alt != needle and normalize_text(alt) in normalize_text(haystack):
            return True
    return False


def verify_chunk_exists(chunk_id) -> bool:
    if not chunk_id:
        return False
    return query_one("SELECT 1 AS x FROM source_chunks WHERE id=?", (chunk_id,)) is not None


def _quote_ok(chunk_id, quote: str) -> bool:
    if not chunk_id or not quote:
        return False
    row = query_one("SELECT text FROM source_chunks WHERE id=?", (chunk_id,))
    if not row:
        return False
    return _fuzzy_contains(row.get("text") or "", quote)


def verify_evidence_list(
    evidence_list: list[dict],
    owner_type: str = "unknown",
    owner_id: int = 0,
    persist: bool = True,
    model: str = "",
    prompt_version: str = "",
) -> list[dict]:
    results = []
    for ev in evidence_list:
        chunk_id = ev.get("chunk_id") or ev.get("chunkId")
        quote = (ev.get("quote") or "").strip()
        locator = (ev.get("locator") or "").strip()
        source_type = ev.get("source_type") or ev.get("evidence_kind") or "course_source"
        exists = verify_chunk_exists(chunk_id)
        quote_ok = _quote_ok(chunk_id, quote) if exists else False
        verified = exists and quote_ok
        # 通用推理（无真实 chunk 需求）视为可接受，但保留原始标记
        if not exists and source_type == "derived_reasoning":
            verified = True

        item = dict(ev)
        item.update({
            "chunk_exists": exists,
            "quote_matches": quote_ok,
            "verified": verified,
            "source_type": source_type,
            "verify_method": "fuzzy_norm",
        })
        results.append(item)
        if persist:
            execute(
                "INSERT INTO evidence_links "
                "(owner_type, owner_id, chunk_id, source_type, quote, locator, evidence_kind, "
                " verify_status, verify_method, model, prompt_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ev.get("owner_type", owner_type), ev.get("owner_id", owner_id), chunk_id,
                 source_type, quote, locator, source_type,
                 "verified" if verified else "failed", "fuzzy_match",
                 model or "", prompt_version or ""),
            )
    return results


def audit_evidence_block(owner_type: str, owner_id: int, block: dict, *, model: str = "", prompt_version: str = "") -> dict:
    """审计一个区块（dict 含 evidence 数组）或直接传入 evidence 数组。"""
    ev_list = block.get("evidence", []) if isinstance(block, dict) else (block or [])
    if not ev_list:
        return {"passed": True, "failed_items": [], "verified_items": [], "note": "无证据要求"}
    verified = verify_evidence_list(
        ev_list, owner_type=owner_type, owner_id=owner_id,
        persist=True, model=model, prompt_version=prompt_version,
    )
    failed = [v for v in verified if not v.get("verified")]
    return {
        "passed": len(failed) == 0,
        "failed_items": failed,
        "verified_items": verified,
    }