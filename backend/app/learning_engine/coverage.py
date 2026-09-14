"""Coverage Plan / Ledger / Auditor（V6 Phase 1）。

设计约束（任务书 §六 + 设计文档 §4.4）:

* 覆盖率由**确定性本地程序**计算，禁止模型填写，也禁止调用方直接伪造
  （``record_coverage_audit`` 只接受 run_id / domain_id，所有指标由 SQL 派生）。
* ``unassigned_count > 0`` 不得通过覆盖门禁。
* ``silent_dropped > 0``、``domain_accounting_rate < 1.0``、存在未分配 required
  item/span ⇒ 运行不得写 ``completed``。
* Phase 1 尚无 segment understanding，因此显式区分:
    - 材料/Source Map 记账覆盖率（map 级，Phase 1 应达 100%）
    - 语义处理覆盖率（semantic 级，Phase 1 诚实反映旧链实际使用量）
  绝不把 ``semantic_processing_rate`` 伪造成 100%。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from .. import database as db
from .normalize import timeline_stats
from . import config as engine_config
from .contracts import (
    DEFAULT_SEMANTIC_RATE_FLOOR,
    SCHEMA_VERSION,
    Gate,
    LedgerOutcome,
    LedgerStage,
    MaterialCoverageReport,
    PlanStrategy,
    SourceKind,
    SpanState,
)

logger = logging.getLogger(__name__)

#: 模型上下文预算预留（无模型档案时的保守默认值）。
DEFAULT_CONTEXT_WINDOW = 32_768
DEFAULT_RESERVED_OUTPUT = 4_096
DEFAULT_RESERVED_SYSTEM = 2_048
SAFETY_MARGIN = 0.10

#: 未使用原因受控集合（必须与 contracts.NotUsedReason 保持一致）。
REASON_LEGACY_TOP_K = "not_selected_by_legacy_top_k"
REASON_BUDGET_TRUNCATED = "budget_truncated"
REASON_OUT_OF_CONTEXT = "out_of_context_window"
REASON_SEGMENT_NOT_REACHED = "segment_not_reached"

#: **Phase 2 硬门禁**：语义处理率固定要求 100%。
#: 不接受任何环境变量把阈值调低（缺陷 4 / 任务书 §八）：Phase 2 已实现全量
#: segment understanding，任何 canonical 非噪声 span 未进入理解都必须 degraded。
SEMANTIC_RATE_FLOOR = 1.0


def semantic_rate_floor() -> float:
    """语义处理率下限。Phase 2 起恒为 ``SEMANTIC_RATE_FLOOR``（1.0）。

    保留函数名以免调用方散落；但**不再读取环境变量**，避免通过
    ``V6_SEMANTIC_RATE_FLOOR`` 之类开关放宽门禁。
    """
    return SEMANTIC_RATE_FLOOR


# ---------------------------------------------------------------------------
# Coverage Plan
# ---------------------------------------------------------------------------
def _model_context_window(model_profile_id: Optional[str]) -> tuple[int, int]:
    """读取模型上下文能力。查不到时使用保守默认值（不猜测放大预算）。"""
    if model_profile_id:
        row = db.fetch_one(
            "SELECT context_window FROM model_profiles WHERE id=?", (model_profile_id,)
        )
        if row and row.get("context_window"):
            try:
                return int(row["context_window"]), DEFAULT_RESERVED_OUTPUT
            except (TypeError, ValueError):
                pass
    return DEFAULT_CONTEXT_WINDOW, DEFAULT_RESERVED_OUTPUT


def build_coverage_plan(domain_id: int, *, model_profile_id: Optional[str] = None,
                        context_window: Optional[int] = None) -> dict[str, Any]:
    """生成**确定性**覆盖计划。

    Phase 1 的 Planner 只做两件事：统计 canonical 非噪声 span 的 token 总量，
    并判断这些 span 能否整体容纳在输入预算内。它**不做相关性淘汰**：
    任何 span 都不会因为「看起来不重要」而被丢掉（设计文档 §10.7）。
    """
    spans = db.fetch_all(
        "SELECT id, source_id, ordinal, token_count FROM source_spans "
        "WHERE domain_id=? AND span_state=? ORDER BY ordinal, id",
        (domain_id, SpanState.INCLUDED.value),
    )
    window, reserved_output = _model_context_window(model_profile_id)
    if context_window:
        window = int(context_window)
    reserved_system = DEFAULT_RESERVED_SYSTEM
    usable = max(int(window * (1.0 - SAFETY_MARGIN)) - reserved_system - reserved_output, 0)
    input_budget = usable
    output_budget = reserved_output

    planned = [
        {"span_id": int(s["id"]), "source_id": s["source_id"],
         "ordinal": int(s["ordinal"]), "tokens": int(s["token_count"] or 0)}
        for s in spans
    ]
    total_tokens = sum(p["tokens"] for p in planned)
    assignments: dict[str, int] = {}
    unassigned: list[str] = []
    if total_tokens <= input_budget:
        # 全部 canonical span 可整体容纳 → single_pass。
        # 注意 input_budget<=0（模型上下文极小/配置异常）不能走这条捷径：
        # 那意味着什么都放不下，必须落入分段逻辑并把 span 记为未分配。
        strategy = PlanStrategy.SINGLE_PASS
        segment_count = 1
        for p in planned:
            assignments[p["source_id"]] = 1
    else:
        # 顺序分段（保序、每段不超过预算）。若单段预算连最小 span 都放不下，
        # 该 span 记为未分配 —— 计划不可用，覆盖门禁必须阻断。
        strategy = PlanStrategy.SEGMENTED_MAP_MERGE
        segment_count = 0
        budget_left = input_budget
        for p in planned:
            tokens = p["tokens"]
            if tokens > input_budget:
                unassigned.append(p["source_id"])
                continue
            if segment_count == 0 or tokens > budget_left:
                segment_count += 1
                budget_left = input_budget
            assignments[p["source_id"]] = segment_count
            budget_left -= tokens
        segment_count = max(segment_count, 1)

    plan_json = json.dumps({
        "schema_version": SCHEMA_VERSION,
        "strategy": strategy.value,
        "segment_count": segment_count,
        "assignments": assignments,
        "total_tokens": total_tokens,
        "safety_margin": SAFETY_MARGIN,
        "planner": "deterministic_sequential_v1",
        "note": "Planner 只分段不淘汰；未分配 span 必须阻断覆盖门禁",
    }, ensure_ascii=False, sort_keys=True)

    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT id FROM coverage_plans WHERE domain_id=?", (domain_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE coverage_plans SET strategy=?, model_profile_id=?, context_window=?, "
                " reserved_output_tokens=?, reserved_system_tokens=?, input_budget_tokens=?, "
                " output_budget_tokens=?, planned_span_count=?, unassigned_count=?, plan_json=? "
                "WHERE domain_id=?",
                (strategy.value, model_profile_id, window, reserved_output, reserved_system,
                 input_budget, output_budget, len(planned), len(unassigned), plan_json, domain_id),
            )
            plan_id = int(existing["id"])
        else:
            cur = conn.execute(
                "INSERT INTO coverage_plans (run_id, domain_id, strategy, model_profile_id, "
                " context_window, reserved_output_tokens, reserved_system_tokens, "
                " input_budget_tokens, output_budget_tokens, planned_span_count, "
                " unassigned_count, plan_json, created_at) "
                "SELECT run_id, id, ?,?,?,?,?,?,?,?,?,?, datetime('now','localtime') "
                "FROM material_domains WHERE id=?",
                (strategy.value, model_profile_id, window, reserved_output, reserved_system,
                 input_budget, output_budget, len(planned), len(unassigned), plan_json, domain_id),
            )
            plan_id = int(cur.lastrowid)

    return {
        "plan_id": plan_id,
        "domain_id": domain_id,
        "strategy": strategy.value,
        "model_profile_id": model_profile_id,
        "context_window": window,
        "input_budget_tokens": input_budget,
        "output_budget_tokens": output_budget,
        "planned_span_count": len(planned),
        "segment_count": segment_count,
        "unassigned_count": len(unassigned),
        "unassigned_source_ids": unassigned,
        "assignments": assignments,
        "total_tokens": total_tokens,
    }


def get_plan(domain_id: int) -> Optional[dict]:
    row = db.fetch_one("SELECT * FROM coverage_plans WHERE domain_id=?", (domain_id,))
    return dict(row) if row else None


def get_plan_assignments(domain_id: int) -> dict[str, int]:
    row = get_plan(domain_id)
    if not row:
        return {}
    try:
        return dict(json.loads(row.get("plan_json") or "{}").get("assignments") or {})
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Coverage Ledger
# ---------------------------------------------------------------------------
def record_ledger_entry(*, run_id: int, domain_id: int, source_span_id: int, source_id: str,
                        stage: str, outcome: str, reason_code: Optional[str] = None,
                        reason_detail: Optional[str] = None,
                        segment_id: Optional[int] = None,
                        knowledge_unit_id: Optional[int] = None) -> None:
    """写入一条账目（同一 span + stage 幂等：重复写入更新既有行）。"""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO coverage_ledger (run_id, domain_id, source_span_id, source_id, stage, "
            " outcome, reason_code, reason_detail, segment_id, knowledge_unit_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now','localtime')) "
            "ON CONFLICT(domain_id, source_span_id, stage) DO UPDATE SET "
            " outcome=excluded.outcome, reason_code=excluded.reason_code, "
            " reason_detail=excluded.reason_detail, segment_id=excluded.segment_id, "
            " knowledge_unit_id=excluded.knowledge_unit_id",
            (run_id, domain_id, source_span_id, source_id, stage, outcome, reason_code,
             reason_detail, segment_id, knowledge_unit_id),
        )


def mark_map_ledger(domain_id: int, run_id: int,
                    omit_span_ids: Optional[set[int]] = None) -> dict[str, int]:
    """为 Source Map 全部 span 写 ``stage='source_map'`` 账目。

    这是 Phase 1 的**记账覆盖**：每个 span 无论状态如何都必须有一条终态账目。

    ``omit_span_ids`` 只用于故障注入 / 证伪测试：故意不为这些 span 记账，从而
    制造真正的 **silent drop**（canonical 非噪声 span 既无账目又无原因）。
    正常流程不得传参。
    """
    rows = db.fetch_all(
        "SELECT id, source_id, span_state, reason_code, reason_detail FROM source_spans "
        "WHERE domain_id=? ORDER BY ordinal, id",
        (domain_id,),
    )
    omit = omit_span_ids or set()
    outcome_by_state = {
        SpanState.INCLUDED.value: LedgerOutcome.PROCESSED.value,
        SpanState.DUPLICATE.value: LedgerOutcome.DUPLICATE.value,
        SpanState.NOISE.value: LedgerOutcome.NOISE.value,
        SpanState.UNSUPPORTED.value: LedgerOutcome.UNSUPPORTED.value,
        SpanState.FAILED.value: LedgerOutcome.FAILED.value,
        SpanState.EXCLUDED_WITH_REASON.value: LedgerOutcome.EXCLUDED.value,
    }
    counts: dict[str, int] = {}
    for row in rows:
        if int(row["id"]) in omit:
            counts["omitted"] = counts.get("omitted", 0) + 1
            continue
        state = row["span_state"]
        outcome = outcome_by_state.get(state)
        if outcome is None:  # pragma: no cover - 受 CHECK 约束保护
            continue
        record_ledger_entry(
            run_id=run_id, domain_id=domain_id, source_span_id=int(row["id"]),
            source_id=row["source_id"], stage=LedgerStage.SOURCE_MAP.value,
            outcome=outcome, reason_code=row.get("reason_code"),
            reason_detail=row.get("reason_detail"),
        )
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def mark_legacy_processing(domain_id: int, run_id: int,
                           processed_source_ids: set[str]) -> dict[str, int]:
    """把旧生成链**实际消费**的 span 记为 ``understand_segments/processed``。

    ``processed_source_ids`` 必须来自旧链真实使用的 chunk（retrieve_context
    candidates / lesson_outline 输入），由 dag_lesson 提供；其余 canonical 非噪声
    span 记为 ``not_used`` + ``not_selected_by_legacy_top_k``。

    诚实原则：这里只记录**真实发生**的消费，不因为「希望覆盖率高」而放宽。
    """
    rows = db.fetch_all(
        "SELECT id, source_id FROM source_spans WHERE domain_id=? AND span_state=? "
        "ORDER BY ordinal, id",
        (domain_id, SpanState.INCLUDED.value),
    )
    processed = not_used = 0
    for row in rows:
        source_id = row["source_id"]
        if source_id in processed_source_ids:
            record_ledger_entry(
                run_id=run_id, domain_id=domain_id, source_span_id=int(row["id"]),
                source_id=source_id, stage=LedgerStage.UNDERSTAND_SEGMENTS.value,
                outcome=LedgerOutcome.PROCESSED.value,
            )
            processed += 1
        else:
            record_ledger_entry(
                run_id=run_id, domain_id=domain_id, source_span_id=int(row["id"]),
                source_id=source_id, stage=LedgerStage.UNDERSTAND_SEGMENTS.value,
                outcome=LedgerOutcome.NOT_USED.value,
                reason_code=REASON_LEGACY_TOP_K,
                reason_detail="旧生成链候选窗口（运行内 chunk 上限 / RAG top_k）未选中",
            )
            not_used += 1
    return {"processed": processed, "not_used": not_used}


def get_ledger(domain_id: int, stage: Optional[str] = None) -> list[dict]:
    if stage:
        rows = db.fetch_all(
            "SELECT * FROM coverage_ledger WHERE domain_id=? AND stage=? ORDER BY id",
            (domain_id, stage),
        )
    else:
        rows = db.fetch_all(
            "SELECT * FROM coverage_ledger WHERE domain_id=? ORDER BY id", (domain_id,)
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Auditor（全部指标由 SQL 派生，不接受调用方传入）
# ---------------------------------------------------------------------------
def _ratio(num: int, den: int) -> float:
    """比率。**分母为 0 时返回 0.0，不返回 1.0**（缺陷 4）。

    空分母永远不能被解释为「处理成功」：没有材料就没有任何东西被理解，
    把它当成 100% 会让「必需材料一个 chunk 都没有」的运行显示 passed。
    需要「无分母即视为健康」的记账类指标请显式使用 ``_ratio_accounting``。
    """
    if den <= 0:
        return 0.0
    return round(num / den, 6)


def _ratio_accounting(num: int, den: int) -> float:
    """记账类比率：空集合视为 100%（没有成员需要记账 ⇒ 记账完备）。"""
    if den <= 0:
        return 1.0
    return round(num / den, 6)


def compute_coverage(domain_id: int) -> dict[str, Any]:
    """只读计算覆盖指标。不写库，可供 benchmark 直接复用。

    两类口径严格分开：

    * **记账覆盖**（map 级）：每个 span/item 是否有终态账目 → ``*_accounting_*``
    * **语义处理覆盖**（Phase 2）：canonical 非噪声 span 是否真的进入了
      ``understand_segments`` → ``semantic_processing_rate``
    """
    domain = db.fetch_one("SELECT * FROM material_domains WHERE id=?", (domain_id,))
    if not domain:
        raise ValueError(f"domain {domain_id} 不存在")
    run_id = int(domain["run_id"])

    item_rows = db.fetch_all(
        "SELECT id, state, reason_code, required FROM material_domain_items "
        "WHERE domain_id=? ORDER BY ordinal, id",
        (domain_id,),
    )
    total_items = len(item_rows)
    accounted_items = 0
    for row in item_rows:
        state = row["state"]
        if state in (SpanState.INCLUDED.value, SpanState.DUPLICATE.value):
            accounted_items += 1
        elif row.get("reason_code"):
            accounted_items += 1
    unique_items = sum(1 for r in item_rows if r["state"] != "duplicate")

    span_where = "domain_id=?"
    span_params = (domain_id,)
    state_counts = {
        r["span_state"]: int(r["n"]) for r in db.fetch_all(
            f"SELECT span_state, COUNT(*) AS n FROM source_spans WHERE {span_where} "
            f"GROUP BY span_state", span_params)
    }
    total_spans = sum(state_counts.values())
    canonical = state_counts.get(SpanState.INCLUDED.value, 0)
    duplicates = state_counts.get(SpanState.DUPLICATE.value, 0)
    noise = state_counts.get(SpanState.NOISE.value, 0)

    # 语义处理：**只认 understand_segments/processed**。
    # 仅写 source_map/processed 不算语义处理；not_used_with_reason 能避免
    # silent drop，但不能提高 semantic_processing_rate。
    processed_span_ids = {
        int(r["source_span_id"]) for r in db.fetch_all(
            "SELECT DISTINCT source_span_id FROM coverage_ledger "
            "WHERE domain_id=? AND stage=? AND outcome=? AND source_span_id IS NOT NULL",
            (domain_id, LedgerStage.UNDERSTAND_SEGMENTS.value, LedgerOutcome.PROCESSED.value),
        )
    }
    canonical_rows = db.fetch_all(
        "SELECT id, source_id FROM source_spans WHERE domain_id=? AND span_state=? "
        "ORDER BY ordinal, id",
        (domain_id, SpanState.INCLUDED.value),
    )
    canonical_ids = {int(r["id"]) for r in canonical_rows}
    processed = len(processed_span_ids & canonical_ids)

    # 旧链候选消费（独立 stage，只用于对照，不参与语义率）
    legacy_span_ids = {
        int(r["source_span_id"]) for r in db.fetch_all(
            "SELECT DISTINCT source_span_id FROM coverage_ledger "
            "WHERE domain_id=? AND stage=? AND outcome=? AND source_span_id IS NOT NULL",
            (domain_id, LedgerStage.LEGACY_CANDIDATE.value, LedgerOutcome.PROCESSED.value),
        )
    }
    legacy_processed = len(legacy_span_ids & canonical_ids)

    # silent drop：canonical 非噪声 span 完全没有账目
    ledger_span_ids = {
        int(r["source_span_id"]) for r in db.fetch_all(
            "SELECT DISTINCT source_span_id FROM coverage_ledger "
            "WHERE domain_id=? AND source_span_id IS NOT NULL",
            (domain_id,),
        )
    }
    silent_ids = sorted(canonical_ids - ledger_span_ids)
    silent_id_set = set(silent_ids)
    silent_source_ids = [r["source_id"] for r in canonical_rows
                         if int(r["id"]) in silent_id_set]

    plan = get_plan(domain_id)
    unassigned_count = int(plan["unassigned_count"]) if plan else 0
    planned_span_count = int(plan["planned_span_count"]) if plan else canonical

    # ---- required 材料专项指标（缺陷 4 核心）----
    required_rows = [r for r in item_rows if r.get("required")]
    required_total = len(required_rows)
    required_failed = sum(1 for r in required_rows if r["state"] == "failed")
    required_unsupported = sum(1 for r in required_rows if r["state"] == "unsupported")
    required_excluded = sum(1 for r in required_rows
                            if r["state"] == "excluded_with_reason")
    # 「required 材料没有任何 canonical span」= 该材料下不存在 span_state='included' 的 span
    required_without_canonical = []
    for item in required_rows:
        if item["state"] == "duplicate":
            continue  # 重复材料由 canonical 材料代表，不算缺失
        # 「有 canonical 表示」= 该材料名下存在 canonical span，**或**存在指向
        # canonical 的 duplicate span（同一内容被两条来源引用时，后者全部记为
        # duplicate 但内容确实进入了理解）。
        has_canonical = db.fetch_one(
            "SELECT 1 AS ok FROM source_spans s "
            "WHERE s.domain_item_id=? AND ("
            "  s.span_state='included' "
            "  OR (s.span_state='duplicate' AND EXISTS ("
            "        SELECT 1 FROM source_spans c "
            "        WHERE c.id = s.canonical_span_id AND c.span_state='included'))) "
            "LIMIT 1",
            (int(item["id"]),),
        )
        if not has_canonical:
            required_without_canonical.append(int(item["id"]))
    # required 材料名下「已建图但没有语义处理」的 canonical span
    required_span_rows = db.fetch_all(
        "SELECT s.id FROM source_spans s JOIN material_domain_items i "
        " ON i.id = s.domain_item_id "
        f"WHERE s.domain_id=? AND s.span_state=? AND i.required=1",
        (domain_id, SpanState.INCLUDED.value),
    )
    required_canonical_ids = {int(r["id"]) for r in required_span_rows}
    required_spans_unprocessed = len(required_canonical_ids - processed_span_ids)

    # 时间轴：转写来源 span（材料 kind 或 chunk type 任一为 transcript 即计入）
    timeline_rows = db.fetch_all(
        "SELECT s.id, s.start_ms, s.end_ms FROM source_spans s "
        "LEFT JOIN materials m ON m.id = s.material_id "
        "WHERE s.domain_id=? AND s.span_state=? "
        " AND (m.kind='transcript' OR m.type='transcript' OR s.source_kind='transcript') "
        "ORDER BY s.ordinal, s.id",
        (domain_id, SpanState.INCLUDED.value),
    )
    timeline_total = len(timeline_rows)
    timeline_processed = sum(1 for r in timeline_rows if int(r["id"]) in processed_span_ids)
    timeline_starts = [r["start_ms"] for r in timeline_rows if r["start_ms"] is not None]
    timeline_ends = [r["end_ms"] for r in timeline_rows if r["end_ms"] is not None]
    timeline_stats_data = timeline_stats(timeline_starts, timeline_ends)

    # PPT 页覆盖
    ppt_rows = db.fetch_all(
        "SELECT id, slide_no, page_no FROM source_spans WHERE domain_id=? AND source_kind=? "
        "AND span_state=? ORDER BY ordinal, id",
        (domain_id, SourceKind.PPT.value, SpanState.INCLUDED.value),
    )
    ppt_pages_all = {(r["slide_no"] or r["page_no"]) for r in ppt_rows
                     if (r["slide_no"] or r["page_no"]) is not None}
    ppt_pages_done = {(r["slide_no"] or r["page_no"]) for r in ppt_rows
                      if (r["slide_no"] or r["page_no"]) is not None
                      and int(r["id"]) in processed_span_ids}

    # source refs：canonical 非噪声 span 作为引用全集，processed 为有效引用
    refs_total = len(canonical_ids)
    refs_valid = processed
    refs_invalid = refs_total - refs_valid

    unprocessed_reasons = [
        dict(r) for r in db.fetch_all(
            "SELECT reason_code, stage, COUNT(*) AS n FROM coverage_ledger "
            "WHERE domain_id=? AND outcome IN ('not_used','noise','duplicate','unsupported','failed','excluded') "
            "GROUP BY reason_code, stage ORDER BY n DESC, reason_code",
            (domain_id,),
        )
    ]

    # segment 侧（Phase 2）：primary segment 是否全部成功
    segment_stats = _segment_stats(domain_id, run_id)

    return {
        "run_id": run_id,
        "domain_id": domain_id,
        "total_domain_items": total_items,
        "unique_domain_items": unique_items,
        "terminal_domain_items": accounted_items,
        "unaccounted_domain_items": total_items - accounted_items,
        "total_source_spans": total_spans,
        "canonical_non_noise_spans": canonical,
        "duplicate_spans": duplicates,
        "noise_spans": noise,
        "unsupported_spans": state_counts.get(SpanState.UNSUPPORTED.value, 0),
        "failed_spans": state_counts.get(SpanState.FAILED.value, 0),
        "excluded_spans": state_counts.get(SpanState.EXCLUDED_WITH_REASON.value, 0),
        "planned_span_count": planned_span_count,
        "unassigned_count": unassigned_count,
        "processed_spans": processed,
        "silent_dropped": len(silent_ids),
        "silent_dropped_source_ids": silent_source_ids,
        # required 专项
        "required_items_total": required_total,
        "required_items_failed": required_failed,
        "required_items_unsupported": required_unsupported,
        "required_items_excluded": required_excluded,
        "required_items_without_canonical_span": len(required_without_canonical),
        "required_items_without_canonical_span_ids": required_without_canonical,
        "required_spans_unprocessed": required_spans_unprocessed,
        # 比率
        "domain_accounting_rate": _ratio_accounting(accounted_items, total_items),
        "semantic_processing_rate": _ratio(processed, canonical),
        "timeline_coverage_rate": _ratio(timeline_processed, timeline_total),
        "legacy_candidate_rate": _ratio(legacy_processed, canonical),
        "legacy_candidate_spans": legacy_processed,
        # 时间轴 / PPT
        "timeline_span_total": timeline_total,
        "timeline_span_processed": timeline_processed,
        "timeline_start_ms": timeline_stats_data["start_ms"],
        "timeline_end_ms": timeline_stats_data["end_ms"],
        "timeline_gap_count": timeline_stats_data["gap_count"],
        "timeline_gap_ms": timeline_stats_data["gap_ms"],
        "ppt_pages_total": len(ppt_pages_all),
        "ppt_pages_processed": len(ppt_pages_done),
        # refs
        "source_refs_total": refs_total,
        "source_refs_valid": refs_valid,
        "source_refs_invalid": refs_invalid,
        # segments
        **segment_stats,
        "silent_drop_span_ids": silent_ids,
        "unprocessed_reasons": unprocessed_reasons,
    }


def _segment_stats(domain_id: int, run_id: int) -> dict[str, Any]:
    """Phase 2 segment 统计。表不存在（v17 库）时返回零值，不抛错。"""
    empty = {
        "segment_count": 0, "primary_segment_count": 0, "segment_failed_count": 0,
        "segment_pending_count": 0, "segments_consumed_by_merge": 0,
        "merge_consumed_all_segments": False,
    }
    if not _table_exists("lesson_segments"):
        return empty
    rows = db.fetch_all(
        "SELECT status, COUNT(*) AS n FROM lesson_segments WHERE domain_id=? "
        "GROUP BY status", (domain_id,))
    counts = {r["status"]: int(r["n"]) for r in rows}
    total = sum(counts.values())
    merge_row = db.fetch_one(
        "SELECT consumed_segment_count, segment_count, structured_json FROM lesson_understandings "
        "WHERE run_id=?", (run_id,)) if _table_exists("lesson_understandings") else None
    consumed = int(merge_row["consumed_segment_count"] or 0) if merge_row else 0
    expected = int(merge_row["segment_count"] or 0) if merge_row else total
    structures = {"segment_structures_total": 0, "merged_structures_total": 0,
                  "structures_dropped": 0}
    if merge_row:
        try:
            payload = json.loads(merge_row.get("structured_json") or "{}")
            for key in structures:
                structures[key] = int(payload.get(key) or 0)
        except Exception:
            pass
    return {
        "segment_count": total,
        "primary_segment_count": total,
        "segment_failed_count": counts.get("failed", 0),
        "segment_pending_count": counts.get("pending", 0) + counts.get("running", 0),
        "segments_consumed_by_merge": consumed,
        "merge_consumed_all_segments": bool(merge_row) and consumed >= expected and expected > 0,
        **structures,
    }


def _table_exists(name: str) -> bool:
    row = db.fetch_one(
        "SELECT 1 AS ok FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return bool(row)


def evaluate_gate(metrics: dict[str, Any]) -> tuple[str, Optional[str], list[str]]:
    """Phase 2 硬门禁判定。返回 ``(gate, degradation_reason, issues)``。

    Phase 2 起 ``semantic_processing_rate`` **固定要求 1.0**，不再接受任何
    环境变量把阈值调低（缺陷 4 + 任务书 §八）。

    规则:

    * ``silent_dropped > 0`` → failed
    * ``domain_accounting_rate < 1.0`` → failed
    * ``required_items_failed/unsupported > 0`` → failed
    * ``required_items_without_canonical_span > 0`` → failed
    * ``canonical_non_noise_spans == 0`` → degraded（没有任何可理解内容）
    * ``unassigned_count > 0`` → failed（计划未覆盖全部 span）
    * ``semantic_processing_rate < 1.0`` → degraded
    * ``required_spans_unprocessed > 0`` → degraded
    * ``source_refs_invalid > 0`` → degraded
    * primary segment 未全部成功 / merge 未消费全部 segment → degraded
    """
    issues: list[str] = []
    blocking: list[str] = []
    degraded: list[str] = []

    silent = int(metrics.get("silent_dropped") or 0)
    if silent > 0:
        issues.append(f"silent_dropped={silent}（canonical 非噪声 span 缺少任何账目）")
        blocking.append(f"silent_dropped={silent}")

    accounting = float(metrics.get("domain_accounting_rate") or 0.0)
    if accounting < 1.0:
        blocking.append(
            f"domain_accounting_rate={accounting}"
            f"（{int(metrics.get('unaccounted_domain_items') or 0)} 个 domain item 无终态）"
        )

    req_failed = int(metrics.get("required_items_failed") or 0)
    req_unsupported = int(metrics.get("required_items_unsupported") or 0)
    if req_failed > 0:
        blocking.append(f"required_items_failed={req_failed}")
    if req_unsupported > 0:
        blocking.append(f"required_items_unsupported={req_unsupported}")

    req_no_canonical = int(metrics.get("required_items_without_canonical_span") or 0)
    if req_no_canonical > 0:
        blocking.append(
            f"required_items_without_canonical_span={req_no_canonical}"
            "（必需材料没有任何可理解的 canonical span）"
        )

    unassigned = int(metrics.get("unassigned_count") or 0)
    if unassigned > 0:
        blocking.append(f"unassigned_count={unassigned}（required span 未分配到任何 segment）")

    if blocking:
        return Gate.FAILED.value, "；".join(blocking), issues + blocking

    canonical = int(metrics.get("canonical_non_noise_spans") or 0)
    if canonical == 0:
        # 全部分母为 0：绝不能被当成 100% 处理成功。
        degraded.append("canonical_non_noise_spans=0（没有任何可理解的 source span）")

    semantic = float(metrics.get("semantic_processing_rate") or 0.0)
    if semantic + 1e-9 < SEMANTIC_RATE_FLOOR:
        degraded.append(
            f"semantic_processing_rate={semantic}<{SEMANTIC_RATE_FLOOR}"
            f"（{int(metrics.get('processed_spans') or 0)}/{canonical} 个 canonical span 进入理解）"
        )

    req_unprocessed = int(metrics.get("required_spans_unprocessed") or 0)
    if req_unprocessed > 0:
        degraded.append(f"required_spans_unprocessed={req_unprocessed}")

    refs_invalid = int(metrics.get("source_refs_invalid") or 0)
    if refs_invalid > 0:
        degraded.append(f"source_refs_invalid={refs_invalid}")

    seg_failed = int(metrics.get("segment_failed_count") or 0)
    if seg_failed > 0:
        degraded.append(f"segment_failed_count={seg_failed}（存在未成功的 primary segment）")

    if metrics.get("segment_count") and not metrics.get("merge_consumed_all_segments"):
        degraded.append(
            f"merge 未消费全部 segment"
            f"（{int(metrics.get('segments_consumed_by_merge') or 0)}/"
            f"{int(metrics.get('segment_count') or 0)}）"
        )

    # 结构级 coverage：任何定义/公式/推导/例题在 merge 中丢失都必须阻断
    structures_dropped = int(metrics.get("structures_dropped") or 0)
    if structures_dropped > 0:
        degraded.append(
            f"structures_dropped={structures_dropped}"
            f"（segment 结构块合计 {int(metrics.get('segment_structures_total') or 0)}，"
            f"合并后消费 {int(metrics.get('merged_structures_total') or 0)}）"
        )

    if degraded:
        return Gate.DEGRADED.value, "；".join(degraded), issues + degraded
    return Gate.PASSED.value, None, issues


def _record_coverage_audit(domain_id: int, report_run_id: int | None) -> MaterialCoverageReport:
    """计算覆盖指标、判定门禁并写入 ``coverage_reports``。

    指标全部由 ``compute_coverage`` 从数据库派生；本函数**不接受**任何指标入参，
    因此调用方无法伪造覆盖率。
    """
    metrics = compute_coverage(domain_id)
    gate, reason, issues = evaluate_gate(metrics)

    # 不可变重试可以复用父 run 的 Material Domain。报告属于本次执行，不能
    # 因 domain.run_id 指向父任务而再次写回父任务，否则子任务即使全部节点
    # 成功也会因缺少自己的 coverage_report 被 fail-closed 降级。
    report_run_id = int(report_run_id if report_run_id is not None else metrics["run_id"])
    report = MaterialCoverageReport(
        run_id=report_run_id,
        domain_id=int(domain_id),
        engine_version=engine_config.engine_version(),
        engine_mode=engine_config.engine_mode(),
        total_domain_items=metrics["total_domain_items"],
        unique_domain_items=metrics["unique_domain_items"],
        terminal_domain_items=metrics["terminal_domain_items"],
        unaccounted_domain_items=metrics["unaccounted_domain_items"],
        total_source_spans=metrics["total_source_spans"],
        canonical_non_noise_spans=metrics["canonical_non_noise_spans"],
        duplicate_spans=metrics["duplicate_spans"],
        noise_spans=metrics["noise_spans"],
        unsupported_spans=metrics["unsupported_spans"],
        failed_spans=metrics["failed_spans"],
        planned_span_count=metrics["planned_span_count"],
        unassigned_count=metrics["unassigned_count"],
        processed_spans=metrics["processed_spans"],
        silent_dropped=metrics["silent_dropped"],
        domain_accounting_rate=metrics["domain_accounting_rate"],
        semantic_processing_rate=metrics["semantic_processing_rate"],
        timeline_coverage_rate=metrics["timeline_coverage_rate"],
        timeline_span_total=metrics["timeline_span_total"],
        timeline_span_processed=metrics["timeline_span_processed"],
        timeline_start_ms=metrics["timeline_start_ms"],
        timeline_end_ms=metrics["timeline_end_ms"],
        ppt_pages_total=metrics["ppt_pages_total"],
        ppt_pages_processed=metrics["ppt_pages_processed"],
        source_refs_total=metrics["source_refs_total"],
        source_refs_valid=metrics["source_refs_valid"],
        source_refs_invalid=metrics["source_refs_invalid"],
        required_items_total=metrics["required_items_total"],
        required_items_failed=metrics["required_items_failed"],
        required_items_unsupported=metrics["required_items_unsupported"],
        required_items_without_canonical_span=metrics["required_items_without_canonical_span"],
        required_spans_unprocessed=metrics["required_spans_unprocessed"],
        segment_count=metrics["segment_count"],
        segment_failed_count=metrics["segment_failed_count"],
        segments_consumed_by_merge=metrics["segments_consumed_by_merge"],
        merge_consumed_all_segments=metrics["merge_consumed_all_segments"],
        segment_structures_total=metrics.get("segment_structures_total", 0),
        merged_structures_total=metrics.get("merged_structures_total", 0),
        structures_dropped=metrics.get("structures_dropped", 0),
        legacy_candidate_rate=metrics["legacy_candidate_rate"],
        timeline_gap_count=metrics["timeline_gap_count"],
        semantic_rate_floor=SEMANTIC_RATE_FLOOR,
        gate=Gate(gate),
        degradation_reason=reason,
        issues=issues,
        unprocessed_reasons=metrics["unprocessed_reasons"],
    )

    payload = json.dumps(_report_to_metrics(metrics), ensure_ascii=False, sort_keys=True)
    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT id FROM coverage_reports WHERE run_id=?", (report.run_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE coverage_reports SET domain_id=?, engine_version=?, engine_mode=?, "
                " metrics_json=?, gate=?, degradation_reason=?, silent_dropped=?, "
                " updated_at=datetime('now','localtime') WHERE run_id=?",
                (domain_id, report.engine_version, report.engine_mode, payload, report.gate.value,
                 report.degradation_reason, report.silent_dropped, report.run_id),
            )
        else:
            conn.execute(
                "INSERT INTO coverage_reports (run_id, domain_id, engine_version, engine_mode, "
                " metrics_json, gate, degradation_reason, silent_dropped, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?, datetime('now','localtime'), datetime('now','localtime'))",
                (report.run_id, domain_id, report.engine_version, report.engine_mode, payload,
                 report.gate.value, report.degradation_reason, report.silent_dropped),
            )
    return report


def record_coverage_audit(domain_id: int) -> MaterialCoverageReport:
    """按 Material Domain 原始运行归属写审计报告。

    公开契约保持只有 ``domain_id`` 一个参数，调用方不能注入覆盖率等指标。
    """
    return _record_coverage_audit(domain_id, None)


def record_coverage_audit_for_run(domain_id: int, run_id: int) -> MaterialCoverageReport:
    """为复用父 Material Domain 的不可变重试任务写独立报告。"""
    return _record_coverage_audit(domain_id, int(run_id))


def _report_to_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """写库用的指标快照（排除纯内部字段），保证报告与 metrics_json 同源。"""
    return {k: v for k, v in metrics.items() if k != "silent_drop_span_ids"}


def get_coverage_report(run_id: int) -> Optional[dict]:
    row = db.fetch_one("SELECT * FROM coverage_reports WHERE run_id=?", (run_id,))
    if not row:
        return None
    out = dict(row)
    try:
        out["metrics"] = json.loads(out.get("metrics_json") or "{}")
    except Exception:
        out["metrics"] = {}
    return out


def get_degradation_reason(run_id: int) -> Optional[str]:
    """运行终态判定用：覆盖门禁未通过时返回原因，通过则 ``None``。"""
    row = db.fetch_one(
        "SELECT gate, degradation_reason FROM coverage_reports WHERE run_id=?", (run_id,)
    )
    if not row:
        return None
    if row.get("gate") == Gate.PASSED.value:
        return None
    return str(row.get("degradation_reason") or f"coverage gate={row.get('gate')}")
