"""节点 06 segment_lesson：无 Top-K 的全量分段（V6 Phase 2）。

设计约束（任务书 §三）:

* 输入只能来自 frozen Material Domain 的 canonical 非噪声 Source Map。
* **不允许**按相关性、重要性或 query 淘汰材料 —— 分段只按顺序与 token 预算。
* 必须保持课堂原始顺序（域内 ordinal）。
* 全部内容能进入上下文 → ``single_pass``；否则按时间/页码/预算顺序分段。
* 每个 canonical 非噪声 span **恰好**属于一个 primary segment
  （数据库层 ``uq_segment_primary_span`` 部分唯一索引兜底）。
* 允许相邻段少量 overlap，但必须区分 primary / overlap，覆盖率只按 primary 算。
* 任一 span 无法分配（单个 span 就超过输入预算）→ 计划不得通过。
* segment ``input_hash`` 稳定，支持幂等与断点复用。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from .. import database as db
from . import config as engine_config
from .contracts import (
    SCHEMA_VERSION,
    LedgerOutcome,
    LedgerStage,
    PlanStrategy,
    SourceKind,
    SpanState,
)

logger = logging.getLogger(__name__)

#: segment prompt/契约版本。进入 input_hash，版本变化必须使缓存失效。
SEGMENT_PROMPT_VERSION = "v2"
SEGMENT_SCHEMA_VERSION = SCHEMA_VERSION

#: 上下文窗口是模型的容量上限，不是单次请求的合理工作量。百万上下文模型若
#: 直接按窗口打包，会把整堂课塞成一个数万 token 的请求，虽然“不超窗”，却
#: 极易触发首包/读取超时。正常生产路径因此再加一层性能预算；显式
#: ``token_budget`` 仍可供测试或运维覆盖。
DEFAULT_PERFORMANCE_INPUT_BUDGET = 12_000

#: 相邻段 overlap 上限（占预算比例）。overlap 只为上下文，不计入覆盖率。
OVERLAP_BUDGET_RATIO = 0.15
MAX_OVERLAP_SPANS = 3

#: 分段时优先使用的自然边界（时间空洞 / 幻灯片页 / 讲义页）。
NATURAL_GAP_MS = 120_000

#: prompt 固定开销的保守估计（system prompt + schema + Source ID 清单 + 外壳）。
#: 仅用于测试/调用方做「预算是否还装得下内容」的粗算；真实校验一律走
#: ``verify_segment_budget`` 对最终 messages 的复算。
OVERHEAD_PROBE = 900


class SegmentationError(RuntimeError):
    """分段失败（span 超预算、无 canonical span 等）。"""


class NoCanonicalSpanError(SegmentationError):
    """域内没有任何 canonical 非噪声 span —— 无内容可分段。"""


def _segment_budget_override() -> Optional[int]:
    """测试/运维用的显式预算覆盖（``V6_SEGMENT_TOKEN_BUDGET``）。

    只影响分段粒度，不影响覆盖口径 —— 未分配 span 依然会被硬门禁阻断。
    """
    raw = (os.environ.get("V6_SEGMENT_TOKEN_BUDGET") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("无效 V6_SEGMENT_TOKEN_BUDGET=%r，忽略", raw)
        return None
    return value if value > 0 else None


def _performance_input_budget() -> int:
    """返回生产分段的性能预算，限制在合理且可运维调整的范围。"""
    raw = (os.environ.get("V6_SEGMENT_PERFORMANCE_BUDGET") or "").strip()
    if not raw:
        return DEFAULT_PERFORMANCE_INPUT_BUDGET
    try:
        return max(2_000, min(int(raw), 64_000))
    except ValueError:
        logger.warning("无效 V6_SEGMENT_PERFORMANCE_BUDGET=%r，使用默认值", raw)
        return DEFAULT_PERFORMANCE_INPUT_BUDGET


@dataclass
class SegmentDraft:
    ordinal: int
    primary: list[dict]
    overlap: list[dict]
    strategy: str
    start_ms: Optional[int]
    end_ms: Optional[int]
    title: Optional[str]

    @property
    def token_count(self) -> int:
        return sum(int(s.get("token_count") or 0) for s in self.primary + self.overlap)

    @property
    def primary_source_ids(self) -> list[str]:
        return [s["source_id"] for s in self.primary]

    @property
    def overlap_source_ids(self) -> list[str]:
        return [s["source_id"] for s in self.overlap]

    def input_hash(self) -> str:
        """稳定输入哈希：domain + 顺序 + source_id + 文本哈希 + 版本。

        不含时间戳/自增 id，保证同一输入重跑得到相同哈希（幂等 / 断点复用）。
        """
        payload = {
            "schema": SEGMENT_SCHEMA_VERSION,
            "prompt": SEGMENT_PROMPT_VERSION,
            "strategy": self.strategy,
            "ordinal": self.ordinal,
            "primary": [[s["source_id"], s["normalized_text_hash"]] for s in self.primary],
            "overlap": [[s["source_id"], s["normalized_text_hash"]] for s in self.overlap],
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def canonical_spans(domain_id: int) -> list[dict]:
    """按课堂原始顺序返回 canonical 非噪声 span。"""
    return [dict(r) for r in db.fetch_all(
        "SELECT id, source_id, ordinal, source_kind, locator, start_ms, end_ms, "
        " page_no, slide_no, token_count, char_count, material_id, normalized_text_hash, text "
        "FROM source_spans WHERE domain_id=? AND span_state=? ORDER BY ordinal, id",
        (domain_id, SpanState.INCLUDED.value),
    )]


def _model_input_budget(model_profile_id: Optional[str],
                        context_window: Optional[int] = None) -> tuple[int, int, int]:
    """返回 ``(input_budget_tokens, context_window, reserved_output)``。"""
    from .coverage import (
        DEFAULT_CONTEXT_WINDOW,
        DEFAULT_RESERVED_OUTPUT,
        DEFAULT_RESERVED_SYSTEM,
        SAFETY_MARGIN,
        _model_context_window,
    )
    window, reserved_output = _model_context_window(model_profile_id)
    if context_window:
        window = int(context_window)
    usable = max(int(window * (1.0 - SAFETY_MARGIN)) - DEFAULT_RESERVED_SYSTEM - reserved_output, 0)
    return usable, window, reserved_output


def _is_natural_boundary(prev: dict, cur: dict) -> bool:
    """判断两个相邻 span 之间是否是自然分段点。

    参与分段决策（预算过半即优先在自然边界成段），使分段同时尊重课堂结构
    （时间空洞 / 换幻灯片 / 换页 / 换来源）与 token 预算，而不是纯按 token 切。
    """
    if cur["source_kind"] == SourceKind.PPT.value:
        if (prev.get("slide_no") or prev.get("page_no")) != (cur.get("slide_no") or cur.get("page_no")):
            return True
    if cur["source_kind"] in (SourceKind.PDF.value, SourceKind.DOC.value):
        if prev.get("page_no") != cur.get("page_no"):
            return True
    p_start, p_end = prev.get("start_ms"), prev.get("end_ms")
    c_start = cur.get("start_ms")
    if p_end is not None and c_start is not None and c_start - p_end >= NATURAL_GAP_MS:
        return True
    if prev.get("source_kind") != cur.get("source_kind"):
        return True
    return False


def _estimate_messages_tokens(messages: list[dict]) -> int:
    """与 understanding.estimate_messages_tokens 同口径（本地实现避免循环导入）。"""
    from .normalize import estimate_tokens
    return sum(estimate_tokens(m.get("content") or "") for m in messages) + len(messages) * 4


def _material_tokens(rows: list[dict]) -> int:
    from .normalize import estimate_tokens
    from .understanding import _render_material_blocks
    return estimate_tokens(_render_material_blocks(rows))


def _prompt_overhead_tokens(segment_ordinal: int, segment_total: int,
                            span_rows: list[dict]) -> int:
    """该 segment 的固定开销（system prompt + JSON schema + Source ID 清单 +
    locator 与 user 消息外壳），**不含材料正文**。

    预算必须以最终实际 messages 为对象计算：材料正文一次性计入，开销单独估算，
    两者相加不得超过 input budget。
    """
    from .understanding import render_segment_messages

    probe = {"ordinal": segment_ordinal, "token_count": 0,
             "sources": [{"source_span_id": int(r["id"]), "source_id": r["source_id"],
                          "role": "primary"} for r in span_rows]}
    messages, _meta = render_segment_messages(probe, segment_total, extra_spans=span_rows)
    return max(_estimate_messages_tokens(messages) - _material_tokens(span_rows), 0)


def _empty_overhead_tokens() -> int:
    """零 span 时的最小 prompt 开销（用于判断预算是否根本装不下任何内容）。"""
    return _prompt_overhead_tokens(1, 1, [])


def plan_segments(domain_id: int, *, model_profile_id: Optional[str] = None,
                  context_window: Optional[int] = None,
                  token_budget: Optional[int] = None) -> dict[str, Any]:
    """计算分段方案（纯函数式：不写库），返回 draft 列表与预算信息。

    ``token_budget`` 可显式覆盖输入预算（用于测试与运维排查；正常路径由模型
    上下文窗口派生）。它**只影响分段粒度**，不影响「哪些 span 必须进入理解」。
    """
    spans = canonical_spans(domain_id)
    budget, window, reserved_output = _model_input_budget(model_profile_id, context_window)
    context_input_budget = budget
    if token_budget is None:
        token_budget = _segment_budget_override()
        if token_budget is None:
            token_budget = _performance_input_budget()
    if token_budget is not None:
        budget = min(context_input_budget, max(int(token_budget), 0))

    if not spans:
        # 没有任何 canonical 非噪声 span：没有内容可分段。
        # 这里**不能**造出一个空的 primary segment —— 那会让 understand_segments
        # 对一个空段调模型并失败，把「必需材料解析失败」误报成「理解节点故障」，
        # 覆盖审计也失去机会给出 required_items_without_canonical_span 结论。
        return {"strategy": PlanStrategy.SINGLE_PASS.value, "budget": budget,
                "context_input_budget": context_input_budget,
                "context_window": window, "reserved_output": reserved_output,
                "total_tokens": 0, "drafts": [], "unassigned_source_ids": []}

    # 预算口径（Phase 2 Closeout，统一为**最终 messages 的总输入预算**）:
    #   prompt_overhead + Σ(primary tokens) + Σ(overlap tokens) ≤ input_budget
    # 因此打包时必须先扣除 prompt 开销，再用剩余额度装 primary。
    overhead0 = _empty_overhead_tokens()
    if budget <= overhead0:
        raise SegmentationError(
            f"输入预算 {budget} 小于最小 prompt 开销 {overhead0}，无法容纳任何材料。"
            "请提高预算或更换上下文更大的模型。"
        )

    # 性能预算不能把一个已经规范化、不可再拆的 canonical span 变成假故障。
    # 若单个 span 天生较大，只对本次计划放宽到“刚好容纳最大 span”；仍受模型
    # 上下文硬预算约束，且不会丢弃或截断内容。
    # 额外 512 tokens 覆盖最终 allowed_source_ids / segment_total 等动态外壳差异；
    # 最终仍由 verify_segment_budget 做硬校验。
    largest_required = max(
        (int(s.get("token_count") or 0) + overhead0 + 512 for s in spans), default=0)
    performance_budget_relaxed = False
    if largest_required > budget and largest_required <= context_input_budget:
        budget = largest_required
        performance_budget_relaxed = True

    total_tokens = sum(int(s.get("token_count") or 0) for s in spans)
    oversized = [s for s in spans
                 if int(s.get("token_count") or 0) + overhead0 > budget]
    if oversized:
        # 单个 span 超预算：build_source_map 已按 cue/句子做过保守拆分，因此
        # 这里出现的超大 span 意味着「单句本身就超预算」，无法在不截断内容的
        # 前提下安全拆分 —— 此时失败而不是丢弃材料。
        raise SegmentationError(
            "存在单个 span 超过输入预算，且无法在不截断内容的前提下安全拆分："
            + ", ".join(f"{s['source_id']}({s['token_count']} tokens)" for s in oversized[:5])
        )

    if total_tokens + overhead0 <= budget:
        draft = SegmentDraft(
            ordinal=1, primary=spans, overlap=[], strategy=PlanStrategy.SINGLE_PASS.value,
            start_ms=min((s["start_ms"] for s in spans if s["start_ms"] is not None), default=None),
            end_ms=max((s["end_ms"] for s in spans if s["end_ms"] is not None), default=None),
            title=None,
        )
        return {"strategy": PlanStrategy.SINGLE_PASS.value, "budget": budget,
                "context_input_budget": context_input_budget,
                "performance_budget_relaxed": performance_budget_relaxed,
                "context_window": window, "reserved_output": reserved_output,
                "total_tokens": total_tokens, "drafts": [draft],
                "unassigned_source_ids": [], "overlap_trimmed": 0}

    # ---- 分段的确定性打包：保序 + 预算（含 prompt 开销）+ 自然边界优先 ----
    drafts: list[SegmentDraft] = []
    current: list[dict] = []
    used = 0
    capacity = 0
    for span in spans:
        tokens = int(span.get("token_count") or 0)
        if current and (used + tokens > capacity
                        or (_is_natural_boundary(current[-1], span)
                            and used >= capacity * 0.5)):
            # 自然边界（时间空洞 / 换页 / 换来源）在用量过半时优先成段，
            # 使分段同时尊重「课堂结构」与「token 预算」，而不是纯 token 切分。
            drafts.append(SegmentDraft(
                ordinal=len(drafts) + 1, primary=current, overlap=[],
                strategy=PlanStrategy.SEGMENTED_MAP_MERGE.value,
                start_ms=min((s["start_ms"] for s in current if s["start_ms"] is not None),
                             default=None),
                end_ms=max((s["end_ms"] for s in current if s["end_ms"] is not None),
                           default=None),
                title=None,
            ))
            current, used = [], 0
        if not current:
            # 新段：按该段的真实 prompt 开销重算可用额度
            capacity = max(budget - _prompt_overhead_tokens(
                len(drafts) + 1, max(len(drafts) + 1, 1), [span]), 0)
        current.append(span)
        used += tokens
    if current:
        drafts.append(SegmentDraft(
            ordinal=len(drafts) + 1, primary=current, overlap=[],
            strategy=PlanStrategy.SEGMENTED_MAP_MERGE.value,
            start_ms=min((s["start_ms"] for s in current if s["start_ms"] is not None), default=None),
            end_ms=max((s["end_ms"] for s in current if s["end_ms"] is not None), default=None),
            title=None,
        ))

    # ---- overlap：只为上下文，且**必须装得下** ----
    # primary 已占满预算时 overlap 会被缩小甚至取消 —— 绝不牺牲 primary。
    overlap_trimmed = 0
    total_segments = len(drafts)
    for i in range(1, len(drafts)):
        prev, cur = drafts[i - 1], drafts[i]
        primary_tokens = sum(int(s.get("token_count") or 0) for s in cur.primary)
        overhead = _prompt_overhead_tokens(cur.ordinal, total_segments, cur.primary)
        free = budget - overhead - primary_tokens
        cap = min(int(budget * OVERLAP_BUDGET_RATIO), max(free, 0))
        take: list[dict] = []
        acc = 0
        for span in reversed(prev.primary):
            tokens = int(span.get("token_count") or 0)
            if len(take) >= MAX_OVERLAP_SPANS or acc + tokens > cap:
                break
            take.insert(0, span)
            acc += tokens
        skipped = len(prev.primary) - len(take)
        overlap_trimmed += skipped
        cur.overlap = take

    return {"strategy": PlanStrategy.SEGMENTED_MAP_MERGE.value, "budget": budget,
            "context_input_budget": context_input_budget,
            "performance_budget_relaxed": performance_budget_relaxed,
            "context_window": window, "reserved_output": reserved_output,
            "total_tokens": total_tokens, "drafts": drafts,
            "unassigned_source_ids": [], "overlap_trimmed": overlap_trimmed}


def verify_segment_budget(domain_id: int, plan: dict[str, Any]) -> dict[str, Any]:
    """用**最终实际 messages** 复算每个 segment 的输入 token，验证不超预算。

    这是 Phase 2 Closeout 的硬校验：预算是相对最终 messages 计算的，而不是
    相对 span token 之和。
    """
    from .understanding import estimate_messages_tokens, render_segment_messages
    total_segments = len(plan["drafts"])
    rows = []
    worst = 0
    over = 0
    for draft in plan["drafts"]:
        span_ids = [int(s["id"]) for s in draft.primary + draft.overlap]
        probe = {"ordinal": draft.ordinal,
                 "token_count": draft.token_count,
                 "sources": [{"source_span_id": i, "source_id": "", "role": "primary"}
                             for i in span_ids]}
        messages, _meta = render_segment_messages(probe, total_segments)
        used = estimate_messages_tokens(messages)
        worst = max(worst, used)
        if used > plan["budget"]:
            over += 1
        rows.append({"ordinal": draft.ordinal, "input_tokens": used,
                     "budget": plan["budget"], "primary": len(draft.primary),
                     "overlap": len(draft.overlap)})
    return {"budget": plan["budget"], "max_input_tokens": worst,
            "over_budget_segments": over, "segments": rows}


def fit_segment_plan_to_budget(domain_id: int, plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """把分段方案修复到最终 messages 的真实输入预算以内。

    初步打包阶段无法预知最终 ``segment_total``，且 ``allowed_source_ids`` 会随
    span 数量增长。两者都会让提示词开销产生少量变化。旧实现遇到这种估算差
    就让整次工作流失败；这里改为以最终渲染消息为准，确定性地移除 overlap，
    必要时继续二分 primary。primary span 不删除、不重排，因此材料覆盖不变。
    """
    drafts: list[SegmentDraft] = list(plan.get("drafts") or [])
    if not drafts:
        return plan, verify_segment_budget(domain_id, plan)

    repaired = 0
    max_rounds = max(sum(len(d.primary) for d in drafts), 1) + 2
    for _ in range(max_rounds):
        for ordinal, draft in enumerate(drafts, 1):
            draft.ordinal = ordinal
        plan["drafts"] = drafts
        check = verify_segment_budget(domain_id, plan)
        over_ordinals = {
            int(row["ordinal"]) for row in check["segments"]
            if int(row["input_tokens"]) > int(row["budget"])
        }
        if not over_ordinals:
            if repaired:
                plan["strategy"] = PlanStrategy.SEGMENTED_MAP_MERGE.value
                plan["budget_repairs"] = repaired
            return plan, check

        changed = False
        rebuilt: list[SegmentDraft] = []
        for draft in drafts:
            if draft.ordinal not in over_ordinals:
                rebuilt.append(draft)
                continue

            # overlap 只是上下文，不计覆盖。优先去掉它即可恢复预算时，不拆 primary。
            if draft.overlap:
                plan["overlap_trimmed"] = int(plan.get("overlap_trimmed") or 0) + len(draft.overlap)
                draft.overlap = []
                rebuilt.append(draft)
                repaired += 1
                changed = True
                continue

            if len(draft.primary) <= 1:
                source_id = draft.primary[0]["source_id"] if draft.primary else "（空段）"
                raise SegmentationError(
                    f"{source_id} 在最终提示词中仍超过输入预算 {plan['budget']}，"
                    "无法在不截断原始材料的前提下继续拆分。"
                )

            total = sum(max(int(s.get("token_count") or 0), 1) for s in draft.primary)
            target = max(total // 2, 1)
            acc = 0
            split_at = 1
            for i, span in enumerate(draft.primary[:-1], 1):
                acc += max(int(span.get("token_count") or 0), 1)
                split_at = i
                if acc >= target:
                    break
            halves = (draft.primary[:split_at], draft.primary[split_at:])
            for primary in halves:
                rebuilt.append(SegmentDraft(
                    ordinal=0,
                    primary=primary,
                    overlap=[],
                    strategy=PlanStrategy.SEGMENTED_MAP_MERGE.value,
                    start_ms=min((s["start_ms"] for s in primary if s["start_ms"] is not None), default=None),
                    end_ms=max((s["end_ms"] for s in primary if s["end_ms"] is not None), default=None),
                    title=draft.title,
                ))
            repaired += 1
            changed = True
        if not changed:
            break
        drafts = rebuilt

    raise SegmentationError("自动重分段未能在有限轮次内满足最终输入预算")


def persist_segments(domain_id: int, run_id: int, plan: dict[str, Any]) -> list[dict]:
    """把分段方案写入 lesson_segments / segment_source_spans。

    幂等：同一 domain 已有 segment 且 input_hash 全一致时直接复用既有行。
    """
    existing = db.fetch_all(
        "SELECT id, ordinal, input_hash, status, strategy, token_count, "
        " primary_span_count, overlap_span_count FROM lesson_segments WHERE domain_id=? "
        "ORDER BY ordinal", (domain_id,))
    if existing:
        by_ordinal = {int(r["ordinal"]): r for r in existing}
        same = (len(existing) == len(plan["drafts"]) and all(
            by_ordinal.get(d.ordinal, {}).get("input_hash") == d.input_hash()
            for d in plan["drafts"]))
        if same:
            # 复用分支必须返回与新建分支**同形状**的 dict：调用方（segment_lesson_node）
            # 会直接读 primary_span_count / overlap_span_count。早期实现直接
            # ``return [dict(r) for r in existing]``，而该 SELECT 没有这两列，
            # 于是「同一 run 重跑」在节点里 KeyError 崩溃。
            return [{
                "id": int(r["id"]), "ordinal": int(r["ordinal"]),
                "input_hash": r["input_hash"], "status": r["status"],
                "strategy": r["strategy"], "token_count": int(r["token_count"] or 0),
                "primary_span_count": int(r["primary_span_count"] or 0),
                "overlap_span_count": int(r["overlap_span_count"] or 0),
                "reused": True,
            } for r in existing]
        # 分段方案变了（材料 / 预算 / 分段算法 / prompt 版本变化 ⇒ 缓存失效）：
        # 整套重建。原 segment 与理解结果被级联删除，避免陈旧缓存被复用。
        logger.info("domain %s 分段方案变化，重建 segments", domain_id)
        with db.transaction() as conn:
            conn.execute("DELETE FROM lesson_segments WHERE domain_id=?", (domain_id,))

    created: list[dict] = []
    with db.transaction() as conn:
        for draft in plan["drafts"]:
            cur = conn.execute(
                "INSERT INTO lesson_segments (run_id, domain_id, ordinal, strategy, title, "
                " input_hash, token_count, primary_span_count, overlap_span_count, start_ms, "
                " end_ms, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'pending', datetime('now','localtime'), "
                " datetime('now','localtime'))",
                (run_id, domain_id, draft.ordinal, draft.strategy, draft.title,
                 draft.input_hash(), draft.token_count, len(draft.primary),
                 len(draft.overlap), draft.start_ms, draft.end_ms),
            )
            segment_id = int(cur.lastrowid)
            for role, rows in (("primary", draft.primary), ("overlap", draft.overlap)):
                for i, span in enumerate(rows, 1):
                    conn.execute(
                        "INSERT INTO segment_source_spans (segment_id, source_span_id, "
                        " source_id, role, ordinal, created_at) "
                        "VALUES (?,?,?,?,?, datetime('now','localtime'))",
                        (segment_id, int(span["id"]), span["source_id"], role, i),
                    )
            created.append({
                "id": segment_id, "ordinal": draft.ordinal, "input_hash": draft.input_hash(),
                "token_count": draft.token_count, "status": "pending",
                "primary_span_count": len(draft.primary),
                "overlap_span_count": len(draft.overlap),
            })
    return created


def record_segment_ledger(domain_id: int, run_id: int) -> dict[str, int]:
    """为每个 canonical span 写 ``understand_segments`` 的**初始*终态账目。

    这里只写 ``not_used`` + ``segment_assignment_pending`` 之外的语义：
    primary 归属本身不是「已理解」，因此本函数写的是
    ``not_used`` + ``segment_not_reached``；节点 07 理解成功后会把它覆盖为
    ``processed``（同一 ``(domain, span, stage)`` 唯一，后者覆盖前者）。
    """
    rows = db.fetch_all(
        "SELECT s.id, s.source_id, "
        " (SELECT ss.segment_id FROM segment_source_spans ss "
        "  WHERE ss.source_span_id = s.id AND ss.role='primary' LIMIT 1) AS segment_id "
        "FROM source_spans s WHERE s.domain_id=? AND s.span_state=? ORDER BY s.ordinal",
        (domain_id, SpanState.INCLUDED.value),
    )
    counts = {"pending": 0}
    from .coverage import REASON_SEGMENT_NOT_REACHED, record_ledger_entry
    for row in rows:
        record_ledger_entry(
            run_id=run_id, domain_id=domain_id, source_span_id=int(row["id"]),
            source_id=row["source_id"], stage=LedgerStage.UNDERSTAND_SEGMENTS.value,
            outcome=LedgerOutcome.NOT_USED.value,
            reason_code=REASON_SEGMENT_NOT_REACHED,
            reason_detail="已分配 primary segment，等待该段理解完成",
            segment_id=int(row["segment_id"]) if row["segment_id"] else None,
        )
        counts["pending"] += 1
    return counts


def get_segments(domain_id: int) -> list[dict]:
    rows = db.fetch_all(
        "SELECT * FROM lesson_segments WHERE domain_id=? ORDER BY ordinal", (domain_id,))
    out = []
    for row in rows:
        item = dict(row)
        item["sources"] = [dict(r) for r in db.fetch_all(
            "SELECT source_id, source_span_id, role, ordinal FROM segment_source_spans "
            "WHERE segment_id=? ORDER BY role, ordinal", (int(row["id"]),))]
        out.append(item)
    return out


def segment_plan_summary(domain_id: int) -> dict[str, Any]:
    """给 API / 报告的 segment 摘要。"""
    rows = get_segments(domain_id)
    drafts = [{
        "segment_id": int(r["id"]),
        "ordinal": int(r["ordinal"]),
        "status": r["status"],
        "strategy": r["strategy"],
        "title": r["title"],
        "input_hash": r["input_hash"],
        "token_count": int(r["token_count"] or 0),
        "primary_source_ids": [s["source_id"] for s in r["sources"] if s["role"] == "primary"],
        "overlap_source_ids": [s["source_id"] for s in r["sources"] if s["role"] == "overlap"],
        "primary_span_count": int(r["primary_span_count"] or 0),
        "overlap_span_count": int(r["overlap_span_count"] or 0),
        "start_ms": r["start_ms"],
        "end_ms": r["end_ms"],
        "attempts": int(r["attempts"] or 0),
        "error": r["error"],
        "model_used": None,
        "duration_ms": None,
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    } for r in rows]
    # 每段的理解状态 / 模型 / 耗时（供审计界面展示）
    if rows:
        su_rows = db.fetch_all(
            "SELECT segment_id, status, model_used, source_ref_count, created_at, updated_at "
            "FROM segment_understandings WHERE domain_id=?", (domain_id,))
        su_by_seg = {int(r["segment_id"]): dict(r) for r in su_rows}
        for draft in drafts:
            su = su_by_seg.get(draft["segment_id"])
            if not su:
                continue
            draft["understanding_status"] = su.get("status")
            draft["model_used"] = su.get("model_used")
            draft["source_ref_count"] = su.get("source_ref_count")
            draft["duration_ms"] = _duration_ms(su.get("created_at"), su.get("updated_at"))
    return {
        "segment_count": len(drafts),
        "strategy": drafts[0]["strategy"] if drafts else None,
        "segments": drafts,
    }


def _duration_ms(start: Any, end: Any) -> Optional[int]:
    """粗略耗时（秒级时间戳相减，仅用于审计展示；解析失败返回 None）。"""
    from datetime import datetime
    if not start or not end:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            a = datetime.strptime(str(start)[:19], fmt)
            b = datetime.strptime(str(end)[:19], fmt)
            return max(int((b - a).total_seconds() * 1000), 0)
        except Exception:
            continue
    return None
