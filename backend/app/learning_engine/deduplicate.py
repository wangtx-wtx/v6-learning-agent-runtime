"""域内去重（V6 Phase 1）。

设计约束（任务书 §六 + 实现原则 7）:

* 只合并重复，**不物理删除** source_chunks、材料文件或 blob。
* 所有 duplicate 保留账目，并指向 canonical item / canonical span。
* chunk 重叠识别必须保守：只有规范化文本完全相同，或明确的前缀包含关系
  且短文本达到最小长度时，才判定为重复。
* 哈希算法版本化（``HASH_ALGO``），便于日后升级而不误判历史数据。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .contracts import ItemReason, SpanReason
from .normalize import hash_text, normalize_text

#: 哈希口径版本（写入报告，便于审计历史口径变化）。
HASH_ALGO = "sha256+norm-v1"

#: chunk 重叠判定的最小长度：低于此长度不做包含关系判定（保守）。
MIN_CONTAINMENT_CHARS = 120

#: 在 ``seen`` 中用于存放「紧凑文本 → canonical 引用」的前缀，避免与哈希键冲突。
_COMPACT_PREFIX = "\x00compact\x00"


@dataclass
class DuplicateVerdict:
    is_duplicate: bool
    reason_code: Optional[str] = None
    detail: Optional[str] = None
    #: 命中重复时，值为 canonical 的**调用方引用**（item 用 ordinal，span 用 ordinal）。
    canonical_ref: Optional[int] = None


_NOT_DUPLICATE = DuplicateVerdict(False)
#: ``span_duplicate_verdict`` 的「非重复」返回值（保持统一的二元组形状）。
_NO_SPAN_DUPLICATE: tuple[DuplicateVerdict, Optional[int]] = (_NOT_DUPLICATE, None)


def item_duplicate_key(content_hash: Optional[str], normalized_hash: Optional[str],
                       source_kind: str) -> Optional[str]:
    """域内 item 去重键：内容哈希优先，退回规范化文本哈希。

    键包含 ``source_kind``：同一份内容以讲义与 PPT 两种来源提供时，两者
    仍是**不同来源**，不互相吞并（覆盖审计需要各自可见）。
    """
    base = content_hash or normalized_hash
    if not base:
        return None
    return f"{source_kind}|{base}"


def detect_item_duplicate(content_hash: Optional[str], normalized_hash: Optional[str],
                          source_kind: str, seen: dict[str, int]) -> tuple[DuplicateVerdict, Optional[int]]:
    """在 ``seen`` 中命中 item 级重复。

    返回 ``(verdict, canonical_ref)``。``seen`` 由调用方维护并在域内复用，
    其值是该材料在调用方语境中的引用（构建阶段为 ordinal，落库后为 item_id）。
    """
    key = item_duplicate_key(content_hash, normalized_hash, source_kind)
    if key is None:
        return _NOT_DUPLICATE, None
    if key in seen:
        return (
            DuplicateVerdict(
                True,
                reason_code=ItemReason.DUPLICATE_CONTENT_HASH.value,
                detail="同一 domain 内内容哈希重复",
                canonical_ref=seen[key],
            ),
            seen[key],
        )
    return _NOT_DUPLICATE, None


def span_duplicate_verdict(text: str, normalized: str, source_kind: str,
                           seen: dict[str, int]) -> tuple[DuplicateVerdict, Optional[int]]:
    """span 级去重判定。

    规则（按顺序，任一命中即判重复）:

    1. 同来源类型内规范化文本哈希完全一致 → ``duplicate_normalized_text``
    2. 保守包含关系：规范化文本去掉空白后，一方是另一方的前缀/后缀，
       且双方都 ≥ ``MIN_CONTAINMENT_CHARS`` → ``duplicate_identical_text``

    规则 2 只在**同类来源**内生效，避免把 PPT 短页与转写长段互相吞并。
    两类索引分别存放，互不污染（早期实现把紧凑文本塞进同一个 dict，
    导致包含关系扫描会把哈希键也当作候选文本）。
    """
    if not normalized.strip():
        return _NO_SPAN_DUPLICATE
    digest = hash_text(normalized)
    key = f"{source_kind}|{digest}"
    if key in seen:
        return (
            DuplicateVerdict(True, reason_code=SpanReason.DUPLICATE_NORMALIZED_TEXT.value,
                             detail="规范化文本哈希一致", canonical_ref=seen[key]),
            seen[key],
        )

    compact = " ".join(normalized.split())
    if len(compact) >= MIN_CONTAINMENT_CHARS:
        for other_compact, other_id in seen.items():
            if not other_compact.startswith(_COMPACT_PREFIX):
                continue
            other = other_compact[len(_COMPACT_PREFIX):]
            if len(other) < MIN_CONTAINMENT_CHARS:
                continue
            if compact.startswith(other) or other.startswith(compact):
                return (
                    DuplicateVerdict(
                        True,
                        reason_code=SpanReason.DUPLICATE_IDENTICAL_TEXT.value,
                        detail="同类来源内规范化文本前缀包含（保守重叠判定）",
                        canonical_ref=other_id,
                    ),
                    other_id,
                )
    return _NO_SPAN_DUPLICATE


def register_span_canonical(seen: dict[str, int], normalized: str, source_kind: str,
                            canonical_ref: int) -> dict[str, int]:
    """登记 canonical 引用（``canonical_ref`` 可以是 ordinal 或 span_id）。

    返回更新后的索引（与 ``seen`` 同一个对象，返回值只是为了链式书写清晰）。
    """
    digest = hash_text(normalized)
    seen.setdefault(f"{source_kind}|{digest}", canonical_ref)
    compact = " ".join(normalized.split())
    if len(compact) >= MIN_CONTAINMENT_CHARS:
        seen.setdefault(_COMPACT_PREFIX + compact, canonical_ref)
    return seen


def register_span(seen: dict[str, int], normalized: str, source_kind: str,
                  span_id: int, canonical_span_id: Optional[int] = None) -> None:
    """登记 span 到去重索引（写库后使用真实 span_id）。

    ``canonical_span_id`` 显式给出时登记到该 canonical（保证后续重复仍指向
    同一个 canonical，而不是指向「重复的重复」）。
    """
    register_span_canonical(
        seen, normalized, source_kind,
        canonical_span_id if canonical_span_id is not None else span_id,
    )


def register_item(seen: dict[str, int], content_hash: Optional[str],
                  normalized_hash: Optional[str], source_kind: str, item_id: int) -> None:
    key = item_duplicate_key(content_hash, normalized_hash, source_kind)
    if key is not None:
        seen.setdefault(key, item_id)


def normalized_hash_of(text: Optional[str]) -> str:
    return hash_text(normalize_text(text))
