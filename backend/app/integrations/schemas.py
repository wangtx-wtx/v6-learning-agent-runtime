"""模型输出的 Pydantic Schema 绑定（V5.5 方案 11.4）。

节点模型输出必须通过 schema 校验后才能进入持久化/下游；
禁止 `extract_json(...) or {}` 式的静默空值兜底——解析/校验失败显式抛
SchemaValidationError，由 DAG 引擎按「schema 错误先修复再换模型」处理。
"""
from __future__ import annotations

import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..dag import SchemaValidationError
from ..reasoning import extract_json


class LessonOutlineOut(BaseModel):
    outline: list = Field(default_factory=list)


AllowedBlockType = Literal[
    "paragraph", "definition", "theorem", "formula", "derivation", "example",
    "procedure", "comparison", "table", "figure", "quote", "key_point",
    "notice", "pitfall", "exception", "memory_tip", "summary", "source_note",
    # V6 Phase 4（Evidence V2）：模型教学补充必须显式成块，渲染时带稳定标识
    # `data-claim-type="ai_explanation"`，不得与课堂事实块混同。
    "ai_explanation",
]


class SourceRef(BaseModel):
    chunk_id: int | None = None
    source_id: str = ""
    quote: str = ""
    locator: str = ""
    source_type: str = "course_source"


class ContentBlock(BaseModel):
    type: AllowedBlockType
    title: str = ""
    content: str = ""
    items: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    #: V6 Phase 4（Evidence V2）：可选的 claim 类型标注。渲染时会写成稳定的
    #: ``data-claim-type`` 属性；``ai_explanation`` 必须可被机器识别。
    claim_type: str = ""
    #: V6 Composer V2 audit fields. They survive normalization and are embedded in
    #: both HTML and PDF's common structured payload.
    claim_key: str = ""
    selection_reason: str = ""


class DocumentSection(BaseModel):
    title: str = ""
    blocks: list[ContentBlock] = Field(default_factory=list)


class StructuredDocument(BaseModel):
    title: str = ""
    subtitle: str = ""
    deck: str = ""
    sections: list[DocumentSection] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)


class NoteWriterOut(BaseModel):
    title: str = ""
    body: str = ""
    evidence: list = Field(default_factory=list)
    document: StructuredDocument | None = None


class CriticOut(BaseModel):
    score: float = 0.0
    passed: bool = False
    issues: list = Field(default_factory=list)


class SolverOut(BaseModel):
    final_answer: str = ""
    solution_plan: str = ""
    detailed_solution: str = ""


class ParallelSolverOut(BaseModel):
    final_answer: str = ""
    solution_plan: str = ""
    detailed_content: str = ""


class AdjudicatorOut(BaseModel):
    decision: str
    reason: str = ""


class TeachingOut(BaseModel):
    teaching: str = ""


class OcrOut(BaseModel):
    text: str = ""


class VisionOut(BaseModel):
    question_text: str = ""
    student_answer: str = ""
    mark_grades: str = ""


class AnalystOut(BaseModel):
    phenomenon: str = ""
    direct_cause: str = ""
    root_cause: str = ""
    knowledge_gaps: list = Field(default_factory=list)
    possible_causes: list = Field(default_factory=list)


class CrossCheckOut(BaseModel):
    confirmed_causes: list = Field(default_factory=list)
    uncertain: str = ""


class ReviewWriterOut(BaseModel):
    outline: list = Field(default_factory=list)
    materials: str = ""
    document: StructuredDocument | None = None


class SelfTestOut(BaseModel):
    questions: list = Field(default_factory=list)


# ---------------------------------------------------------------------------
# V6 Phase 2：Full Lesson Understanding（分段理解 / 全局合并）
#
# 模型**只输出结构化理解**，不输出 exact quote：原文由程序按 Source ID 从
# Source Map 取值（Phase 4 Evidence V2）。所有模型输出 extra="forbid"。
# ---------------------------------------------------------------------------
class V6KnowledgeUnitOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temp_id: str = Field(min_length=1)
    topic: str = ""
    kind: str = "concept"
    summary: str = ""
    source_refs: list[str] = Field(default_factory=list)
    teacher_emphasis: float = Field(default=0.0, ge=0.0, le=1.0)
    relations: list[str] = Field(default_factory=list)


