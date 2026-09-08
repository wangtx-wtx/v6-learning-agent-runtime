"""模型输出的 Pydantic Schema 绑定（V5.5 方案 11.4）。

节点模型输出必须通过 schema 校验后才能进入持久化/下游；
禁止 `extract_json(...) or {}` 式的静默空值兜底——解析/校验失败显式抛
SchemaValidationError，由 DAG 引擎按「schema 错误先修复再换模型」处理。
"""
from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError

from ..dag import SchemaValidationError
from ..reasoning import extract_json


class LessonOutlineOut(BaseModel):
    outline: list = Field(default_factory=list)


class NoteWriterOut(BaseModel):
    title: str = ""
    body: str = ""
    evidence: list = Field(default_factory=list)


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


class SelfTestOut(BaseModel):
    questions: list = Field(default_factory=list)


def parse_model_output(model_cls: type[BaseModel], content: str, node: str = "") -> dict:
    """extract_json + Pydantic 校验。失败抛 SchemaValidationError（带节点名与原因）。"""
    data = extract_json(content)
    if data is None:
        raise SchemaValidationError(
            f"{node or model_cls.__name__}: 模型输出不是合法 JSON: {content[:160]}")
    try:
        validated = model_cls.model_validate(data)
    except ValidationError as e:
        raise SchemaValidationError(
            f"{node or model_cls.__name__}: 输出不符合 schema {model_cls.__name__}: {e.errors()[:3]}") from e
    return validated.model_dump()
