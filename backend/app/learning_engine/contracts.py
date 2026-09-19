"""V6 Learning Engine Phase 1 契约（Pydantic, ``extra='forbid'``）。

原则:

* 所有版本化输出携带 ``schema_version``。
* 枚举显式；非法值直接 ``ValidationError``，不静默忽略。
* 不虚构缺失内容：可选字段默认 ``None``，绝不把缺失内容补成 ``""`` 后当有效材料。
* 覆盖指标一律由确定性程序计算（``coverage.py``），模型不得填写。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "v6.1"

# 覆盖率下限（Phase 2）。任务书 §八要求语义处理率固定 100%：Phase 2 已实现全量
# segment understanding，任何 canonical 非噪声 span 未进入理解都不得 completed。
# 该值**不可通过环境变量放宽**。
DEFAULT_SEMANTIC_RATE_FLOOR = 1.0


class StrictModel(BaseModel):
    """V6 全部契约的基类：多余字段直接报错。"""

    model_config = ConfigDict(extra="forbid")


class SourceKind(str, Enum):
    TRANSCRIPT = "transcript"
    PPT = "ppt"
    PDF = "pdf"
    DOC = "doc"
    TEXT = "text"
    OCR = "ocr"
    UNKNOWN = "unknown"


class DomainItemState(str, Enum):
    INCLUDED = "included"
    DUPLICATE = "duplicate"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    EXCLUDED_WITH_REASON = "excluded_with_reason"


#: 这些状态必须携带 reason_code（数据库 CHECK 与应用层双重强制）。
REASON_REQUIRED_STATES = frozenset({
    DomainItemState.UNSUPPORTED,
    DomainItemState.FAILED,
    DomainItemState.EXCLUDED_WITH_REASON,
})

#: 需要终态、但不允许作为「已计入覆盖」的 item 状态。
UNACCOUNTED_ITEM_STATES = REASON_REQUIRED_STATES


class SpanState(str, Enum):
    INCLUDED = "included"
    DUPLICATE = "duplicate"
    NOISE = "noise"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    EXCLUDED_WITH_REASON = "excluded_with_reason"


#: 这些 span 状态必须携带 reason_code。
REASON_REQUIRED_SPAN_STATES = frozenset({
    SpanState.DUPLICATE,
    SpanState.NOISE,
    SpanState.UNSUPPORTED,
    SpanState.FAILED,
    SpanState.EXCLUDED_WITH_REASON,
})


class ItemReason(str, Enum):
    """受控 reason_code（避免自由文本导致审计不可聚合）。"""

    DUPLICATE_CONTENT_HASH = "duplicate_content_hash"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    UNSUPPORTED_PARSE_STATE = "unsupported_parse_state"
    MATERIAL_MISSING = "material_missing"
    MATERIAL_FILE_MISSING = "material_file_missing"
    MATERIAL_NOT_IN_SCOPE = "material_not_in_scope"
    PARSE_FAILED = "parse_failed"
    PARSE_EMPTY = "parse_empty"
    TRANSCRIPT_EMPTY = "transcript_empty"
    OPTIONAL_NOT_SELECTED = "optional_not_selected"
    REQUIRED_BUT_EXCLUDED = "required_but_excluded"


class SpanReason(str, Enum):
    DUPLICATE_CONTENT_HASH = "duplicate_content_hash"
    DUPLICATE_NORMALIZED_TEXT = "duplicate_normalized_text"
    DUPLICATE_IDENTICAL_TEXT = "duplicate_identical_text"
    NOISE_EMPTY_TEXT = "noise_empty_text"
    NOISE_PLACEHOLDER = "noise_placeholder"
    NOISE_PARSE_MARKER = "noise_parse_marker"
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    FAILED_PARSE = "failed_parse"
    EXCLUDED_UNASSIGNED = "excluded_unassigned"


class LedgerOutcome(str, Enum):
    PROCESSED = "processed"
    NOT_USED = "not_used"
    DUPLICATE = "duplicate"
    NOISE = "noise"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    EXCLUDED = "excluded"


#: 受控的 not_used 原因（outcome=not_used 必须有 reason_code）。
class NotUsedReason(str, Enum):
    NOT_SELECTED_BY_LEGACY_TOP_K = "not_selected_by_legacy_top_k"
    BUDGET_TRUNCATED = "budget_truncated"
    OUT_OF_CONTEXT_WINDOW = "out_of_context_window"
    SEGMENT_NOT_REACHED = "segment_not_reached"
    LOWER_INFORMATION_VALUE = "lower_information_value"


class LedgerStage(str, Enum):
    NORMALIZE = "normalize"
    DEDUPLICATE = "deduplicate"
    SOURCE_MAP = "source_map"
    PLAN_COVERAGE = "plan_coverage"
    #: 旧 V5 生成链候选窗口实际消费的 span（Phase 2 起只作对照，不计入语义处理率）
    LEGACY_CANDIDATE = "legacy_candidate"
    #: Phase 2 全量理解：每个 canonical 非噪声 span 都必须在此 stage 有终态
    UNDERSTAND_SEGMENTS = "understand_segments"
    COMPOSE = "compose"
    PUBLISH = "publish"
    COVERAGE_AUDIT = "coverage_audit"


class PlanStrategy(str, Enum):
    SINGLE_PASS = "single_pass"
    SEGMENTED_MAP_MERGE = "segmented_map_merge"


class Gate(str, Enum):
    PASSED = "passed"
    DEGRADED = "degraded"
    FAILED = "failed"


class DomainScope(str, Enum):
    LESSON = "lesson"
    CHAPTER = "chapter"
    COURSE = "course"


class DomainState(str, Enum):
    DRAFT = "draft"
    FROZEN = "frozen"


class DomainItem(StrictModel):
    domain_item_id: Optional[int] = None
    material_id: Optional[int] = None
    source_kind: SourceKind
    locator: Optional[str] = None
    ordinal: int = Field(ge=1)
    required: bool = True
    raw_chars: int = Field(default=0, ge=0)
    raw_tokens: int = Field(default=0, ge=0)
    content_hash: Optional[str] = None
    state: DomainItemState = DomainItemState.INCLUDED
    reason_code: Optional[ItemReason] = None
    reason_detail: Optional[str] = None
    duplicate_of_item_id: Optional[int] = None

    @model_validator(mode="after")
    def _check_state_contract(self) -> "DomainItem":
        if self.state in REASON_REQUIRED_STATES and not self.reason_code:
            raise ValueError(f"state={self.state.value} 必须提供 reason_code")
        if self.state == DomainItemState.DUPLICATE and self.duplicate_of_item_id is None:
            raise ValueError("duplicate item 必须提供 duplicate_of_item_id")
        if self.state != DomainItemState.DUPLICATE and self.duplicate_of_item_id is not None:
            raise ValueError("duplicate_of_item_id 只允许出现在 duplicate 状态")
        # 注意: ``duplicate`` 是自动推导状态（同一内容哈希被引用多次），它不是
        # 「材料被丢弃」——两个引用都保留账目，只是不重复进入语义理解。因此
        # duplicate 允许出现在 required item 上。真正需要拦截的是「required 材料
        # 被显式排除/解析失败」，该判断在 domain.py 中完成（需要 run 级上下文）。
        return self


class MaterialDomainSnapshot(StrictModel):
    schema_version: str = SCHEMA_VERSION
    domain_id: Optional[int] = None
    run_id: int
    scope: DomainScope = DomainScope.LESSON
    course_id: Optional[int] = None
    chapter_id: Optional[int] = None
    lesson_id: Optional[int] = None
    version: int = Field(default=1, ge=1)
    domain_hash: str
    state: DomainState = DomainState.FROZEN
    engine_version: str = "v6-phase1"
    transcript_chars: int = Field(default=0, ge=0)
    frozen_at: Optional[str] = None
    items: list[DomainItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_materials(self) -> "MaterialDomainSnapshot":
        seen: set[int] = set()
        for item in self.items:
            if item.material_id is None:
                continue
            if item.material_id in seen:
                raise ValueError(f"同一 domain 内 material_id={item.material_id} 重复")
            seen.add(item.material_id)
        ordinals = [i.ordinal for i in self.items]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("domain item ordinal 必须唯一")
        return self


class SourceSpan(StrictModel):
    schema_version: str = SCHEMA_VERSION
    span_id: Optional[int] = None
    source_id: str = Field(min_length=1)
    domain_id: int
    domain_item_id: Optional[int] = None
    source_chunk_id: Optional[int] = None
    material_id: Optional[int] = None
    source_kind: SourceKind
    locator: Optional[str] = None
    ordinal: int = Field(ge=1)
    start_ms: Optional[int] = Field(default=None, ge=0)
    end_ms: Optional[int] = Field(default=None, ge=0)
    page_no: Optional[int] = Field(default=None, ge=1)
    slide_no: Optional[int] = Field(default=None, ge=1)
    text: str = ""
    normalized_text_hash: str
    token_count: int = Field(default=0, ge=0)
    char_count: int = Field(default=0, ge=0)
    canonical_span_id: Optional[int] = None
    span_state: SpanState = SpanState.INCLUDED
    reason_code: Optional[SpanReason] = None
    reason_detail: Optional[str] = None

    @model_validator(mode="after")
    def _check_span_contract(self) -> "SourceSpan":
        if self.span_state in REASON_REQUIRED_SPAN_STATES and not self.reason_code:
            raise ValueError(f"span_state={self.span_state.value} 必须提供 reason_code")
        if self.span_state == SpanState.DUPLICATE and self.canonical_span_id is None:
            raise ValueError("duplicate span 必须提供 canonical_span_id")
        if self.span_state != SpanState.DUPLICATE and self.canonical_span_id is not None:
            raise ValueError("canonical_span_id 只允许出现在 duplicate 状态")
        if self.start_ms is not None and self.end_ms is not None and self.end_ms < self.start_ms:
            raise ValueError("end_ms 不得小于 start_ms")
        return self

    @property
    def is_canonical_non_noise(self) -> bool:
        """唯一、非噪声 —— 覆盖率的语义处理分母只统计这些 span。"""
        return self.span_state == SpanState.INCLUDED


class CoveragePlanModel(StrictModel):
    schema_version: str = SCHEMA_VERSION
    plan_id: Optional[int] = None
    domain_id: int
    strategy: PlanStrategy
    model_profile_id: Optional[str] = None
    context_window: int = Field(default=0, ge=0)
    reserved_output_tokens: int = Field(default=0, ge=0)
    reserved_system_tokens: int = Field(default=0, ge=0)
    input_budget_tokens: int = Field(default=0, ge=0)
    output_budget_tokens: int = Field(default=0, ge=0)
    planned_span_count: int = Field(default=0, ge=0)
    segment_count: int = Field(default=1, ge=1)
    unassigned_count: int = Field(default=0, ge=0)
    unassigned_source_ids: list[str] = Field(default_factory=list)
    assignments: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_plan_consistent(self) -> "CoveragePlanModel":
        if self.unassigned_count != len(self.unassigned_source_ids):
            raise ValueError("unassigned_count 必须等于 unassigned_source_ids 长度")
        if self.unassigned_count and self.unassigned_source_ids:
            # unassigned 非空即意味着计划本身不可用（硬门禁）。
            pass
        if self.strategy == PlanStrategy.SINGLE_PASS and self.segment_count != 1:
            raise ValueError("single_pass 的 segment_count 必须为 1")
        return self


class CoverageLedgerEntry(StrictModel):
    schema_version: str = SCHEMA_VERSION
    ledger_id: Optional[int] = None
    run_id: int
    domain_id: int
    source_span_id: Optional[int] = None
    source_id: str
    stage: LedgerStage
    outcome: LedgerOutcome
    reason_code: Optional[str] = None
    reason_detail: Optional[str] = None
    segment_id: Optional[int] = None
    knowledge_unit_id: Optional[int] = None

    @model_validator(mode="after")
    def _check_outcome_contract(self) -> "CoverageLedgerEntry":
        if self.outcome == LedgerOutcome.NOT_USED and not self.reason_code:
            raise ValueError("outcome=not_used 必须提供 reason_code")
        if self.outcome in (LedgerOutcome.DUPLICATE, LedgerOutcome.NOISE,
                            LedgerOutcome.UNSUPPORTED, LedgerOutcome.FAILED,
                            LedgerOutcome.EXCLUDED) and not self.reason_code:
            raise ValueError(f"outcome={self.outcome.value} 必须提供 reason_code")
        return self


class MaterialCoverageReport(StrictModel):
    """覆盖报告。所有比率字段只允许由 ``coverage.py`` 的确定性程序填写。"""

    schema_version: str = SCHEMA_VERSION
    run_id: int
    domain_id: int
    engine_version: str = "v6-phase1"
    engine_mode: str = "shadow"

    total_domain_items: int = Field(default=0, ge=0)
    unique_domain_items: int = Field(default=0, ge=0)
    terminal_domain_items: int = Field(default=0, ge=0)
    unaccounted_domain_items: int = Field(default=0, ge=0)

    total_source_spans: int = Field(default=0, ge=0)
    canonical_non_noise_spans: int = Field(default=0, ge=0)
    duplicate_spans: int = Field(default=0, ge=0)
    noise_spans: int = Field(default=0, ge=0)
    unsupported_spans: int = Field(default=0, ge=0)
    failed_spans: int = Field(default=0, ge=0)

    planned_span_count: int = Field(default=0, ge=0)
    unassigned_count: int = Field(default=0, ge=0)
    processed_spans: int = Field(default=0, ge=0)
    silent_dropped: int = Field(default=0, ge=0)

    domain_accounting_rate: float = 0.0
    semantic_processing_rate: float = 0.0
    timeline_coverage_rate: float = 0.0
    timeline_span_total: int = Field(default=0, ge=0)
    timeline_span_processed: int = Field(default=0, ge=0)
    timeline_start_ms: Optional[int] = None
    timeline_end_ms: Optional[int] = None
    ppt_pages_total: int = Field(default=0, ge=0)
    ppt_pages_processed: int = Field(default=0, ge=0)

    source_refs_total: int = Field(default=0, ge=0)
    source_refs_valid: int = Field(default=0, ge=0)
    source_refs_invalid: int = Field(default=0, ge=0)

    # ---- required 材料专项（缺陷 4：required 材料无内容绝不允许 passed）----
    required_items_total: int = Field(default=0, ge=0)
    required_items_failed: int = Field(default=0, ge=0)
    required_items_unsupported: int = Field(default=0, ge=0)
    required_items_without_canonical_span: int = Field(default=0, ge=0)
    required_spans_unprocessed: int = Field(default=0, ge=0)

    # ---- Phase 2：segment 与 merge ----
    segment_count: int = Field(default=0, ge=0)
    segment_failed_count: int = Field(default=0, ge=0)
    segments_consumed_by_merge: int = Field(default=0, ge=0)
    merge_consumed_all_segments: bool = False
    #: 结构级 coverage：定义/公式/推导/例题在 merge 中的守恒
    segment_structures_total: int = Field(default=0, ge=0)
    merged_structures_total: int = Field(default=0, ge=0)
    structures_dropped: int = Field(default=0, ge=0)

    # ---- Phase 2 增强：段内引用覆盖率（**观测，不参与门禁**）----
    # 段成功 ⇒ 该段全部 primary span 仍照旧记账为 processed，覆盖率语义不变。
    # 这四个字段额外回答「模型真的注意到多少段内内容」，用于把长上下文下的
    # 注意力衰减（中间迷失）从黑盒变成可定位的问题。
    # 刻意不纳入 evaluate_gate：课堂材料里大量过渡性内容本就不会被引用，
    # 按引用率卡门禁会让几乎所有 run 误降级。
    segment_ref_coverage_min: float = 0.0
    segment_ref_coverage_avg: float = 0.0
    low_ref_coverage_count: int = Field(default=0, ge=0)
    low_ref_coverage_segments: list[dict[str, Any]] = Field(default_factory=list)

    #: 旧 V5 生成链候选窗口的真实消费率（仅作对照，不参与门禁）。
    legacy_candidate_rate: float = 0.0
    timeline_gap_count: int = Field(default=0, ge=0)

    semantic_rate_floor: float = DEFAULT_SEMANTIC_RATE_FLOOR
    gate: Gate = Gate.DEGRADED
    degradation_reason: Optional[str] = None
    issues: list[str] = Field(default_factory=list)
    unprocessed_reasons: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_gate_consistency(self) -> "MaterialCoverageReport":
        if self.gate == Gate.PASSED:
            blocking = []
            if self.silent_dropped > 0:
                blocking.append("silent_dropped>0")
            if self.domain_accounting_rate < 1.0:
                blocking.append("domain_accounting_rate<1")
            if self.unassigned_count > 0:
                blocking.append("unassigned_count>0")
            if self.required_items_failed > 0:
                blocking.append("required_items_failed>0")
            if self.required_items_unsupported > 0:
                blocking.append("required_items_unsupported>0")
            if self.required_items_without_canonical_span > 0:
                blocking.append("required_items_without_canonical_span>0")
            if self.semantic_processing_rate + 1e-9 < self.semantic_rate_floor:
                blocking.append("semantic_processing_rate<floor")
            if self.required_spans_unprocessed > 0:
                blocking.append("required_spans_unprocessed>0")
            if self.source_refs_invalid > 0:
                blocking.append("source_refs_invalid>0")
            if self.structures_dropped > 0:
                blocking.append("structures_dropped>0")
            if blocking:
                raise ValueError(
                    "硬门禁不满足时不得标记 passed: " + ", ".join(blocking)
                )
        return self


#: 供 API 复用的导出（避免调用方重新拼装字段名）。
class SegmentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SegmentSourceRole(str, Enum):
    """span 在 segment 中的角色。

    覆盖率**只按 primary 计算**：overlap 只为模型提供上下文，不得被计入
    「已处理」，否则一段重叠就能虚增覆盖率。
    """

    PRIMARY = "primary"
    OVERLAP = "overlap"


class LessonSegment(StrictModel):
    """lesson_segments 的契约投影。"""

    schema_version: str = SCHEMA_VERSION
    segment_id: Optional[int] = None
    run_id: int
    domain_id: int
    ordinal: int = Field(ge=1)
    strategy: PlanStrategy = PlanStrategy.SINGLE_PASS
    title: Optional[str] = None
    input_hash: str
    token_count: int = Field(default=0, ge=0)
    primary_span_count: int = Field(default=0, ge=0)
    overlap_span_count: int = Field(default=0, ge=0)
    primary_source_ids: list[str] = Field(default_factory=list)
    overlap_source_ids: list[str] = Field(default_factory=list)
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    status: SegmentStatus = SegmentStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    error: Optional[str] = None


class KnowledgeUnitDraft(StrictModel):
    """单段理解产出的知识单元草案（尚未全局合并）。"""

    temp_id: str = Field(min_length=1)
    topic: str = ""
    kind: str = "concept"
    summary: str = ""
    source_refs: list[str] = Field(default_factory=list)
    teacher_emphasis: float = Field(default=0.0, ge=0.0, le=1.0)
    relations: list[str] = Field(default_factory=list)


class DefinitionDraft(StrictModel):
    term: str = ""
    meaning: str = ""
    source_refs: list[str] = Field(default_factory=list)


class FormulaDraft(StrictModel):
    expression: str = ""
    meaning: str = ""
    source_refs: list[str] = Field(default_factory=list)


class DerivationDraft(StrictModel):
    goal: str = ""
    steps: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)


class ExampleDraft(StrictModel):
    prompt: str = ""
    solution: str = ""
    source_refs: list[str] = Field(default_factory=list)


class SegmentUnderstanding(StrictModel):
    """单段理解结果（模型输出，``extra='forbid'``）。

    模型**只允许**引用输入中真实存在的 Source ID；不得要求模型复制 exact quote
    （原文由程序按 Source ID 从 Source Map 取，见 Phase 4 Evidence V2）。
    """

    schema_version: str = SCHEMA_VERSION
    understanding_id: Optional[int] = None
    segment_id: int
    run_id: int
    domain_id: int
    topics: list[str] = Field(default_factory=list)
    knowledge_units: list[KnowledgeUnitDraft] = Field(default_factory=list)
    definitions: list[DefinitionDraft] = Field(default_factory=list)
    formulas: list[FormulaDraft] = Field(default_factory=list)
    derivations: list[DerivationDraft] = Field(default_factory=list)
    examples: list[ExampleDraft] = Field(default_factory=list)
    teacher_emphasis: float = Field(default=0.0, ge=0.0, le=1.0)
    unresolved_points: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    prompt_version: str = "v1"
    model_used: str = ""


class MergedKnowledgeUnit(StrictModel):
    id: str = Field(min_length=1)
    topic: str = ""
    kind: str = "concept"
    summary: str = ""
    source_refs: list[str] = Field(default_factory=list)
    teacher_emphasis: float = Field(default=0.0, ge=0.0, le=1.0)
    relations: list[str] = Field(default_factory=list)
    segment_ids: list[int] = Field(default_factory=list)


class LessonUnderstanding(StrictModel):
    """全局理解（节点 08 输出）。必须消费**全部** primary segment。"""

    schema_version: str = SCHEMA_VERSION
    understanding_id: Optional[int] = None
    run_id: int
    domain_id: int
    lesson_id: Optional[int] = None
    input_hash: str
    topics: list[str] = Field(default_factory=list)
    knowledge_units: list[MergedKnowledgeUnit] = Field(default_factory=list)
    definitions: list[DefinitionDraft] = Field(default_factory=list)
    formulas: list[FormulaDraft] = Field(default_factory=list)
    derivations: list[DerivationDraft] = Field(default_factory=list)
    examples: list[ExampleDraft] = Field(default_factory=list)
    teacher_emphasis: float = Field(default=0.0, ge=0.0, le=1.0)
    unresolved_conflicts: list[str] = Field(default_factory=list)
    consumed_segment_ids: list[int] = Field(default_factory=list)
    consumed_segment_count: int = Field(default=0, ge=0)
    segment_count: int = Field(default=0, ge=0)
    valid_source_ids: list[str] = Field(default_factory=list)
    prompt_version: str = "v1"
    model_used: str = ""
    merge_levels: int = Field(default=1, ge=1)


class CognitiveItemType(str, Enum):
    """Phase 3 八类认知项（设计文档 §6.6）。"""

    CONFUSION_POINT = "confusion_point"
    PREREQUISITE_GAP = "prerequisite_gap"
    PITFALL = "pitfall"
    EMPHASIS = "emphasis"
    CONCEPT_RELATION = "concept_relation"
    MEMORY_ANCHOR = "memory_anchor"
    MISSING_STEP = "missing_step"
    DIFFICULTY = "difficulty"


class CognitiveOrigin(str, Enum):
    """认知项来源。模型推断与课堂证据必须可区分。"""

    CLASSROOM_EVIDENCE = "classroom_evidence"
    CONFIRMED_ERROR = "confirmed_error"
    MODEL_COGNITIVE_INFERENCE = "model_cognitive_inference"
    CHAPTER_NOTE = "chapter_note"
    MASTERY_SIGNAL = "mastery_signal"
    HOMEWORK_FEEDBACK = "homework_feedback"


class CognitiveTreatment(str, Enum):
    EXPLANATION = "explanation"
    EXAMPLE = "example"
    CONTRAST = "contrast"
    DRILL = "drill"
    MEMORY_HOOK = "memory_hook"
    PREREQUISITE_REVIEW = "prerequisite_review"
    NONE = "none"


class CognitiveStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class CognitiveMapStatus(str, Enum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    #: 没有识别到需要额外认知加工的内容（**不是**失败，也不虚构 pitfall）
    EMPTY = "empty"


#: 需要课堂 Source ID 支持的「课堂事实型」认知项。
COGNITIVE_TYPES_REQUIRING_SOURCE = frozenset({
    CognitiveItemType.PITFALL,
    CognitiveItemType.EMPHASIS,
    CognitiveItemType.MISSING_STEP,
    CognitiveItemType.CONFUSION_POINT,
})


class CognitiveItem(StrictModel):
    """单个认知项。``extra='forbid'``；severity/confidence 限制在 [0,1]。"""

    schema_version: str = SCHEMA_VERSION
    item_id: Optional[int] = None
    stable_key: str = Field(min_length=1)
    item_type: CognitiveItemType
    title: str = ""
    explanation: str = ""
    severity: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    recommended_treatment: CognitiveTreatment = CognitiveTreatment.EXPLANATION
    origin: CognitiveOrigin = CognitiveOrigin.MODEL_COGNITIVE_INFERENCE
    status: CognitiveStatus = CognitiveStatus.PROPOSED
    knowledge_unit_refs: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    #: Phase 4.1：本 domain 内合法、但**不属于产出该 item 的那次模型调用**的来源。
    #: 保留以便 Evidence V2 判为「未进入该 producer 输入」并阻断门禁。
    out_of_batch_source_refs: list[str] = Field(default_factory=list)
    #: Phase 4.1：产出该 item 的 invocation 级可见性 provenance（逐 batch）。
    provenance: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_item_contract(self) -> "CognitiveItem":
        # 每个 item 至少关联一个 Knowledge Unit（任务书 §十二）
        if not self.knowledge_unit_refs:
            raise ValueError(
                f"cognitive item {self.stable_key} 必须至少关联一个 Knowledge Unit"
            )
        # 课堂事实型 item 必须绑定真实 Source ID
        if self.item_type in COGNITIVE_TYPES_REQUIRING_SOURCE and not self.source_refs:
            raise ValueError(
                f"cognitive item {self.stable_key}(type={self.item_type.value}) "
                "必须绑定至少一个课堂 Source ID"
            )
        # 模型推断必须显式标注，不得伪装成课堂证据
        if self.origin == CognitiveOrigin.CLASSROOM_EVIDENCE and not self.source_refs:
            raise ValueError("origin=classroom_evidence 必须提供 source_refs")
        return self


class CognitiveMap(StrictModel):
    """Phase 3 认知图谱（节点 09 输出）。"""

    schema_version: str = SCHEMA_VERSION
    run_id: int
    domain_id: int
    lesson_id: Optional[int] = None
    status: CognitiveMapStatus = CognitiveMapStatus.SUCCEEDED
    knowledge_unit_total: int = Field(default=0, ge=0)
    knowledge_unit_processed: int = Field(default=0, ge=0)
    cognitive_input_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    batch_count: int = Field(default=0, ge=0)
    items: list[CognitiveItem] = Field(default_factory=list)
    by_type: dict[str, int] = Field(default_factory=dict)
    input_hash: str = ""
    content_hash: str = ""
    prompt_version: str = "v1"
    model_used: str = ""
    error: Optional[str] = None

    @model_validator(mode="after")
    def _check_map_contract(self) -> "CognitiveMap":
        if self.knowledge_unit_processed > self.knowledge_unit_total:
            raise ValueError("knowledge_unit_processed 不得大于 total")
        if self.status == CognitiveMapStatus.EMPTY and self.items:
            raise ValueError("status=empty 时不得包含认知项")
        keys = [i.stable_key for i in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("cognitive item stable_key 必须唯一")
        return self


class ClaimType(str, Enum):
    """主张类型（决定证据要求；与 0020 的 CHECK 一一对应）。

    * ``CLASSROOM_FACT``       —— 课堂事实（教师原话 / 材料直接支持），**必须**有
      有效且模型实际可见的 Source ID。
    * ``CLASSROOM_PARAPHRASE`` —— 课堂重述：仍须绑定真实 Source ID，但 Binder 只
      证明「绑定存在」，**不**声称已证明语义等价（语义审查属 Phase 5 Critic）。
    * ``AI_EXPLANATION``       —— 模型教学补充：``evidence_status=not_required``，
      不计入课堂事实证据率分母，且必须保留明确类型，绝不降级成课堂事实。
    """

    CLASSROOM_FACT = "classroom_fact"
    CLASSROOM_PARAPHRASE = "classroom_paraphrase"
    AI_EXPLANATION = "ai_explanation"


#: 需要课堂 Source ID 支持的 claim 类型（证据率分母）。
CLAIM_TYPES_REQUIRING_SOURCE = frozenset({
    ClaimType.CLASSROOM_FACT,
    ClaimType.CLASSROOM_PARAPHRASE,
})


class ClaimImportance(str, Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    SUPPORTING = "supporting"


class ClaimEvidenceStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    NOT_REQUIRED = "not_required"


class ClaimSourceRelation(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    ILLUSTRATES = "illustrates"


class ClaimBindingStatus(str, Enum):
    """逐来源绑定结果。

    * ``BOUND``        —— 在本 domain 找到 span，``bound_quote`` 取自数据库。
    * ``INVALID``      —— 格式非法或**不属于当前 domain**（跨域 / 伪造）。
    * ``MISSING``      —— 格式合法、本 domain 内不存在该 Source ID。
    * ``NOT_REQUIRED`` —— 该 claim 类型不要求来源（``ai_explanation``）。
    """

    BOUND = "bound"
    INVALID = "invalid"
    MISSING = "missing"
    NOT_REQUIRED = "not_required"


class ClaimBindingMethod(str, Enum):
    """绑定方式。Phase 4 只允许确定性 Source ID 绑定（不得模糊检索）。"""

    DETERMINISTIC_SOURCE_ID = "deterministic_source_id"


class VisibilityBasis(str, Enum):
    """生产可见性的依据来源（必须持久化，不得只存 run/domain 级集合）。

    * ``SEGMENT_INPUT_LEDGER``   —— LessonUnderstanding 的 segment 输入账本
      （``segment_source_spans``，Phase 2 已持久化）。
    * ``COGNITIVE_BATCH_AUDIT``  —— CognitiveMap 的逐 batch 审计记录
      （``cognitive_maps.structured_json.batches``）。
    * ``UNAVAILABLE``            —— provenance 缺失/损坏：claim 必须 failed，
      **绝不** fallback 到全局并集。
    """

    SEGMENT_INPUT_LEDGER = "segment_input_ledger"
    COGNITIVE_BATCH_AUDIT = "cognitive_batch_audit"
    UNAVAILABLE = "unavailable"


class ProducerKind(str, Enum):
    LESSON_UNDERSTANDING = "lesson_understanding"
    COGNITIVE_MAP = "cognitive_map"


class ClaimSourceVisibility(str, Enum):
    """某条来源相对**该 claim 自己的 producer invocation** 的可见性。"""

    VISIBLE = "visible"
    NOT_VISIBLE = "not_visible"
    UNKNOWN = "unknown"


class ClaimProvenance(StrictModel):
    """单条 claim 的生产可见性 provenance（Phase 4.1）。

    这是「模型实际读过该来源」的**唯一**依据：逐 claim、逐 producer invocation。
    只存 run/domain/模型级集合是不允许的 —— 那正是 Phase 4 的 P1 缺陷。
    """

    producer_stage: str = ""
    producer_node: str = ""
    producer_kind: ProducerKind
    #: 该 claim 来自哪些 segment ordinal / cognitive batch ordinal（保序去重）
    invocation_refs: list[int] = Field(default_factory=list)
    #: 该 producer invocation **实际发送**的 Source ID
    visible_source_ids: list[str] = Field(default_factory=list)
    visibility_basis: VisibilityBasis = VisibilityBasis.UNAVAILABLE
    provenance_version: str = ""
    #: 多 invocation 合并时，逐来源记录「哪些 invocation 真的发过它」
    per_source_invocations: dict[str, list[int]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_provenance(self) -> "ClaimProvenance":
        if self.visibility_basis != VisibilityBasis.UNAVAILABLE and not self.producer_kind:
            raise ValueError("provenance 必须声明 producer_kind")
        return self


def provenance_visible_for(provenance: ClaimProvenance, source_id: str) -> ClaimSourceVisibility:
    """该来源是否进入了**这条 claim 的 producer invocation**。

    只有 ``visible_source_ids`` 里的来源才算「模型已读」；其他 batch 读过、
    或仅存在于 domain，都返回 ``NOT_VISIBLE``。provenance 不可用 → ``UNKNOWN``。
    """
    if provenance.visibility_basis == VisibilityBasis.UNAVAILABLE:
        return ClaimSourceVisibility.UNKNOWN
    return (ClaimSourceVisibility.VISIBLE if source_id in set(provenance.visible_source_ids)
            else ClaimSourceVisibility.NOT_VISIBLE)


class ClaimDraft(StrictModel):
    """确定性投影产出的 claim 草稿（**不是**模型输出）。

    ``source_refs`` 只允许是 Source ID 字符串；模型若给出 quote 字段，一律被
    Binder 忽略（``bound_quote`` 只能来自数据库）。

    ``provenance`` 是 Phase 4.1 的核心：它记录**这条 claim 自己的** producer
    invocation 实际发送过哪些来源。缺失即 ``UNAVAILABLE`` → claim failed。
    """

    claim_key: str = Field(min_length=1)
    claim_type: ClaimType
    claim_text: str = ""
    importance: ClaimImportance = ClaimImportance.SUPPORTING
    source_refs: list[str] = Field(default_factory=list)
    #: 来源对象的稳定标识（KU id / cognitive stable_key / structure key …）
    origin_ref: str = ""
    producer_node: str = ""
    producer_input_hash: str = ""
    provenance: ClaimProvenance = Field(
        default_factory=lambda: ClaimProvenance(
            producer_kind=ProducerKind.LESSON_UNDERSTANDING,
            visibility_basis=VisibilityBasis.UNAVAILABLE))

    @model_validator(mode="after")
    def _check_draft(self) -> "ClaimDraft":
        if self.claim_type in CLAIM_TYPES_REQUIRING_SOURCE and not self.source_refs:
            # 不是立刻报错：Binder 需要把「缺来源的课堂事实」记为 failed 并出报告，
            # 因此这里只拒绝「空 claim_key」这类结构性问题。
            pass
        if self.claim_type == ClaimType.AI_EXPLANATION and self.source_refs:
            # ai_explanation 可以附带来源（例如解释所依据的材料），但不得因此
            # 被当成课堂事实 —— 类型字段说了算。
            pass
        return self


class BoundClaimSource(StrictModel):
    """单个来源的绑定结果（``bound_quote`` 一律来自 ``source_spans.text``）。"""

    source_id: str
    source_span_id: Optional[int] = None
    relation: ClaimSourceRelation = ClaimSourceRelation.SUPPORTS
    binding_status: ClaimBindingStatus
    binding_method: ClaimBindingMethod = ClaimBindingMethod.DETERMINISTIC_SOURCE_ID
    bound_quote: str = ""
    quote_hash: str = ""
    source_text_hash: str = ""
    locator: Optional[str] = None
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    page_no: Optional[int] = None
    slide_no: Optional[int] = None
    source_kind: Optional[str] = None
    presented_to_producer: bool = False
    #: Phase 4.1：该来源相对**本 claim 的 producer invocation** 的可见性
    visibility: ClaimSourceVisibility = ClaimSourceVisibility.UNKNOWN
    #: 真正发过该来源的 invocation（segment/batch ordinal）
    visible_invocations: list[int] = Field(default_factory=list)
    failure_reason: Optional[str] = None


class BoundEvidence(StrictModel):
    """单个 claim 的绑定结论。"""

    claim_key: str
    claim_id: Optional[int] = None
    claim_type: ClaimType
    importance: ClaimImportance
    evidence_status: ClaimEvidenceStatus
    provenance: Optional[ClaimProvenance] = None
    sources: list[BoundClaimSource] = Field(default_factory=list)
    failure_reasons: list[str] = Field(default_factory=list)

    @property
    def requires_source(self) -> bool:
        return self.claim_type in CLAIM_TYPES_REQUIRING_SOURCE


class EvidenceReport(StrictModel):
    """Evidence V2 覆盖/门禁报告（全部数值由确定性程序计算）。"""

    run_id: int
    domain_id: int
    engine_version: str = ""
    engine_mode: str = ""
    claims_total: int = Field(default=0, ge=0)
    required_claims: int = Field(default=0, ge=0)
    bound_claims: int = Field(default=0, ge=0)
    failed_claims: int = Field(default=0, ge=0)
    not_required_claims: int = Field(default=0, ge=0)
    critical_claims: int = Field(default=0, ge=0)
    critical_claims_bound: int = Field(default=0, ge=0)
    critical_claim_evidence_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    #: critical 且需要课堂证据的 claim 总数；为 0 时证据率记 1.0，但**必须**让
    #: 调用方看到分母为 0，不得伪装成「验证了很多证据」。
    critical_evidence_denominator: int = Field(default=0, ge=0)
    invalid_source_refs: int = Field(default=0, ge=0)
    cross_domain_refs: int = Field(default=0, ge=0)
    unbound_source_refs: int = Field(default=0, ge=0)
    unread_source_refs: int = Field(default=0, ge=0)
    #: Phase 4.1：来源未进入**该 claim 自己的** producer invocation
    not_in_claim_producer_refs: int = Field(default=0, ge=0)
    #: 该 claim 缺少可用的 producer provenance
    claims_without_provenance: int = Field(default=0, ge=0)
    claims_by_type: dict[str, int] = Field(default_factory=dict)
    claims_by_producer: dict[str, int] = Field(default_factory=dict)
    gate: Gate = Gate.PASSED
    degradation_reason: Optional[str] = None
    issues: list[str] = Field(default_factory=list)
    legacy_projection_rows: int = Field(default=0, ge=0)


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_SEMANTIC_RATE_FLOOR",
    "StrictModel",
    "SourceKind",
    "DomainItemState",
    "SpanState",
    "ItemReason",
    "SpanReason",
    "LedgerOutcome",
    "NotUsedReason",
    "LedgerStage",
    "PlanStrategy",
    "Gate",
    "DomainScope",
    "DomainState",
    "DomainItem",
    "MaterialDomainSnapshot",
    "SourceSpan",
    "CoveragePlanModel",
    "CoverageLedgerEntry",
    "MaterialCoverageReport",
    "REASON_REQUIRED_STATES",
    "REASON_REQUIRED_SPAN_STATES",
    "UNACCOUNTED_ITEM_STATES",
    # Phase 2
    "SegmentStatus",
    "SegmentSourceRole",
    "LessonSegment",
    "KnowledgeUnitDraft",
    "DefinitionDraft",
    "FormulaDraft",
    "DerivationDraft",
    "ExampleDraft",
    "SegmentUnderstanding",
    "MergedKnowledgeUnit",
    "LessonUnderstanding",
    # Phase 3
    "CognitiveItemType",
    "CognitiveOrigin",
    "CognitiveTreatment",
    "CognitiveStatus",
    "CognitiveMapStatus",
    "COGNITIVE_TYPES_REQUIRING_SOURCE",
    "CognitiveItem",
    "CognitiveMap",
    # Phase 4
    "ClaimType",
    "CLAIM_TYPES_REQUIRING_SOURCE",
    "ClaimImportance",
    "ClaimEvidenceStatus",
    "ClaimSourceRelation",
    "ClaimBindingStatus",
    "ClaimBindingMethod",
    "VisibilityBasis",
    "ProducerKind",
    "ClaimSourceVisibility",
    "ClaimProvenance",
    "provenance_visible_for",
    "ClaimDraft",
    "BoundClaimSource",
    "BoundEvidence",
    "EvidenceReport",
]