class V6DefinitionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str = ""
    meaning: str = ""
    source_refs: list[str] = Field(default_factory=list)


class V6FormulaOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expression: str = ""
    meaning: str = ""
    source_refs: list[str] = Field(default_factory=list)


class V6DerivationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = ""
    steps: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)


class V6ExampleOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = ""
    solution: str = ""
    source_refs: list[str] = Field(default_factory=list)


class SegmentUnderstandingOut(BaseModel):
    """节点 07 单段理解输出契约。"""

    model_config = ConfigDict(extra="forbid")

    topics: list[str] = Field(default_factory=list)
    knowledge_units: list[V6KnowledgeUnitOut] = Field(default_factory=list)
    definitions: list[V6DefinitionOut] = Field(default_factory=list)
    formulas: list[V6FormulaOut] = Field(default_factory=list)
    derivations: list[V6DerivationOut] = Field(default_factory=list)
    examples: list[V6ExampleOut] = Field(default_factory=list)
    teacher_emphasis: float = Field(default=0.0, ge=0.0, le=1.0)
    unresolved_points: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)


class MergeUnderstandingOut(BaseModel):
    """节点 08 分层合并输出契约（模型只做归纳，不得新增课堂事实）。

    ``unresolved_conflicts`` 必须显式列出冲突，禁止静默覆盖。
    """

    model_config = ConfigDict(extra="forbid")

    topics: list[str] = Field(default_factory=list)
    knowledge_units: list[V6KnowledgeUnitOut] = Field(default_factory=list)
    unresolved_conflicts: list[str] = Field(default_factory=list)
    teacher_emphasis: float = Field(default=0.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# V6 Phase 3：Cognitive Layer（节点 09 student_simulator）
#
# 模型只提出「学生可能如何理解/混淆/记忆已有课堂内容」的候选认知项；
# 不得新增课堂事实（source_refs 只能是输入中存在的 Source ID）。
# 空结果必须返回空数组 —— 不得为了模板完整而编造 pitfall。
# ---------------------------------------------------------------------------
class CognitiveItemOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(description="八类之一：confusion_point / prerequisite_gap / pitfall / "
                                  "emphasis / concept_relation / memory_anchor / missing_step / difficulty")
    title: str = ""
    explanation: str = ""
    severity: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    recommended_treatment: str = "explanation"
    origin: str = "model_cognitive_inference"
    knowledge_unit_refs: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)


class StudentSimulatorOut(BaseModel):
    """节点 09 输出契约。``items`` 允许为空数组（没有认知风险就是空）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[CognitiveItemOut] = Field(default_factory=list)
    #: 模型对本批材料的整体说明（可选，不进入最终文档）
    analysis_note: str = ""


def _coerce_chunk_id(value):
    """模型可能把 chunk_id 写成 ``'788'`` / ``'chunk 788'`` / ``788.0``。

    统一收敛为 int 或 None；不能解析时不猜测，交给证据校验判定失败。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if float(value).is_integer() else None
    m = re.search(r"\d+", str(value))
    return int(m.group(0)) if m else None


def _as_source_ref(item) -> dict:
    """模型常把来源写成字符串/纯数字，这里收敛成 SourceRef 字典。"""
    if isinstance(item, dict):
        out = dict(item)
        if "chunkId" in out:
            out.setdefault("chunk_id", out.pop("chunkId"))
        if "chunk_id" in out:
            out["chunk_id"] = _coerce_chunk_id(out.get("chunk_id"))
        return out
    if isinstance(item, bool):
        return {"quote": str(item)}
    if isinstance(item, int):
        return {"chunk_id": item}
    text = str(item if item is not None else "").strip()
    m = re.fullmatch(r"(?:chunk[_ ]?)?(\d+)", text)
    if m:
        return {"chunk_id": int(m.group(1))}
    return {"quote": text}


def _normalize_block(block) -> dict:
    if not isinstance(block, dict):
        return {"type": "paragraph", "content": str(block if block is not None else "")}
    out = dict(block)
    for field in ("source_refs", "sources"):
        value = out.get(field)
        if isinstance(value, list):
            out[field] = [_as_source_ref(x) for x in value]
    for field in ("items", "rows"):
        value = out.get(field)
        if isinstance(value, list):
            out[field] = [
                [str(c) for c in row] if isinstance(row, list) else str(row)
                for row in value
            ]
    return out


