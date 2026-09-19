"""节点 05.5 plan_segment_boundaries：分段边界规划（V6 Phase 2 增强）。

为什么需要这个节点
==================

理解阶段的「段」同时承担三个角色，三者都要求段不能无限大：

1. **审计粒度**：段成功 ⇒ 该段全部 primary span 被**无条件**记为 processed
   （``understanding._understand_with_retries``）。段越大这个记账越粗糙；
   ``single_pass`` 时「已处理」一句话就覆盖全部材料，审计实质失效。
2. **注意力质量**：超长上下文存在「中间迷失」——模型对超长输入的注意力并不
   均匀。把全部材料塞成一段，中段内容容易被忽略，而覆盖率仍显示 100%。
3. **输出能力**：每段独立调用，单段输出上限 16384 token。整堂课的笔记塞进
   一次输出必然被截断。分段等于给输出扩容。

同时，段还有**下界**：每段至少覆盖一个**完整知识点**，否则知识点被腰斩，
两段各得半截（merge 要么进 ``unresolved_conflicts``，要么产出两个残缺块）。

于是段被夹在两个约束之间::

    下界：段 ⊇ 一个完整知识点
    上界：段 ≤ 注意力 / 输出能力的可承受量

纯 token 装箱只能满足上界（见 ``segment.plan_segments``）。本节点负责下界：
让一个**超长上下文模型**先读完全部材料，标出知识点边界与建议切点。

职责边界
========

* 只产出**建议**（知识点区间 + 切点）并做确定性校验；
* 建议不可用时**绝不阻断** run —— 调用方回退 ``segment.plan_segments`` 的
  结构边界逻辑（``preferred_breaks=None``）；
* 不写 ``lesson_segments`` / ``segment_source_spans``（那是
  ``segment.persist_segments`` 的职责）。本模块只落
  ``segment_boundary_plans`` 这一张「建议账」。

三条设计约定（都是讨论中被推翻过的方案，记录在此避免重蹈）
==========================================================

1. **输出用 ordinal，不用字符偏移**：``segment_source_spans`` 引用的是
   ``source_span_id``，系统表达不了「半个 span」。ordinal 是天然粒度，
   既不需要「吸附到边界」的映射，也不可能切在 span 中间。
2. **输入给完整原文，不给「首句」**：知识点边界是语义判断，只看首句等价于
   关键词匹配——而课堂口语里「好 / 那么 / 接下来」是高密度填充词，
   拿它们当边界信号假阳性会爆炸。
3. **与模型沟通尺寸用「字数」**：模型没有 tokenizer，无法精确计 token，
   报 token 值它只能猜。报成汉字数它才数得出来。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .. import database as db

logger = logging.getLogger(__name__)

#: 契约版本。进入 input_hash；版本变化必须使建议缓存失效。
BOUNDARY_SCHEMA_VERSION = "v6.1"
BOUNDARY_PROMPT_VERSION = "v1"

#: 建议输出的 token 上限。只输出数字区间，几百个切点也远不到这个量。
BOUNDARY_MAX_OUTPUT_TOKENS = 2_048

#: 一个知识点至少覆盖多少个 span。低于此值 = 把知识点切碎（L1）。
MIN_SPANS_PER_KU = 3

#: 段数上限（占 canonical span 数的比例）。防止切成几百个碎段（L2）。
MAX_SEGMENTS_RATIO = 0.3

#: 「退回重画」的最大轮次（首轮之外）。超了就回退结构边界。
MAX_RETRY_ROUNDS = 2

#: token → 汉字数的保守折算系数，**仅用于与模型沟通尺寸**。
#: ``normalize.estimate_tokens`` 对中文按 1 字 ≈ 1 token、ASCII 按 4 字符
#: ≈ 1 token 计；混合材料实际字数低于 token 数，取 0.75 作保守提示。
TOKEN_TO_CJK_HINT = 0.75


def tokens_to_char_hint(tokens: int) -> int:
    """把 token 预算折算成「大约多少个汉字」，用于 prompt 措辞。"""
    return max(int(int(tokens) * TOKEN_TO_CJK_HINT), 1)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class KnowledgeUnitRange:
    """模型标出的一个知识点（span 序号区间）。"""

    name: str
    kind: str
    start_ordinal: int
    end_ordinal: int
    confidence: float = 1.0

    @property
    def span_count(self) -> int:
        return max(self.end_ordinal - self.start_ordinal + 1, 0)


@dataclass
class SegmentRange:
    """建议方案里的一段（仅序号区间，尚未与 span id 绑定）。"""

    ordinal: int
    start_ordinal: int
    end_ordinal: int
    ku_count: int
    tokens: int


@dataclass
class BoundaryValidation:
    """建议方案的校验结果。

    * ``hard`` 非空 ⇒ 建议**不可用**（切点会破坏覆盖不变量，可能让 run failed）
    * ``soft`` 非空 ⇒ 建议**格式合法但无用/低质**（防偷懒三道）
    * ``oversized_ku`` ⇒ 观测项，既不拒绝也不降级，供下游标记
    """

    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    oversized_ku: list[dict] = field(default_factory=list)
    segments: list[SegmentRange] = field(default_factory=list)
    breaks: list[int] = field(default_factory=list)
    knowledge_units: list[KnowledgeUnitRange] = field(default_factory=list)
    max_ordinal: int = 0
    total_tokens: int = 0
    max_segment_tokens: int = 0

    @property
    def ok(self) -> bool:
        return not self.hard and not self.soft

    @property
    def status(self) -> str:
        if self.hard:
            return "rejected"
        if self.soft:
            return "low_quality"
        return "succeeded"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "hard": list(self.hard),
            "soft": list(self.soft),
            "oversized_ku": list(self.oversized_ku),
            "segment_count": len(self.segments),
            "ku_count": len(self.knowledge_units),
            "max_ordinal": self.max_ordinal,
            "total_tokens": self.total_tokens,
        }


# ---------------------------------------------------------------------------
# 纯函数：渲染 / 哈希 / 校验 / 反馈
# ---------------------------------------------------------------------------
def _span_token_map(spans: list[dict]) -> dict[int, int]:
    return {int(s["ordinal"]): int(s.get("token_count") or 0) for s in spans}


def render_span_index(spans: list[dict]) -> str:
    """把全部 span 渲染成 ``[序号] (locator)`` + **完整原文** 的编号表。

    刻意不做摘要、不截断：知识点边界是语义判断，残缺输入只会退化成
    关键词匹配（转折词在课堂口语里是高频填充词）。
    """
    blocks: list[str] = []
    for s in spans:
        ordinal = int(s["ordinal"])
        locator = str(s.get("locator") or "").strip()
        text = str(s.get("text") or "").strip()
        head = f"[{ordinal}] ({locator})" if locator else f"[{ordinal}]"
        blocks.append(f"{head}\n{text}")
    return "\n\n".join(blocks)


def compute_boundary_input_hash(spans: list[dict], *, max_segment_tokens: int,
                                min_spans_per_ku: int = MIN_SPANS_PER_KU) -> str:
    """建议方案的稳定输入哈希（不含 run_id，支持跨 run 复用）。

    进入哈希的量必须能决定「建议长什么样」：span 顺序 / 文本哈希 /
    上界 / 下限 / 两个版本号。上界变化必须使缓存失效 —— 上界变了，
    合理的切法就该重算。
    """
    payload = {
        "schema": BOUNDARY_SCHEMA_VERSION,
        "prompt": BOUNDARY_PROMPT_VERSION,
        "max_segment_tokens": int(max_segment_tokens),
        "min_spans_per_ku": int(min_spans_per_ku),
        "spans": [
            [int(s["ordinal"]), str(s.get("source_id") or ""),
             str(s.get("normalized_text_hash") or "")]
            for s in spans
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def validate_boundaries(data: dict, spans: list[dict], *,
                        max_segment_tokens: int,
                        min_spans_per_ku: int = MIN_SPANS_PER_KU,
                        max_segments_ratio: float = MAX_SEGMENTS_RATIO
                        ) -> BoundaryValidation:
    """对模型建议做确定性校验：6 条硬约束 + 3 条防偷懒下限 + 1 项观测。

    硬约束（任一违反 ⇒ 建议不可用，调用方回退结构边界）::

        H1 每个知识点 start <= end
        H2 知识点区间严格递增且不重叠
        H3 知识点区间无空隙、完整覆盖 [1, max_ordinal]
        H4 breaks 严格递增，且每个值都是某个知识点的 end_ordinal
        H5 breaks 不含 max_ordinal（否则末段为空）
        H6 每个知识点恰好落在一段内（不跨切点）

    **H3 是生死线**：违反会让部分 span 落不进任何段 ⇒ ``unassigned_count > 0``
    ⇒ ``coverage.evaluate_gate`` 直接 ``gate=failed``（不是降级）。

    下限（合法但无用 ⇒ ``low_quality``，仍回退结构边界）::

        L1 每个知识点覆盖的 span 数 >= min_spans_per_ku
        L2 段数 <= max_segments_ratio × span 数
        L3 材料超上界时必须至少切一刀
        L4 段超上界、但段内知识点 >= 2（还能在知识点边界再切 ⇒ 退回重画）

    **L3 是 H1~H6 全都拦不住的形态**：``{"knowledge_units": [{"start_ordinal": 1,
    "end_ordinal": 500}], "breaks": []}`` 格式完全合法，却等价于不分段 ——
    中间迷失与审计失效原样回归。

    观测（不拒绝、不降级）::

        O1 某知识点自身 token 超过上界 ⇒ 记入 ``oversized_ku``，
           由下游切开并在报告中标记「此处质量未保证」。
    """
    tokens_of = _span_token_map(spans)
    max_ordinal = max(tokens_of) if tokens_of else 0
    total_tokens = sum(tokens_of.values())
    v = BoundaryValidation(max_ordinal=max_ordinal, total_tokens=total_tokens,
                           max_segment_tokens=int(max_segment_tokens))

    if max_ordinal <= 0:
        v.hard.append("H0: 域内没有 canonical span，无需规划边界")
        return v

    # ---- 解析知识点 ----
    kus: list[KnowledgeUnitRange] = []
    for i, raw in enumerate(data.get("knowledge_units") or []):
        if not isinstance(raw, dict):
            v.hard.append(f"H1: knowledge_units[{i}] 不是对象")
            continue
        try:
            ku = KnowledgeUnitRange(
                name=str(raw.get("name") or ""),
                kind=str(raw.get("kind") or "concept"),
                start_ordinal=int(raw.get("start_ordinal") or 0),
                end_ordinal=int(raw.get("end_ordinal") or 0),
                confidence=float(raw.get("confidence", 1.0) or 0.0),
            )
        except (TypeError, ValueError):
            v.hard.append(f"H1: knowledge_units[{i}] 区间不是合法整数")
            continue
        kus.append(ku)

    if not kus:
        v.hard.append("H0: knowledge_units 为空，无法判断段边界")
        return v

    kus.sort(key=lambda k: k.start_ordinal)
    v.knowledge_units = kus

    # ---- H1 / H2 / H3 ----
    if kus[0].start_ordinal != 1:
        v.hard.append(
            f"H3: 首个知识点起点 {kus[0].start_ordinal} != 1（前面有 span 未覆盖）")
    if kus[-1].end_ordinal != max_ordinal:
        v.hard.append(
            f"H3: 末个知识点终点 {kus[-1].end_ordinal} != {max_ordinal}"
            "（末尾有 span 未覆盖）")
    for i, ku in enumerate(kus):
        if ku.start_ordinal < 1 or ku.end_ordinal < 1:
            v.hard.append(f"H1: 知识点[{i}]「{ku.name}」区间含非正序号")
        elif ku.start_ordinal > ku.end_ordinal:
            v.hard.append(
                f"H1: 知识点[{i}]「{ku.name}」区间倒挂 "
                f"{ku.start_ordinal}>{ku.end_ordinal}")
        if i and ku.start_ordinal != kus[i - 1].end_ordinal + 1:
            v.hard.append(
                f"H2/H3: 知识点[{i - 1}]→[{i}] 边界不连续 "
                f"({kus[i - 1].end_ordinal} → {ku.start_ordinal})，"
                "存在重叠或空隙")

    # ---- H4 / H5 ----
    ku_ends = {ku.end_ordinal for ku in kus}
    raw_breaks = data.get("breaks") or []
    try:
        parsed = [int(b) for b in raw_breaks]
    except (TypeError, ValueError):
        v.hard.append("H4: breaks 含非整数")
        return v
    if len(set(parsed)) != len(parsed):
        v.hard.append("H4: breaks 有重复值（必须是严格递增的切点集合）")
    for b in parsed:
        if b not in ku_ends:
            v.hard.append(f"H4: 切点 {b} 不是任何知识点的结束位置（会腰斩知识点）")
        if b >= max_ordinal:
            v.hard.append(f"H5: 切点 {b} >= 最大序号 {max_ordinal}，会产生空段")
    breaks = sorted({b for b in parsed if 1 <= b < max_ordinal})
    v.breaks = breaks

    # ---- 段划分 + H6 ----
    starts = [1] + [b + 1 for b in breaks]
    ends = list(breaks) + [max_ordinal]
    segments: list[SegmentRange] = []
    for i, (start, end) in enumerate(zip(starts, ends), 1):
        if start > end:
            v.hard.append(f"H5: 第 {i} 段区间为空（{start} > {end}）")
            continue
        covered = [k for k in kus
                   if k.start_ordinal >= start and k.end_ordinal <= end]
        crossing = [k for k in kus
                    if k.start_ordinal <= end and k.end_ordinal >= start
                    and (k.start_ordinal < start or k.end_ordinal > end)]
        if crossing:
            v.hard.append(
                f"H6: 知识点「{crossing[0].name}」跨越第 {i} 段边界"
                f"（{crossing[0].start_ordinal}-{crossing[0].end_ordinal}）")
        seg_tokens = sum(tokens_of.get(o, 0) for o in range(start, end + 1))
        segments.append(SegmentRange(ordinal=i, start_ordinal=start,
                                     end_ordinal=end, ku_count=len(covered),
                                     tokens=seg_tokens))
    v.segments = segments

    # ---- L1 / L2 / L3（防偷懒）----
    small = [k for k in kus if k.span_count < min_spans_per_ku]
    if small:
        v.soft.append(
            f"L1: {len(small)} 个知识点覆盖不足 {min_spans_per_ku} 个 span"
            f"（如「{small[0].name}」仅 {small[0].span_count} 个）")
    max_segments = max(int(max_ordinal * max_segments_ratio), 1)
    if segments and len(segments) > max_segments:
        v.soft.append(
            f"L2: 段数 {len(segments)} 超过上限 {max_segments}"
            f"（{max_ordinal} 个 span × {max_segments_ratio}）")
    if total_tokens > max_segment_tokens and not breaks:
        v.soft.append(
            f"L3: 材料共 {total_tokens} token 超过上界 {max_segment_tokens}，"
            "但一个切点都没给（等价于不分段）")

    # ---- L4：段超上界，但段内有 >=2 个知识点（还能再切 ⇒ 退回重画） ----
    # 与 O1 的分界：段重且段内只有 1 个知识点 ⇒ 该知识点必然自身超重 ⇒ 无解，
    # 属于观测项；段重但段内有多个知识点 ⇒ 模型本可以在知识点边界切得更细。
    overweight = [s for s in segments
                  if max_segment_tokens and s.tokens > max_segment_tokens]
    fixable = [s for s in overweight if s.ku_count >= 2]
    if fixable:
        v.soft.append(
            f"L4: {len(fixable)} 段超过上界 {max_segment_tokens} token"
            f"（如第 {fixable[0].ordinal} 段 {fixable[0].tokens} token，"
            f"含 {fixable[0].ku_count} 个知识点），可在知识点边界处再切")

    # ---- O1：超大知识点（观测，不拒绝） ----
    for ku in kus:
        ku_tokens = sum(tokens_of.get(o, 0)
                        for o in range(ku.start_ordinal, ku.end_ordinal + 1))
        if ku_tokens > max_segment_tokens:
            v.oversized_ku.append({
                "name": ku.name, "kind": ku.kind,
                "start_ordinal": ku.start_ordinal,
                "end_ordinal": ku.end_ordinal,
                "tokens": ku_tokens, "limit": int(max_segment_tokens),
            })

    return v


def build_retry_hint(v: Optional[BoundaryValidation]) -> str:
    """构造「退回重画」的反馈文本（无问题或首轮时返回空串）。

    超重/违规时**退回给模型重画，而不是由程序硬切**：程序硬切是在知识点中间
    下刀，等于把模型辛苦识别出的边界破坏掉；退回则给它机会重新理解这段内容
    —— 那里本来就可能藏着两个知识点。
    """
    if v is None:
        return ""
    lines: list[str] = []
    if v.hard:
        lines.append("上一次输出被程序拒绝，原因如下：")
        lines.extend(f"- {x}" for x in v.hard[:6])
    if v.soft:
        lines.append("上一次输出格式合法，但不符合要求：")
        lines.extend(f"- {x}" for x in v.soft[:6])
    over = [s for s in v.segments
            if v.max_segment_tokens and s.tokens > v.max_segment_tokens]
    if over:
        lines.append("以下片段仍然过重，请在其中继续寻找可切分的位置：")
        for s in over[:6]:
            lines.append(
                f"- 第 {s.ordinal} 段（序号 {s.start_ordinal}-{s.end_ordinal}，"
                f"约 {s.tokens} token，含 {s.ku_count} 个知识点）")
        lines.append(
            "如果确实无法在不破坏知识点完整性的前提下再切分，请在 notes 中说明原因。")
    if not lines:
        return ""
    lines.append("请重新输出完整的 JSON（knowledge_units + breaks），"
                 "覆盖全部序号，不要只输出修改的部分。")
    return "\n".join(lines)


def usable_breaks(v: BoundaryValidation) -> Optional[list[int]]:
    """校验通过时返回可用的切点，否则返回 ``None``（调用方走结构边界）。"""
    return list(v.breaks) if v.ok else None


# ---------------------------------------------------------------------------
# 数据库访问
# ---------------------------------------------------------------------------
def _table_exists(name: str) -> bool:
    try:
        row = db.fetch_one(
            "SELECT 1 AS ok FROM sqlite_master WHERE type='table' AND name=?", (name,))
    except Exception:  # pragma: no cover - 数据库未初始化
        return False
    return bool(row)


def load_canonical_spans(domain_id: int) -> list[dict]:
    """按域内顺序读取 canonical 非噪声 span（复用 segment 的口径，避免漂移）。"""
    from .segment import canonical_spans
    return canonical_spans(int(domain_id))


def load_boundary_plan(domain_id: int) -> Optional[dict]:
    """读取本域的边界建议（表不存在时返回 ``None``，兼容旧库）。"""
    if not _table_exists("segment_boundary_plans"):
        return None
    row = db.fetch_one(
        "SELECT * FROM segment_boundary_plans WHERE domain_id=? ORDER BY id DESC LIMIT 1",
        (int(domain_id),))
    return dict(row) if row else None


def _json_load(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return default
    return value if value is not None else default


def save_boundary_plan(*, domain_id: int, run_id: int, model_used: str,
                       input_hash: str, span_count: int, total_tokens: int,
                       max_segment_tokens: int, boundaries: dict,
                       validation: dict, status: str, detail: str = "") -> None:
    """落「建议账」。同域同 input_hash 覆盖更新（允许 fallback → succeeded 重试）。"""
    if not _table_exists("segment_boundary_plans"):
        return
    boundaries_json = json.dumps(boundaries or {}, ensure_ascii=False, sort_keys=True)
    validation_json = json.dumps(validation or {}, ensure_ascii=False, sort_keys=True)
    segment_count = int((validation or {}).get("segment_count") or 0)
    ku_count = int((validation or {}).get("ku_count") or 0)
    oversized_count = len((validation or {}).get("oversized_ku") or [])
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO segment_boundary_plans (domain_id, run_id, model_used, "
            " prompt_version, input_hash, span_count, total_tokens, max_segment_tokens, "
            " boundaries_json, validation_json, segment_count, ku_count, "
            " oversized_ku_count, status, detail, attempts, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1, datetime('now','localtime'), "
            " datetime('now','localtime')) "
            "ON CONFLICT(domain_id, input_hash) DO UPDATE SET "
            " run_id=excluded.run_id, model_used=excluded.model_used, "
            " boundaries_json=excluded.boundaries_json, "
            " validation_json=excluded.validation_json, "
            " segment_count=excluded.segment_count, ku_count=excluded.ku_count, "
            " oversized_ku_count=excluded.oversized_ku_count, status=excluded.status, "
            " detail=excluded.detail, attempts=segment_boundary_plans.attempts + 1, "
            " updated_at=excluded.updated_at",
            (int(domain_id), int(run_id), str(model_used or ""), BOUNDARY_PROMPT_VERSION,
             str(input_hash), int(span_count), int(total_tokens), int(max_segment_tokens),
             boundaries_json, validation_json, segment_count, ku_count,
             oversized_count, str(status), str(detail)[:500]),
        )


#: 公开视图的限量（避免超大材料把响应撑爆；与 API 层同口径）。
MAX_BOUNDARY_KUS_VIEW = 300
MAX_BOUNDARY_BREAKS_VIEW = 300
MAX_BOUNDARY_VIOLATIONS_VIEW = 10
MAX_BOUNDARY_OVERSIZED_VIEW = 20


def get_boundary_view(domain_id: int) -> Optional[dict]:
    """给 API / 前端的公开视图。

    安全边界（与 material-domain / coverage 同口径）：
    * 不返回材料原文、不返回任何文件系统路径；
    * 只返回 ordinal 区间、校验结论、降级原因与统计；
    * 超量列表截断并显式标记 ``truncated``，不静默丢。

    旧库无表、或该域尚无建议记录 → ``None``（调用方按 404 语义处理）。
    """
    row = load_boundary_plan(int(domain_id))
    if not row:
        return None
    boundaries = _json_load(row.get("boundaries_json"), {}) or {}
    validation = _json_load(row.get("validation_json"), {}) or {}
    kus = [k for k in (boundaries.get("knowledge_units") or []) if isinstance(k, dict)]
    breaks = [int(b) for b in (boundaries.get("breaks") or [])]
    hard = [str(x) for x in (validation.get("hard") or [])]
    soft = [str(x) for x in (validation.get("soft") or [])]
    oversized = [o for o in (validation.get("oversized_ku") or []) if isinstance(o, dict)]

    return {
        "status": str(row.get("status") or "pending"),
        "model_used": str(row.get("model_used") or ""),
        "prompt_version": str(row.get("prompt_version") or ""),
        "input_hash": str(row.get("input_hash") or ""),
        "span_count": int(row.get("span_count") or 0),
        "total_tokens": int(row.get("total_tokens") or 0),
        "max_segment_tokens": int(row.get("max_segment_tokens") or 0),
        "segment_count": int(row.get("segment_count") or 0),
        "ku_count": int(row.get("ku_count") or 0),
        "oversized_ku_count": int(row.get("oversized_ku_count") or 0),
        "attempts": int(row.get("attempts") or 0),
        "breaks": breaks[:MAX_BOUNDARY_BREAKS_VIEW],
        "knowledge_units": [
            {
                "name": str(k.get("name") or ""),
                "kind": str(k.get("kind") or ""),
                "start_ordinal": int(k.get("start_ordinal") or 0),
                "end_ordinal": int(k.get("end_ordinal") or 0),
            }
            for k in kus[:MAX_BOUNDARY_KUS_VIEW]
        ],
        "hard_violations": hard[:MAX_BOUNDARY_VIOLATIONS_VIEW],
        "soft_violations": soft[:MAX_BOUNDARY_VIOLATIONS_VIEW],
        "oversized_ku": oversized[:MAX_BOUNDARY_OVERSIZED_VIEW],
        "detail": str(row.get("detail") or ""),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "truncated": {
            "knowledge_units": len(kus) > MAX_BOUNDARY_KUS_VIEW,
            "breaks": len(breaks) > MAX_BOUNDARY_BREAKS_VIEW,
            "hard_violations": len(hard) > MAX_BOUNDARY_VIOLATIONS_VIEW,
            "soft_violations": len(soft) > MAX_BOUNDARY_VIOLATIONS_VIEW,
            "oversized_ku": len(oversized) > MAX_BOUNDARY_OVERSIZED_VIEW,
        },
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def resolve_max_segment_tokens(planning_model_profile_id: Optional[str]) -> int:
    """按理解模型的上下文能力推导「段的上界」。

    必须与 ``segment.plan_segments`` 装箱时用的是**同一个值**，否则会出现
    「模型说一段可行、装箱却必须切两段」的错配。
    """
    from .coverage import (DEFAULT_RESERVED_OUTPUT, DEFAULT_RESERVED_SYSTEM,
                           SAFETY_MARGIN, _model_context_window)
    from .segment import _performance_input_budget
    window, reserved_output = _model_context_window(planning_model_profile_id)
    usable = max(int(window * (1.0 - SAFETY_MARGIN))
                 - DEFAULT_RESERVED_SYSTEM - reserved_output, 0)
    return int(min(usable, _performance_input_budget()))


async def _call_boundary_model(model_id: str, spans: list[dict], *,
                               max_segment_tokens: int,
                               retry_hint: str = "") -> dict:
    """一次模型调用：渲染编号表 → 调网关 → schema 校验。"""
    from ..gateway import gateway
    from ..integrations.prompts import render_prompt
    from ..integrations.schemas import SegmentBoundariesOut, parse_model_output

    total_tokens = sum(int(s.get("token_count") or 0) for s in spans)
    p = render_prompt(
        "lesson/segment_boundaries", BOUNDARY_PROMPT_VERSION,
        span_count=str(len(spans)),
        total_tokens=str(total_tokens),
        max_segment_tokens=str(int(max_segment_tokens)),
        max_segment_chars=str(tokens_to_char_hint(max_segment_tokens)),
        min_spans_per_ku=str(MIN_SPANS_PER_KU),
        span_index=render_span_index(spans),
    )
    user_text = "下面是全部材料的编号表，请输出分段边界 JSON。"
    if retry_hint:
        user_text = f"{user_text}\n\n{retry_hint}"
    messages = [
        {"role": "system", "content": p["text"]},
        {"role": "user", "content": user_text},
    ]
    resp = await gateway.chat(
        model_id, messages,
        contract="lesson/segment_boundaries",
        temperature=0.1,          # 规划任务，稳定性优先
        max_tokens=BOUNDARY_MAX_OUTPUT_TOKENS,
        response_format={"type": "json_object"},
    )
    data = parse_model_output(SegmentBoundariesOut, resp.get("content", ""),
                              f"segment_boundaries#{len(spans)}")
    data["_prompt_checksum"] = p["checksum"]
    data["_model"] = model_id
    data["_gateway_model"] = resp.get("model") or ""
    data["_tokens_in"] = resp.get("tokens_in", 0)
    data["_tokens_out"] = resp.get("tokens_out", 0)
    return data


async def plan_segment_boundaries(domain_id: int, run_id: int, model_id: str, *,
                                  max_segment_tokens: Optional[int] = None,
                                  planning_model_profile_id: Optional[str] = None
                                  ) -> dict:
    """主流程：读全文 → 调用 → 校验 →（不合格）退回重画 → 落库。

    返回可直接并入 DAG 节点输出的 dict。**除取消外不抛异常**：任何失败都
    转成 ``status='fallback'``，由调用方回退结构边界。
    """
    import asyncio

    from ..dag import RunCancelledError

    spans = load_canonical_spans(int(domain_id))
    if not spans:
        return {"status": "fallback", "detail": "域内没有 canonical span",
                "segment_count": 0, "ku_count": 0, "breaks": []}

    limit = (int(max_segment_tokens) if max_segment_tokens is not None
             else resolve_max_segment_tokens(planning_model_profile_id))
    input_hash = compute_boundary_input_hash(spans, max_segment_tokens=limit)
    total_tokens = sum(int(s.get("token_count") or 0) for s in spans)

    # ---- 缓存命中：同域同哈希且曾成功 → 零模型调用 ----
    cached = load_boundary_plan(int(domain_id))
    if cached and cached.get("input_hash") == input_hash \
            and cached.get("status") == "succeeded":
        boundaries = _json_load(cached.get("boundaries_json"), {})
        return {
            "status": "succeeded", "reused": True, "input_hash": input_hash,
            "model_used": cached.get("model_used") or "",
            "segment_count": int(cached.get("segment_count") or 0),
            "ku_count": int(cached.get("ku_count") or 0),
            "breaks": list(boundaries.get("breaks") or []),
            "max_segment_tokens": limit, "total_tokens": total_tokens,
        }

    last_v: Optional[BoundaryValidation] = None
    data: dict = {}
    rounds = 0
    try:
        for round_no in range(1, MAX_RETRY_ROUNDS + 2):
            rounds = round_no
            data = await _call_boundary_model(
                model_id, spans, max_segment_tokens=limit,
                retry_hint=build_retry_hint(last_v))
            last_v = validate_boundaries(data, spans, max_segment_tokens=limit)
            if last_v.ok:
                break
            logger.info("分段建议第 %s 轮未通过：hard=%s soft=%s",
                        round_no, last_v.hard[:3], last_v.soft[:3])
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (asyncio.CancelledError, RunCancelledError)):
            raise                       # 取消必须穿透
        if not isinstance(exc, Exception):
            raise                       # KeyboardInterrupt 等不吞
        logger.warning("分段边界规划失败，回退结构边界: %s", exc)
        save_boundary_plan(
            domain_id=int(domain_id), run_id=int(run_id), model_used=model_id,
            input_hash=input_hash, span_count=len(spans), total_tokens=total_tokens,
            max_segment_tokens=limit, boundaries={},
            validation={"status": "fallback"}, status="fallback",
            detail=str(exc)[:300])
        return {"status": "fallback", "detail": str(exc)[:300], "rounds": rounds,
                "input_hash": input_hash, "max_segment_tokens": limit,
                "total_tokens": total_tokens, "segment_count": 0,
                "ku_count": 0, "breaks": []}

    v = last_v or BoundaryValidation()
    status = "succeeded" if v.ok else ("low_quality" if not v.hard else "fallback")
    save_boundary_plan(
        domain_id=int(domain_id), run_id=int(run_id), model_used=model_id,
        input_hash=input_hash, span_count=len(spans), total_tokens=total_tokens,
        max_segment_tokens=limit,
        boundaries={"breaks": v.breaks,
                    "knowledge_units": [k.__dict__ for k in v.knowledge_units]},
        validation=v.as_dict(), status=status,
        detail="；".join((v.hard + v.soft)[:4]))
    return {
        "status": status,
        "reused": False,
        "rounds": rounds,
        "input_hash": input_hash,
        "model_used": model_id,
        "segment_count": len(v.segments),
        "ku_count": len(v.knowledge_units),
        "oversized_ku_count": len(v.oversized_ku),
        "oversized_ku": v.oversized_ku[:10],
        "breaks": v.breaks,
        "hard_violations": v.hard[:6],
        "soft_violations": v.soft[:6],
        "max_segment_tokens": limit,
        "total_tokens": total_tokens,
        "notes": [str(n) for n in (data.get("notes") or [])][:10],
        "prompt_checksum": data.get("_prompt_checksum", ""),
        "gateway_model": data.get("_gateway_model", ""),
    }
