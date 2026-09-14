"""Phase 4 Evidence V2：模型只选 Source ID，程序确定性绑定真实原文。

职责边界（任务书 §四–§七）:

* **模型**只负责生成/选择 claim 并返回 Source ID。模型**绝不**生成 exact quote：
  ``bound_quote`` 一律由本模块从 ``source_spans.text`` 复制，``quote_hash`` /
  ``source_text_hash`` 由程序计算；模型输出里即使出现 ``quote`` 字段也被忽略。
* **程序**负责：Source ID 格式校验 → 限定 ``domain_id`` 查询 Source Span →
  复制 locator / 时间戳 / 页码 / source_kind → 生成 bound_quote →
  计算证据覆盖率与门禁。
* 本阶段 **没有 Composer V2**：claim 来自确定性的 *shadow projection*（从已存在的
  LessonUnderstanding / CognitiveMap 投影），因此不得声称正式笔记已由 V6 接管
  （``real_notes_publish_enabled()`` 仍为 ``False``）。

三类 claim 与证据要求:

===========================  ==================  ==================================
claim_type                   需要课堂 Source ID   说明
===========================  ==================  ==================================
``classroom_fact``           是（且必须模型可见） 教师原话 / 材料直接支持
``classroom_paraphrase``     是（且必须模型可见） 课堂重述；Binder **只证明绑定存在**,
                                                 不声称已证明语义等价（属 Phase 5 Critic）
``ai_explanation``           否（not_required）  模型教学补充；不计入证据率分母,
                                                 且不得降级成课堂事实
===========================  ==================  ==================================

本模块**不**做语义真实性评审，只验证：引用存在、归属正确、quote 确实来自数据库、
类型与证据要求一致。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from .. import database as db
from . import config as engine_config
from .contracts import (
    CLAIM_TYPES_REQUIRING_SOURCE,
    SCHEMA_VERSION,
    BoundClaimSource,
    BoundEvidence,
    ClaimBindingMethod,
    ClaimBindingStatus,
    ClaimDraft,
    ClaimEvidenceStatus,
    ClaimImportance,
    ClaimProvenance,
    ClaimSourceRelation,
    ClaimSourceVisibility,
    ClaimType,
    EvidenceReport,
    Gate,
    ProducerKind,
    VisibilityBasis,
    provenance_visible_for,
)
from .understanding import get_lesson_understanding

logger = logging.getLogger(__name__)

#: 投影口径版本：进入 claim_key / input_hash，变更即让旧投影失效。
EVIDENCE_PROJECTION_VERSION = "v1"
#: Binder 版本（绑定算法变更时必须递增）。
EVIDENCE_BINDER_VERSION = "v1"
#: Phase 4.1：逐 claim 生产者可见性 provenance 口径版本。
EVIDENCE_PROVENANCE_VERSION = "v1"

#: 伪标签集合：``ai_explanation`` 必须保留自身类型，不得降级成课堂事实。
_FORBIDDEN_CLAIM_TYPES_FOR_AI = (ClaimType.CLASSROOM_FACT, ClaimType.CLASSROOM_PARAPHRASE)

#: Source ID 格式（与 source_map.SOURCE_ID_PATTERN 同口径，避免导入环）。
_SOURCE_ID_PREFIXES = ("T", "P", "D", "I", "S")

#: legacy ``evidence_links.owner_type``：V6 Evidence V2 的兼容投影标记。
LEGACY_OWNER_TYPE = "content_claim"


class EvidenceBindingError(RuntimeError):
    """绑定阶段的不可恢复错误（结构性问题，不是"某条 claim 失败"）。"""


# ---------------------------------------------------------------------------
# Source ID 格式
# ---------------------------------------------------------------------------
def source_id_format_ok(source_id: str) -> bool:
    """Source ID 格式校验（不查库）。

    与 ``source_map.SOURCE_ID_PATTERN`` 一致：``T/P/D/I/S`` + 4–6 位数字，
    可选 ``.NN`` 页内段号。格式非法必须**显式失败**，绝不静默剔除。
    """
    import re

    value = str(source_id or "").strip()
    if not value or value[0] not in _SOURCE_ID_PREFIXES:
        return False
    return bool(re.fullmatch(r"[TPDIS]\d{4,6}(?:\.\d{1,2})?", value))


def _hash_text(text: str) -> str:
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _short_hash(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# 模型可见证据边界（Phase 3 → Phase 4）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 逐 claim、逐生产者可见性（Phase 4.1）
# ---------------------------------------------------------------------------
def segment_input_ledger(domain_id: int) -> dict[int, list[str]]:
    """Phase 2 的 segment 输入账本：``{segment_ordinal: [source_id, ...]}``。

    只取 ``role='primary'``：overlap 只是上下文，不是该段**要求**理解的内容；
    把 overlap 也算作「模型已读」会虚增可见性。
    """
    rows = db.fetch_all(
        "SELECT ls.ordinal, ss.source_id, ss.role FROM lesson_segments ls "
        "JOIN segment_source_spans ss ON ss.segment_id = ls.id "
        "WHERE ls.domain_id=? ORDER BY ls.ordinal, ss.role, ss.ordinal", (int(domain_id),))
    out: dict[int, list[str]] = {}
    for row in rows:
        if str(row.get("role")) != "primary":
            continue
        out.setdefault(int(row["ordinal"]), [])
        sid = str(row["source_id"])
        if sid not in out[int(row["ordinal"])]:
            out[int(row["ordinal"])].append(sid)
    return out


def cognitive_batch_ledger(run_id: int) -> dict[int, dict[str, Any]]:
    """认知层逐 batch 审计账本：``{batch_ordinal: {...}}``（含 status 与已发送来源）。"""
    row = db.fetch_one("SELECT structured_json FROM cognitive_maps WHERE run_id=?",
                       (int(run_id),))
    if not row:
        return {}
    try:
        payload = json.loads(row.get("structured_json") or "{}")
    except Exception:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for batch in payload.get("batches") or []:
        if not isinstance(batch, dict):
            continue
        ordinal = int(batch.get("batch_ordinal") or 0)
        if ordinal <= 0:
            continue
        out[ordinal] = {
            "status": batch.get("status"),
            "input_hash": batch.get("input_hash") or "",
            "allowed_source_ids": [str(s) for s in (batch.get("allowed_source_ids") or [])],
            "visible_source_ids": [str(s) for s in
                                   (batch.get("material_included_source_ids") or [])],
        }
    return out


def _provenance_for_segments(domain_id: int, segment_ordinals: list[int],
                             producer_node: str,
                             producer_input_hash: str = "") -> ClaimProvenance:
    """按 **segment 输入账本**为 LessonUnderstanding claim 构造 provenance。"""
    ledger = segment_input_ledger(domain_id)
    ordinals = sorted({int(o) for o in segment_ordinals if int(o) > 0})
    invocations = []
    per_source: dict[str, list[int]] = {}
    for ordinal in ordinals:
        visible = list(ledger.get(ordinal) or [])
        invocations.append({
            "invocation": ordinal,
            "input_hash": producer_input_hash,
            "allowed_source_ids": visible,
            "visible_source_ids": visible,
        })
        for sid in visible:
            per_source.setdefault(sid, []).append(ordinal)
    basis = (VisibilityBasis.SEGMENT_INPUT_LEDGER if invocations
             else VisibilityBasis.UNAVAILABLE)
    return ClaimProvenance(
        producer_stage="phase2_segment_understanding",
        producer_node=producer_node,
        producer_kind=ProducerKind.LESSON_UNDERSTANDING,
        invocation_refs=ordinals,
        visible_source_ids=sorted(per_source.keys()),
        visibility_basis=basis,
        provenance_version=EVIDENCE_PROVENANCE_VERSION,
        per_source_invocations={k: sorted(set(v)) for k, v in per_source.items()},
    )


def _provenance_for_item(domain_id: int, run_id: int, item: dict
                         ) -> ClaimProvenance:
    """按 **cognitive batch 审计**为 CognitiveItem claim 构造 provenance。

    只认该 item **自己的** invocation 记录（``provenance.per_source_invocations``）。
    记录缺失/损坏 → ``UNAVAILABLE``（claim 必须 failed，不得回退到全局并集）。
    """
    stored = item.get("provenance") or {}
    if not isinstance(stored, dict) or not stored.get("per_source_invocations"):
        return ClaimProvenance(
            producer_stage="phase3_student_simulator",
            producer_node="cognitive_map",
            producer_kind=ProducerKind.COGNITIVE_MAP,
            invocation_refs=[],
            visible_source_ids=[],
            visibility_basis=VisibilityBasis.UNAVAILABLE,
            provenance_version=EVIDENCE_PROVENANCE_VERSION,
        )
    per_source_raw = stored.get("per_source_invocations") or {}
    per_source: dict[str, list[int]] = {}
    for sid, ordinals in per_source_raw.items():
        try:
            per_source[str(sid)] = sorted({int(o) for o in (ordinals or []) if int(o) > 0})
        except (TypeError, ValueError):
            continue
    invocations = []
    for entry in stored.get("invocations") or []:
        if not isinstance(entry, dict):
            continue
        invocations.append({
            "invocation": int(entry.get("invocation") or 0),
            "input_hash": entry.get("input_hash") or "",
            "allowed_source_ids": [str(s) for s in (entry.get("allowed_source_ids") or [])],
            "visible_source_ids": [str(s) for s in (entry.get("visible_source_ids") or [])],
        })
    basis = (VisibilityBasis.COGNITIVE_BATCH_AUDIT
             if (per_source or invocations) else VisibilityBasis.UNAVAILABLE)
    return ClaimProvenance(
        producer_stage="phase3_student_simulator",
        producer_node="cognitive_map",
        producer_kind=ProducerKind.COGNITIVE_MAP,
        invocation_refs=sorted({o for v in per_source.values() for o in v}),
        visible_source_ids=sorted(per_source.keys()),
        visibility_basis=basis,
        provenance_version=EVIDENCE_PROVENANCE_VERSION,
        per_source_invocations=per_source,
    )


def presented_source_ids(domain_id: int, run_id: int) -> set[str]:
    """[已停用为通过依据] run 级已呈现来源**并集**。

    保留仅供诊断/对照：它把该 run 所有认知批次读过的来源合并成一个集合，
    **不能**证明某条 claim 的来源进入了产出该 claim 的那次调用。
    Phase 4.1 起 Binder 一律使用逐 claim 的 ``ClaimProvenance``；此函数返回的集合
    出现在任何 gate 判定里都属回归。
    """
    return {sid for batch in cognitive_batch_ledger(run_id).values()
            if batch.get("status") == "consumed"
            for sid in (batch.get("visible_source_ids") or [])}


def omitted_due_budget_source_ids(run_id: int) -> set[str]:
    """进入 ``omitted_due_budget`` 的来源（合法但未发送给模型）。"""
    row = db.fetch_one("SELECT structured_json FROM cognitive_maps WHERE run_id=?",
                       (int(run_id),))
    if not row:
        return set()
    try:
        payload = json.loads(row.get("structured_json") or "{}")
    except Exception:
        return set()
    out: set[str] = set()
    for batch in payload.get("batches") or []:
        if not isinstance(batch, dict):
            continue
        for entry in batch.get("material_omitted_due_budget") or []:
            if isinstance(entry, dict) and entry.get("source_id"):
                out.add(str(entry["source_id"]))
            elif isinstance(entry, str):
                out.add(entry)
    return out


# ---------------------------------------------------------------------------
# 确定性 shadow claim projection
# ---------------------------------------------------------------------------
def _claim_key(producer: str, claim_type: ClaimType, origin_ref: str,
               claim_text: str) -> str:
    """确定性稳定键：同输入重跑必须完全一致（不含时间戳 / 自增 id）。"""
    digest = _short_hash({
        "projection": EVIDENCE_PROJECTION_VERSION,
        "producer": producer,
        "type": claim_type.value,
        "origin": origin_ref,
        "text": claim_text,
    })
    return f"{producer}:{claim_type.value}:{digest}"


def _importance_from_emphasis(emphasis: Any) -> ClaimImportance:
    try:
        value = float(emphasis or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if value >= 0.85:
        return ClaimImportance.CRITICAL
    if value >= 0.6:
        return ClaimImportance.MAJOR
    return ClaimImportance.SUPPORTING


def _as_refs(raw: Any, valid_source_ids: set[str]) -> list[str]:
    """只接受**字符串形式的 Source ID**；对象形式的 quote 一律丢弃。

    这正是「模型不得生成 exact quote」的落地点：任何 ``{"chunk_id":..,"quote":..}``
    形状都不会被当成来源，也不会把 quote 带进 claim。
    """
    refs: list[str] = []
    for item in raw or []:
        if isinstance(item, str):
            value = item.strip()
            if value and value in valid_source_ids and value not in refs:
                refs.append(value)
        elif isinstance(item, dict):
            # 对象形式（含 quote / chunk_id）：只取其中的 Source ID 字符串字段，
            # quote 字段被显式忽略。
            for key in ("source_id", "source_ref"):
                value = str(item.get(key) or "").strip()
                if value and value in valid_source_ids and value not in refs:
                    refs.append(value)
        else:
            continue
    return refs


def project_claims(domain_id: int, run_id: int) -> list[ClaimDraft]:
    """从现有 V6 中间产物**确定性**投影 ClaimDraft（不调用模型）。

    Phase 5 才实现 Composer V2；本函数不修改任何既有产物，也不从 legacy note
    文本反向猜 Source ID，更不做模糊检索。

    **Phase 4.1**：每条 draft 都带**自己**的 producer provenance：

    * LessonUnderstanding → 按 ``segment_source_spans``（segment 输入账本）判定，
      **与认知层完全无关**；
    * CognitiveMap → 按该 item 自己记录的 invocation 级 provenance 判定。
    """
    lesson = get_lesson_understanding(run_id) or {}
    structured = lesson.get("structured") or {}
    valid_source_ids = {str(s) for s in (structured.get("valid_source_ids") or [])}
    lu_hash = lesson.get("input_hash") or ""
    understanding_id = lesson.get("id")
    # KU / 结构块 → 产生它的 segment ordinal（Phase 2 已持久化 segment_ids）
    segment_ledger = segment_input_ledger(domain_id)
    drafts: list[ClaimDraft] = []

    def segment_refs_of(obj: dict) -> list[int]:
        raw = obj.get("segment_ids")
        if isinstance(raw, (list, tuple)):
            try:
                return sorted({int(x) for x in raw if int(x) > 0})
            except (TypeError, ValueError):
                return []
        return []

    def add(claim_type: ClaimType, origin_ref: str, text: str,
            refs: list[str], importance: ClaimImportance, producer: str,
            provenance: ClaimProvenance) -> None:
        body = str(text or "").strip()
        if not body:
            return
        drafts.append(ClaimDraft(
            claim_key=_claim_key(producer, claim_type, origin_ref, body),
            claim_type=claim_type,
            claim_text=body,
            importance=importance,
            source_refs=refs,
            origin_ref=origin_ref,
            producer_node=producer,
            producer_input_hash=lu_hash,
            provenance=provenance,
        ))

    def lu_provenance(obj: dict, producer: str) -> ClaimProvenance:
        return _provenance_for_segments(domain_id, segment_refs_of(obj), producer, lu_hash)

    # ---- LessonUnderstanding ----
    for unit in structured.get("knowledge_units") or []:
        if not isinstance(unit, dict):
            continue
        refs = _as_refs(unit.get("source_refs"), valid_source_ids)
        prov = lu_provenance(unit, "lesson_understanding")
        add(ClaimType.CLASSROOM_PARAPHRASE, f"ku:{unit.get('id') or unit.get('topic') or ''}",
            unit.get("summary") or unit.get("topic") or "", refs,
            _importance_from_emphasis(unit.get("teacher_emphasis")), "lesson_understanding",
            prov)
        # 关系也是课堂重述（"A 与 B 的关系"），必须带来源
        for relation in unit.get("relations") or []:
            text = str(relation).strip()
            if text:
                add(ClaimType.CLASSROOM_PARAPHRASE,
                    f"ku-rel:{unit.get('id') or ''}:{_short_hash(text)[:8]}", text, refs,
                    ClaimImportance.SUPPORTING, "lesson_understanding", prov)

    structure_specs = (
        ("definitions", ClaimType.CLASSROOM_FACT, "definition", "term", "meaning"),
        ("formulas", ClaimType.CLASSROOM_FACT, "formula", "expression", "meaning"),
        ("derivations", ClaimType.CLASSROOM_FACT, "derivation", "goal", "steps"),
        ("examples", ClaimType.CLASSROOM_FACT, "example", "prompt", "solution"),
    )
    for key, claim_type, label, title_key, body_key in structure_specs:
        for item in structured.get(key) or []:
            if not isinstance(item, dict):
                continue
            refs = _as_refs(item.get("source_refs"), valid_source_ids)
            title = str(item.get(title_key) or "").strip()
            body = item.get(body_key)
            if isinstance(body, list):
                body = "；".join(str(x) for x in body if str(x).strip())
            text = f"{title}：{body}" if title and body else (title or str(body or ""))
            origin = f"{label}:{title or _short_hash(item)[:8]}"
            importance = (ClaimImportance.MAJOR if key == "formulas"
                          else ClaimImportance.SUPPORTING)
            add(claim_type, origin, text, refs, importance, "lesson_understanding",
                lu_provenance(item, "lesson_understanding"))

    # ---- CognitiveMap ----
    cog_row = db.fetch_one("SELECT id, structured_json FROM cognitive_maps WHERE run_id=?",
                           (int(run_id),))
    if cog_row:
        try:
            cog_payload = json.loads(cog_row.get("structured_json") or "{}")
        except Exception:
            cog_payload = {}
        for item in cog_payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("item_type") or "")
            origin = str(item.get("origin") or "")
            refs = _as_refs(item.get("source_refs"), valid_source_ids)
            title = str(item.get("title") or "").strip()
            explanation = str(item.get("explanation") or "").strip()
            text = f"{title}：{explanation}" if title and explanation else (title or explanation)
            origin_ref = f"cog:{item.get('stable_key') or item_type}"
            if origin == "classroom_evidence":
                # 课堂证据 → 课堂事实（教师明确提醒 / 材料直接支持）
                claim_type = ClaimType.CLASSROOM_FACT
                producer = "cognitive_map"
            elif origin == "confirmed_error":
                # 错题支持：不是教师课堂事实，按课堂重述处理并保留 origin
                claim_type = ClaimType.CLASSROOM_PARAPHRASE
                producer = "cognitive_map:confirmed_error"
            else:
                # model_cognitive_inference（含 model origin 兜底）→ 模型教学补充
                claim_type = ClaimType.AI_EXPLANATION
                producer = "cognitive_map:model_inference"
            importance = _importance_from_emphasis(item.get("severity"))
            add(claim_type, origin_ref, text, refs, importance, producer,
                _provenance_for_item(domain_id, run_id, item))

    return drafts


def projection_meta(run_id: int, domain_id: int) -> dict[str, Any]:
    """投影来源的 id 与哈希（写入 content_claims 便于反查）。"""
    lesson = get_lesson_understanding(run_id) or {}
    cog_row = db.fetch_one("SELECT id FROM cognitive_maps WHERE run_id=?", (int(run_id),))
    return {
        "lesson_understanding_id": int(lesson["id"]) if lesson.get("id") else None,
        "cognitive_map_id": int(cog_row["id"]) if cog_row else None,
        "producer_input_hash": lesson.get("input_hash") or "",
    }


# ---------------------------------------------------------------------------
# 确定性 Evidence Binder
# ---------------------------------------------------------------------------
def _span_rows(domain_id: int, source_ids: list[str]) -> dict[str, dict]:
    """**同时限定 domain_id + source_id** 读取 span（禁止仅按 source_id 全库查询）。"""
    if not source_ids:
        return {}
    q = ",".join("?" * len(source_ids))
    rows = db.fetch_all(
        f"SELECT id, source_id, locator, start_ms, end_ms, page_no, slide_no, source_kind, "
        f" text, normalized_text_hash FROM source_spans "
        f"WHERE domain_id=? AND source_id IN ({q})", tuple([int(domain_id)] + source_ids))
    return {str(r["source_id"]): dict(r) for r in rows}


def _foreign_source_ids(source_ids: list[str]) -> set[str]:
    """这些 source_id 是否**存在于别的 domain**（跨域引用判定）。"""
    if not source_ids:
        return set()
    q = ",".join("?" * len(source_ids))
    rows = db.fetch_all(
        f"SELECT DISTINCT source_id FROM source_spans WHERE source_id IN ({q})",
        tuple(source_ids))
    return {str(r["source_id"]) for r in rows}


def bind_claims(domain_id: int, run_id: int,
                drafts: Optional[list[ClaimDraft]] = None) -> list[BoundEvidence]:
    """把 ClaimDraft 绑定为 BoundEvidence（**纯确定性**，不调用模型）。

    规则（任务书 §六 + Phase 4.1）:

    1. Source ID 先做格式校验（非法 → ``invalid`` 并阻断门禁，不静默剔除）；
    2. 查询同时限定 ``domain_id`` + ``source_id``；本 domain 找不到但别处存在
       → ``invalid`` + ``cross_domain``（并计入 ``cross_domain_refs``）；
    3. ``bound_quote`` 直接取 ``source_spans.text``；locator / 时间戳 / 页码 /
       source_kind 全部从数据库复制；``quote_hash`` / ``source_text_hash`` 由程序算；
    4. **可见性以该 claim 自己的 producer provenance 为准**：
       * provenance 不可用 → ``producer_visibility_missing``，claim failed；
       * 来源不在该 claim 的 ``visible_source_ids`` → ``presented_to_producer=False``
         + ``source_not_presented_to_this_claim_producer``，claim failed
         （**即使本 run 其他 batch 读过该来源也必须 failed**）；
       * 只有该 claim 的 invocation 真的发过 → ``presented_to_producer=True``；
    5. ``ai_explanation`` → ``not_required``；但若它主动声明了来源，仍如实绑定并
       标注可见性，不得据此把课堂事实伪装成 AI 补充。
    """
    if drafts is None:
        drafts = project_claims(domain_id, run_id)
    results: list[BoundEvidence] = []

    for draft in drafts:
        source_ids = list(dict.fromkeys(str(s) for s in draft.source_refs if str(s).strip()))
        spans = _span_rows(domain_id, source_ids)
        foreign = (_foreign_source_ids(source_ids) - set(spans.keys())) if source_ids else set()
        requires = draft.claim_type in CLAIM_TYPES_REQUIRING_SOURCE
        provenance = draft.provenance
        provenance_usable = provenance.visibility_basis != VisibilityBasis.UNAVAILABLE

        sources: list[BoundClaimSource] = []
        reasons: list[str] = []
        if requires and not provenance_usable:
            # 没有可用的 producer provenance：不得推断，直接失败
            reasons.append("producer_visibility_missing")
        for sid in source_ids:
            span = spans.get(sid)
            if not source_id_format_ok(sid):
                sources.append(BoundClaimSource(
                    source_id=sid, binding_status=ClaimBindingStatus.INVALID,
                    visibility=ClaimSourceVisibility.UNKNOWN,
                    failure_reason="malformed_source_id"))
                reasons.append(f"malformed_source_id:{sid}")
                continue
            if span is None:
                status = (ClaimBindingStatus.INVALID if sid in foreign
                          else ClaimBindingStatus.MISSING)
                reason = ("cross_domain_source_id" if sid in foreign
                          else "source_not_in_domain")
                sources.append(BoundClaimSource(
                    source_id=sid, binding_status=status,
                    visibility=ClaimSourceVisibility.UNKNOWN, failure_reason=reason))
                reasons.append(f"{reason}:{sid}")
                continue
            quote = str(span.get("text") or "")
            visibility = (provenance_visible_for(provenance, sid) if provenance_usable
                          else ClaimSourceVisibility.UNKNOWN)
            visible_invocations = list(
                (provenance.per_source_invocations or {}).get(sid) or [])
            entry = BoundClaimSource(
                source_id=sid,
                source_span_id=int(span["id"]),
                relation=ClaimSourceRelation.SUPPORTS,
                binding_status=ClaimBindingStatus.BOUND,
                binding_method=ClaimBindingMethod.DETERMINISTIC_SOURCE_ID,
                bound_quote=quote,                       # 只可能来自数据库
                quote_hash=_hash_text(quote),            # 程序计算
                source_text_hash=str(span.get("normalized_text_hash") or ""),
                locator=span.get("locator"),
                start_ms=span.get("start_ms"),
                end_ms=span.get("end_ms"),
                page_no=span.get("page_no"),
                slide_no=span.get("slide_no"),
                source_kind=span.get("source_kind"),
                presented_to_producer=(visibility == ClaimSourceVisibility.VISIBLE),
                visibility=visibility,
                visible_invocations=visible_invocations,
            )
            if requires and visibility != ClaimSourceVisibility.VISIBLE:
                # 「数据库里有」≠「该 producer 读过」；「别的 batch 读过」也不算
                entry.failure_reason = "source_not_presented_to_this_claim_producer"
                reasons.append(
                    f"source_not_presented_to_this_claim_producer:{sid}")
            sources.append(entry)

        bound = [s for s in sources
                 if s.binding_status == ClaimBindingStatus.BOUND
                 and s.failure_reason is None]

        if draft.claim_type == ClaimType.AI_EXPLANATION:
            if draft.source_refs and not sources:
                status = ClaimEvidenceStatus.FAILED
                reasons.append("ai_explanation_with_unresolvable_source_refs")
            else:
                # 声明了来源也不要求证据；但来源的绑定/可见性事实已如实记录在上方，
                # 不得因为「标成 ai_explanation」就掩盖「它其实声称了课堂事实」。
                status = ClaimEvidenceStatus.NOT_REQUIRED
        elif not draft.source_refs:
            status = ClaimEvidenceStatus.FAILED
            reasons.append("classroom_claim_without_source")
        elif bound and provenance_usable:
            status = ClaimEvidenceStatus.PASSED
        else:
            status = ClaimEvidenceStatus.FAILED
            if not reasons:
                reasons.append("no_bound_source")

        results.append(BoundEvidence(
            claim_key=draft.claim_key,
            claim_type=draft.claim_type,
            importance=draft.importance,
            evidence_status=status,
            provenance=provenance,
            sources=sources,
            failure_reasons=sorted(set(reasons)),
        ))
    return results


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------
def evaluate_evidence_gate(report: EvidenceReport) -> tuple[Gate, Optional[str], list[str]]:
    """Evidence 硬门禁（任务书 §七 + Phase 4.1）。

    只有「引用存在、归属正确、quote 来自数据库、**来源确实进入产出该 claim 的
    producer invocation**、类型与证据要求一致」被验证；**不**声称语义真实性已
    评审（那属 Phase 5 Critic）。
    """
    issues: list[str] = []
    blocking: list[str] = []

    if report.invalid_source_refs > 0:
        blocking.append(f"invalid_source_refs={report.invalid_source_refs}"
                        "（伪造或格式非法的 Source ID）")
    if report.cross_domain_refs > 0:
        blocking.append(f"cross_domain_refs={report.cross_domain_refs}"
                        "（引用了不属于当前 Material Domain 的来源）")
    if report.unbound_source_refs > 0:
        blocking.append(f"unbound_source_refs={report.unbound_source_refs}"
                        "（本 domain 内不存在该 Source ID）")
    if report.not_in_claim_producer_refs > 0:
        blocking.append(
            f"not_in_claim_producer_refs={report.not_in_claim_producer_refs}"
            "（来源未进入产出该 claim 的模型调用 —— 其他 batch 读过不算已读）")
    if report.claims_without_provenance > 0:
        blocking.append(
            f"claims_without_provenance={report.claims_without_provenance}"
            "（缺少 producer provenance：不得回退到 run/domain 级全局集合）")
    if report.unread_source_refs > 0:
        blocking.append(f"unread_source_refs={report.unread_source_refs}"
                        "（来源未呈现给模型 —— 不得算作模型已阅读的证据）")
    if report.failed_claims > 0:
        blocking.append(f"failed_claims={report.failed_claims}"
                        f"（{report.failed_claims}/{report.required_claims} 条需要课堂证据的 claim 未通过绑定）")
    if report.critical_evidence_denominator > 0 \
            and report.critical_claim_evidence_rate < 1.0:
        blocking.append(
            f"critical_claim_evidence_rate={report.critical_claim_evidence_rate}"
            f"（{report.critical_claims_bound}/{report.critical_evidence_denominator} "
            "条 critical claim 通过证据绑定）")

    if blocking:
        return Gate.FAILED.value, "；".join(blocking), issues + blocking
    if report.critical_evidence_denominator == 0:
        issues.append("critical_evidence_denominator=0（本轮没有需要课堂证据的 critical claim，"
                      "证据率记 1.0 但分母为 0）")
    return Gate.PASSED.value, None, issues


#: 这些 failure_reason 表示「来源未进入产出该 claim 的 producer invocation」。
_NOT_IN_CLAIM_PRODUCER = "source_not_presented_to_this_claim_producer"
#: 兼容旧口径的原因码（Phase 4 曾用过），同样计入 not_in_claim_producer_refs。
_NOT_IN_CLAIM_PRODUCER_LEGACY = "source_not_presented_to_producer"


def compute_evidence_report(run_id: int, domain_id: int,
                            bound: Optional[list[BoundEvidence]] = None) -> EvidenceReport:
    """从绑定结果计算 Evidence 报告（全部数值确定性派生）。"""
    if bound is None:
        bound = bind_claims(domain_id, run_id)

    by_type: dict[str, int] = {}
    by_producer: dict[str, int] = {}
    required = passed = failed = not_required = 0
    critical_denom = critical_bound = 0
    invalid = cross = unbound = unread = not_in_producer = no_provenance = 0

    for item in bound:
        by_type[item.claim_type.value] = by_type.get(item.claim_type.value, 0) + 1
        producer = (item.provenance.producer_node if item.provenance else "") or "unknown"
        by_producer[producer] = by_producer.get(producer, 0) + 1
        if item.claim_type == ClaimType.AI_EXPLANATION:
            not_required += 1
        else:
            required += 1
            if item.evidence_status == ClaimEvidenceStatus.PASSED:
                passed += 1
            else:
                failed += 1
        if item.claim_type in CLAIM_TYPES_REQUIRING_SOURCE:
            if item.provenance is None or \
                    item.provenance.visibility_basis == VisibilityBasis.UNAVAILABLE:
                no_provenance += 1
            if item.importance == ClaimImportance.CRITICAL:
                critical_denom += 1
                if item.evidence_status == ClaimEvidenceStatus.PASSED:
                    critical_bound += 1
        for src in item.sources:
            if src.binding_status == ClaimBindingStatus.INVALID:
                if src.failure_reason == "cross_domain_source_id":
                    cross += 1
                else:
                    invalid += 1
            elif src.binding_status == ClaimBindingStatus.MISSING:
                unbound += 1
            if src.failure_reason in (_NOT_IN_CLAIM_PRODUCER, _NOT_IN_CLAIM_PRODUCER_LEGACY):
                not_in_producer += 1
            if src.failure_reason in (_NOT_IN_CLAIM_PRODUCER, _NOT_IN_CLAIM_PRODUCER_LEGACY,
                                      "source_not_presented_to_producer"):
                unread += 1
        if "producer_visibility_missing" in (item.failure_reasons or []):
            no_provenance += 1

    rate = (round(critical_bound / critical_denom, 6) if critical_denom else 1.0)
    report = EvidenceReport(
        run_id=int(run_id), domain_id=int(domain_id),
        engine_version=engine_config.engine_version(),
        engine_mode=engine_config.engine_mode(),
        claims_total=len(bound),
        required_claims=required,
        bound_claims=passed,
        failed_claims=failed,
        not_required_claims=not_required,
        critical_claims=critical_denom,
        critical_claims_bound=critical_bound,
        critical_claim_evidence_rate=rate,
        critical_evidence_denominator=critical_denom,
        invalid_source_refs=invalid,
        cross_domain_refs=cross,
        unbound_source_refs=unbound,
        unread_source_refs=unread,
        not_in_claim_producer_refs=not_in_producer,
        claims_without_provenance=no_provenance,
        claims_by_type=by_type,
        claims_by_producer=by_producer,
    )
    gate, reason, issues = evaluate_evidence_gate(report)
    report.gate = Gate(gate)
    report.degradation_reason = reason
    report.issues = issues
    return report


# ---------------------------------------------------------------------------
# 持久化（事务 + 幂等 + 取消安全）
# ---------------------------------------------------------------------------
def _claim_input_hash(draft: ClaimDraft, domain_id: int) -> str:
    """claim 输入哈希：区域 + 类型 + 文本 + 来源 + 生产者输入 + provenance。"""
    return "sha256:" + hashlib.sha256(json.dumps({
        "algo": "sha256+claim-input-v1",
        "schema": SCHEMA_VERSION,
        "projection": EVIDENCE_PROJECTION_VERSION,
        "binder": EVIDENCE_BINDER_VERSION,
        "provenance_version": EVIDENCE_PROVENANCE_VERSION,
        "domain_id": int(domain_id),
        "claim_key": draft.claim_key,
        "claim_type": draft.claim_type.value,
        "claim_text": draft.claim_text,
        "importance": draft.importance.value,
        "source_refs": list(draft.source_refs),
        "origin_ref": draft.origin_ref,
        "producer_node": draft.producer_node,
        "producer_input_hash": draft.producer_input_hash,
        # Phase 4.1：可见性口径进入输入哈希 —— provenance 变则 claim 输入变
        "producer_kind": draft.provenance.producer_kind.value,
        "visibility_basis": draft.provenance.visibility_basis.value,
        "invocation_refs": list(draft.provenance.invocation_refs),
        "visible_source_ids": list(draft.provenance.visible_source_ids),
        "per_source_invocations": draft.provenance.per_source_invocations,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _claim_content_hash(bound: BoundEvidence) -> str:
    """claim 内容哈希：结论 + 逐来源绑定终态（可复算）。"""
    return "sha256:" + hashlib.sha256(json.dumps({
        "algo": "sha256+claim-content-v1",
        "binder": EVIDENCE_BINDER_VERSION,
        "provenance_version": EVIDENCE_PROVENANCE_VERSION,
        "claim_key": bound.claim_key,
        "claim_type": bound.claim_type.value,
        "importance": bound.importance.value,
        "evidence_status": bound.evidence_status.value,
        "failure_reasons": list(bound.failure_reasons),
        "sources": [{
            "source_id": s.source_id,
            "source_span_id": s.source_span_id,
            "relation": s.relation.value,
            "binding_status": s.binding_status.value,
            "binding_method": s.binding_method.value,
            "quote_hash": s.quote_hash,
            "source_text_hash": s.source_text_hash,
            "presented_to_producer": s.presented_to_producer,
            "visibility": s.visibility.value,
            "visible_invocations": list(s.visible_invocations),
            "failure_reason": s.failure_reason,
        } for s in bound.sources],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def persist_evidence(domain_id: int, run_id: int,
                     drafts: list[ClaimDraft],
                     bound: list[BoundEvidence]) -> dict[str, int]:
    """在**单个事务**里写入 content_claims + claim_sources（幂等）。

    * 同 run 重跑：按 ``(run_id, claim_key)`` UPSERT，sources 全量重写 —— 数量不增加；
    * 输入改变后重建的是**当前 run** 的投影，历史 run 的 claims 不受影响；
    * 事务失败/取消即整体回滚，不留半套数据。
    """
    by_key = {b.claim_key: b for b in bound}
    meta = projection_meta(run_id, domain_id)
    written_claims = written_sources = 0

    with db.transaction() as conn:
        # 只清理**本 run** 的投影（外键 ON DELETE CASCADE 会带走 claim_sources）
        conn.execute("DELETE FROM content_claims WHERE run_id=?", (int(run_id),))
        for draft in drafts:
            item = by_key.get(draft.claim_key)
            if item is None:
                continue
            cur = conn.execute(
                "INSERT INTO content_claims (run_id, domain_id, lesson_understanding_id, "
                " cognitive_map_id, note_id, block_id, claim_key, claim_type, claim_text, "
                " importance, source_refs_json, producer_node, producer_input_hash, "
                " evidence_status, input_hash, content_hash, producer_provenance_json, "
                " created_at, updated_at) "
                "VALUES (?,?,?,?,NULL,NULL,?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'), "
                " datetime('now','localtime'))",
                (int(run_id), int(domain_id), meta["lesson_understanding_id"],
                 meta["cognitive_map_id"], draft.claim_key, draft.claim_type.value,
                 draft.claim_text, draft.importance.value,
                 json.dumps(list(draft.source_refs), ensure_ascii=False),
                 draft.producer_node, draft.producer_input_hash,
                 item.evidence_status.value, _claim_input_hash(draft, domain_id),
                 _claim_content_hash(item),
                 json.dumps(draft.provenance.model_dump(mode="json"),
                            ensure_ascii=False, sort_keys=True)),
            )
            claim_id = int(cur.lastrowid)
            item.claim_id = claim_id
            written_claims += 1
            for src in item.sources:
                conn.execute(
                    "INSERT INTO claim_sources (claim_id, source_span_id, source_id, relation, "
                    " binding_status, binding_method, bound_quote, quote_hash, source_text_hash, "
                    " locator, start_ms, end_ms, page_no, slide_no, source_kind, "
                    " presented_to_producer, visibility, visible_invocations_json, "
                    " failure_reason, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))",
                    (claim_id, src.source_span_id, src.source_id, src.relation.value,
                     src.binding_status.value, src.binding_method.value, src.bound_quote,
                     src.quote_hash, src.source_text_hash, src.locator, src.start_ms,
                     src.end_ms, src.page_no, src.slide_no, src.source_kind,
                     1 if src.presented_to_producer else 0, src.visibility.value,
                     json.dumps(list(src.visible_invocations), ensure_ascii=False),
                     src.failure_reason),
                )
                written_sources += 1
    return {"claims": written_claims, "sources": written_sources}


def project_legacy_evidence_links(domain_id: int, run_id: int) -> int:
    """把 Evidence V2 投影到 legacy ``evidence_links``（兼容，**只增不减历史**）。

    策略（任务书 §九）:

    * 不破坏、不删除任何历史 legacy 行（``owner_type`` 不是 ``content_claim`` 的
      行一律不碰 —— 旧 note/review 证据链完整保留）；
    * 只写**当前 run** 自己的行，用 ``owner_type='content_claim'`` +
      ``owner_id=content_claims.id`` 保证可追溯到 V2 claim；
    * ``chunk_id`` 取该 claim 绑定到的 span 的 ``source_chunk_id``（拿不到就 NULL）；
    * 重跑不重复：先按**本 run 上一轮投影过的 claim id** 清理，再写本轮。
      注意 ``persist_evidence`` 重建 content_claims 会换掉自增 id，因此必须先
      取回旧 id 集合再删除 —— 否则旧投影行会残留并累积（已复现的缺陷）。
    * Evidence V2 的 API 与审计**不反向依赖**旧表。
    """
    # 1) 清理上一轮投影（必须由调用方在重建 claims 之前完成，见 clear_legacy_projection）
    rows = db.fetch_all(
        "SELECT c.id AS claim_id, c.claim_type, c.evidence_status, s.source_id, "
        " s.bound_quote, s.locator, s.binding_status, sp.source_chunk_id "
        "FROM content_claims c "
        "LEFT JOIN claim_sources s ON s.claim_id = c.id "
        "LEFT JOIN source_spans sp ON sp.id = s.source_span_id "
        "WHERE c.run_id=? ORDER BY c.id, s.id", (int(run_id),))
    if not rows:
        return 0
    written = 0
    with db.transaction() as conn:
        for row in rows:
            verify_status = ("verified" if row["evidence_status"] == "passed"
                             else "not_required" if row["evidence_status"] == "not_required"
                             else "failed")
            conn.execute(
                "INSERT OR IGNORE INTO evidence_links (owner_type, owner_id, chunk_id, "
                " source_type, quote, locator, evidence_kind, verify_status, verify_method, "
                " model, prompt_version, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))",
                (LEGACY_OWNER_TYPE, int(row["claim_id"]), row["source_chunk_id"],
                 "course_source", row["bound_quote"] or "", row["locator"] or "",
                 row["claim_type"], verify_status, "evidence_v2_deterministic",
                 "", EVIDENCE_PROJECTION_VERSION),
            )
            written += 1
    return written


def clear_legacy_projection(run_id: int) -> int:
    """清理本 run 的 legacy 兼容投影（在重建 claims **之前**调用）。

    必须在 ``content_claims`` 重建前调用：重建会换掉自增 id，届时已无法用
    ``owner_id`` 反查旧投影行。只删 ``owner_type='content_claim'`` 且
    ``owner_id`` 属于本 run 的行，历史 legacy 证据链不受影响。
    """
    row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM evidence_links WHERE owner_type=? AND owner_id IN "
        "(SELECT id FROM content_claims WHERE run_id=?)",
        (LEGACY_OWNER_TYPE, int(run_id)))
    count = int((row or {}).get("n") or 0)
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM evidence_links WHERE owner_type=? AND owner_id IN "
            "(SELECT id FROM content_claims WHERE run_id=?)",
            (LEGACY_OWNER_TYPE, int(run_id)))
    return count


# ---------------------------------------------------------------------------
# 节点入口
# ---------------------------------------------------------------------------
def run_bind_evidence(domain_id: int, run_id: int) -> dict[str, Any]:
    """节点 11 bind_evidence 主体（local 节点，**不调用模型**）。"""
    drafts = project_claims(domain_id, run_id)
    bound = bind_claims(domain_id, run_id, drafts)
    # 顺序敏感：legacy 投影按 content_claims.id 定位，重建 claims 会换掉自增 id，
    # 因此必须在重建**之前**清理上一轮投影，否则旧行残留、重跑不断累积。
    cleared = clear_legacy_projection(run_id)
    counts = persist_evidence(domain_id, run_id, drafts, bound)
    # 报告从**已落库**的状态复算（与 API / coverage_audit 完全同一口径）
    report_dict = get_evidence_report(run_id)
    if report_dict is None:  # pragma: no cover - 刚落库即不可读
        raise EvidenceBindingError(f"run {run_id} 的证据报告落库后不可读")
    report = EvidenceReport.model_validate(report_dict)
    legacy_rows = project_legacy_evidence_links(domain_id, run_id)
    report.legacy_projection_rows = legacy_rows
    return {
        "enabled": True,
        "domain_id": int(domain_id),
        "run_id": int(run_id),
        "gate": report.gate.value,
        "degradation_reason": report.degradation_reason,
        "claims_total": report.claims_total,
        "required_claims": report.required_claims,
        "bound_claims": report.bound_claims,
        "failed_claims": report.failed_claims,
        "not_required_claims": report.not_required_claims,
        "critical_claims": report.critical_claims,
        "critical_claims_bound": report.critical_claims_bound,
        "critical_claim_evidence_rate": report.critical_claim_evidence_rate,
        "critical_evidence_denominator": report.critical_evidence_denominator,
        "invalid_source_refs": report.invalid_source_refs,
        "cross_domain_refs": report.cross_domain_refs,
        "unbound_source_refs": report.unbound_source_refs,
        "unread_source_refs": report.unread_source_refs,
        "claims_by_type": report.claims_by_type,
        "issues": report.issues,
        "persisted": counts,
        "legacy_projection_cleared": cleared,
        "legacy_projection_rows": legacy_rows,
        "report": json.loads(report.model_dump_json()),
    }


# ---------------------------------------------------------------------------
# 读取（供 API / 审计 / 测试）
# ---------------------------------------------------------------------------
def get_evidence_report(run_id: int) -> Optional[dict]:
    """从**持久化状态**复算 Evidence 报告（不信任任何缓存）。

    关键：报告基于 ``content_claims`` / ``claim_sources`` 里真实存在的行，而不是
    重新跑一遍投影。否则任何「表里存在但投影不再产出」的 claim（例如越界/伪造
    来源被写入，或投影口径变更后的陈旧行）都会被静默忽略，门禁就会假通过。

    作用域永远取当前 frozen MaterialDomain；没有 domain 就没有 V6 证据链
    （返回 ``None``，由调用方如实呈现「无 Evidence V2」而不是伪造空报告）。
    """
    dom = db.fetch_one("SELECT id FROM material_domains WHERE run_id=?", (int(run_id),))
    if not dom:
        return None
    domain_id = int(dom["id"])
    bound = load_bound_evidence(run_id)
    report = compute_evidence_report(run_id, domain_id, bound)
    legacy = db.fetch_one(
        "SELECT COUNT(*) AS n FROM evidence_links WHERE owner_type=? AND owner_id IN "
        "(SELECT id FROM content_claims WHERE run_id=?)",
        (LEGACY_OWNER_TYPE, int(run_id),))
    report.legacy_projection_rows = int((legacy or {}).get("n") or 0)
    return json.loads(report.model_dump_json())


def load_bound_evidence(run_id: int) -> list[BoundEvidence]:
    """从数据库还原逐 claim 的绑定终态（报告与审计的唯一真相源）。

    同时还原 Phase 4.1 的 producer provenance —— **绝不**用 run 级集合反推：
    provenance 落库时是什么就是什么，缺失则视为 ``UNAVAILABLE``（claim failed）。
    """
    claims = db.fetch_all(
        "SELECT id, claim_key, claim_type, importance, evidence_status, "
        " producer_provenance_json FROM content_claims WHERE run_id=? ORDER BY id",
        (int(run_id),))
    if not claims:
        return []
    ids = [int(c["id"]) for c in claims]
    q = ",".join("?" * len(ids))
    src_rows = db.fetch_all(
        f"SELECT * FROM claim_sources WHERE claim_id IN ({q}) ORDER BY claim_id, id",
        tuple(ids))
    by_claim: dict[int, list[dict]] = {}
    for row in src_rows:
        by_claim.setdefault(int(row["claim_id"]), []).append(dict(row))

    out: list[BoundEvidence] = []
    for row in claims:
        provenance = _provenance_from_row(row.get("producer_provenance_json"))
        sources = []
        reasons: list[str] = []
        for src in by_claim.get(int(row["id"]), []):
            reason = src.get("failure_reason")
            if reason:
                reasons.append(f"{reason}:{src.get('source_id')}")
            try:
                visible_invocations = [int(x) for x in
                                       json.loads(src.get("visible_invocations_json") or "[]")]
            except Exception:
                visible_invocations = []
            sources.append(BoundClaimSource(
                source_id=str(src["source_id"]),
                source_span_id=(int(src["source_span_id"])
                                if src.get("source_span_id") is not None else None),
                relation=src.get("relation") or ClaimSourceRelation.SUPPORTS.value,
                binding_status=src.get("binding_status") or ClaimBindingStatus.MISSING.value,
                binding_method=(src.get("binding_method")
                                or ClaimBindingMethod.DETERMINISTIC_SOURCE_ID.value),
                bound_quote=src.get("bound_quote") or "",
                quote_hash=src.get("quote_hash") or "",
                source_text_hash=src.get("source_text_hash") or "",
                locator=src.get("locator"),
                start_ms=src.get("start_ms"),
                end_ms=src.get("end_ms"),
                page_no=src.get("page_no"),
                slide_no=src.get("slide_no"),
                source_kind=src.get("source_kind"),
                presented_to_producer=bool(src.get("presented_to_producer")),
                visibility=src.get("visibility") or ClaimSourceVisibility.UNKNOWN.value,
                visible_invocations=visible_invocations,
                failure_reason=reason,
            ))
        status = row.get("evidence_status") or ClaimEvidenceStatus.PENDING.value
        if status == ClaimEvidenceStatus.FAILED.value and not reasons:
            reasons.append("claim_failed_without_recorded_source_failure")
        out.append(BoundEvidence(
            claim_key=str(row["claim_key"]),
            claim_id=int(row["id"]),
            claim_type=row["claim_type"],
            importance=row.get("importance") or ClaimImportance.SUPPORTING.value,
            evidence_status=status,
            provenance=provenance,
            sources=sources,
            failure_reasons=sorted(set(reasons)),
        ))
    return out


def _provenance_from_row(raw: Any) -> ClaimProvenance:
    """把落库的 provenance JSON 还原为契约；损坏/缺失 → ``UNAVAILABLE``。"""
    if not raw:
        return ClaimProvenance(
            producer_kind=ProducerKind.LESSON_UNDERSTANDING,
            visibility_basis=VisibilityBasis.UNAVAILABLE,
            provenance_version=EVIDENCE_PROVENANCE_VERSION)
    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        logger.warning("producer_provenance_json 无法解析，按 UNAVAILABLE 处理")
        return ClaimProvenance(
            producer_kind=ProducerKind.LESSON_UNDERSTANDING,
            visibility_basis=VisibilityBasis.UNAVAILABLE,
            provenance_version=EVIDENCE_PROVENANCE_VERSION)
    try:
        return ClaimProvenance.model_validate(payload)
    except Exception as e:  # noqa: BLE001
        logger.warning("producer_provenance_json 不符合契约，按 UNAVAILABLE 处理: %s", e)
        return ClaimProvenance(
            producer_kind=ProducerKind.LESSON_UNDERSTANDING,
            visibility_basis=VisibilityBasis.UNAVAILABLE,
            provenance_version=EVIDENCE_PROVENANCE_VERSION)


def get_claims(run_id: int) -> list[dict]:
    """读取 claim 及其逐来源绑定（供只读 API；不含 prompt / reasoning / 路径）。"""
    claims = [dict(r) for r in db.fetch_all(
        "SELECT id, run_id, domain_id, claim_key, claim_type, claim_text, importance, "
        " producer_node, evidence_status, input_hash, content_hash, "
        " producer_provenance_json, created_at, updated_at "
        "FROM content_claims WHERE run_id=? ORDER BY claim_type, id", (int(run_id),))]
    if not claims:
        return []
    ids = [int(c["id"]) for c in claims]
    q = ",".join("?" * len(ids))
    src_rows = db.fetch_all(
        f"SELECT claim_id, source_id, source_span_id, relation, binding_status, "
        f" binding_method, bound_quote, quote_hash, source_text_hash, locator, start_ms, "
        f" end_ms, page_no, slide_no, source_kind, presented_to_producer, visibility, "
        f" visible_invocations_json, failure_reason "
        f"FROM claim_sources WHERE claim_id IN ({q}) ORDER BY claim_id, id", tuple(ids))
    by_claim: dict[int, list[dict]] = {}
    for row in src_rows:
        item = dict(row)
        item["presented_to_producer"] = bool(item.get("presented_to_producer"))
        try:
            item["visible_invocations"] = [
                int(x) for x in json.loads(item.pop("visible_invocations_json") or "[]")]
        except Exception:
            item["visible_invocations"] = []
        by_claim.setdefault(int(item.pop("claim_id")), []).append(item)
    for claim in claims:
        claim["sources"] = by_claim.get(int(claim["id"]), [])
        claim["requires_source"] = claim["claim_type"] in {
            t.value for t in CLAIM_TYPES_REQUIRING_SOURCE}
        claim["is_ai_explanation"] = claim["claim_type"] == ClaimType.AI_EXPLANATION.value
        provenance = _provenance_from_row(claim.pop("producer_provenance_json", None))
        # 只暴露安全摘要：不含 prompt / 模型上下文
        claim["provenance"] = {
            "producer_stage": provenance.producer_stage,
            "producer_node": provenance.producer_node,
            "producer_kind": provenance.producer_kind.value,
            "invocation_refs": list(provenance.invocation_refs),
            "visible_source_ids": list(provenance.visible_source_ids),
            "visibility_basis": provenance.visibility_basis.value,
            "provenance_version": provenance.provenance_version,
            "per_source_invocations": dict(provenance.per_source_invocations),
            "source_scope": "claim_producer_invocations",
            "available": provenance.visibility_basis != VisibilityBasis.UNAVAILABLE,
        }
    return claims