def _normalize_document(doc):
    if not isinstance(doc, dict):
        return doc
    out = dict(doc)
    if isinstance(out.get("sources"), list):
        out["sources"] = [_as_source_ref(x) for x in out["sources"]]
    sections = out.get("sections")
    if isinstance(sections, list):
        normalized = []
        for section in sections:
            if isinstance(section, dict):
                item = dict(section)
                if isinstance(item.get("blocks"), list):
                    item["blocks"] = [_normalize_block(b) for b in item["blocks"]]
                normalized.append(item)
            else:
                normalized.append({"title": str(section), "blocks": []})
        out["sections"] = normalized
    return out


def _wants_source_ref_objects(model_cls: type[BaseModel]) -> bool:
    """该 schema 是否属于「V5 证据契约」，需要把宽松来源收敛成 ``SourceRef`` 对象？

    V5 契约（note_writer / review_writer）里 ``source_refs`` / ``sources`` / ``evidence``
    是 ``list[SourceRef]``，需要把 ``"chunk 788"`` 这类宽松输入收敛成对象。
    V6 Phase 2 契约（segment/merge understanding）里 ``source_refs`` 是
    ``list[str]``（**只放 Source ID 字符串**），若也套用 ``_as_source_ref`` 会把
    ``"T000012"`` 变成 ``{"quote": "T000012"}``，直接 schema 校验失败。

    判定依据：只有 V5 证据契约才声明 ``document`` / ``evidence`` 字段。
    按字段存在性分流，而不是无条件规整（``evidence`` 在 pydantic 里声明为裸
    ``list``，无法从注解拿到 ``SourceRef``，因此不能靠 __args__ 判断）。
    """
    fields = getattr(model_cls, "model_fields", {}) or {}
    return "document" in fields or "evidence" in fields


def _normalize_model_data(data, model_cls: type[BaseModel]):
    """把模型输出的常见形态偏差收敛到 schema 可接受结构。

    只做「形状」纠正（字符串→对象、字符串块→段落块），不补造内容；
    真正的缺字段/类型错误仍然交给 Pydantic 抛 SchemaValidationError。
    """
    if not isinstance(data, dict):
        return data
    out = dict(data)
    wants_objects = _wants_source_ref_objects(model_cls)
    for field in ("source_refs", "sources"):
        value = out.get(field)
        if isinstance(value, list) and wants_objects:
            out[field] = [_as_source_ref(x) for x in value]
    if isinstance(out.get("document"), dict):
        out["document"] = _normalize_document(out["document"])
    if isinstance(out.get("blocks"), list):
        out["blocks"] = [_normalize_block(b) for b in out["blocks"]]
    # evidence 也要规整：下游 verify_evidence_list 直接用 chunk_id 查库，
    # 字符串型 chunk_id 会导致真实存在的引用被判为不存在。
    if isinstance(out.get("evidence"), list) and wants_objects:
        out["evidence"] = [_as_source_ref(e) for e in out["evidence"]]
    # V6 Phase 2：knowledge_units[].source_refs 等嵌套 str 列表保持原样。
    for key in ("knowledge_units", "definitions", "formulas", "derivations", "examples"):
        items = out.get(key)
        if isinstance(items, list) and not wants_objects:
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("source_refs"), list):
                    item["source_refs"] = [str(x) for x in item["source_refs"]]
    return out


def parse_model_output(model_cls: type[BaseModel], content: str, node: str = "") -> dict:
    """extract_json + Pydantic 校验。失败抛 SchemaValidationError（带节点名与原因）。"""
    data = extract_json(content)
    if data is None:
        raise SchemaValidationError(
            f"{node or model_cls.__name__}: 模型输出不是合法 JSON: {content[:160]}")
    data = _normalize_model_data(data, model_cls)
    try:
        validated = model_cls.model_validate(data)
    except ValidationError as e:
        raise SchemaValidationError(
            f"{node or model_cls.__name__}: 输出不符合 schema {model_cls.__name__}: {e.errors()[:3]}") from e
    return validated.model_dump()
