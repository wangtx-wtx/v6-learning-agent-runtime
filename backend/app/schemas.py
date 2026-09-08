"""
V5.2 Pydantic 数据契约。

对 DAG 各 handler 返回值做结构化校验:
- 通过校验 → 继续
- 校验失败 → DAGNode.run 触发 fallback 模型重试
- 业务级错误 → BusinessError,直接 fail

注意:这些 schema 是软约束(handler 返回 dict 后才校验),
不会阻塞现有 stub 实现,仅在显式调用 validate_or_raise 时生效。
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from .dag import SchemaValidationError


class Evidence(BaseModel):
    """证据条目,evidence_checker 必须能定位到具体 chunk"""

    chunk_id: Optional[int] = None
    quote: str = ""
    locator: str = ""
    verified: bool = False
    reason: str = ""


class NoteResult(BaseModel):
    """note_writer / lesson.note_writer 返回值"""

    title: str = ""
    body: str = ""
    evidence: List[Evidence] = Field(default_factory=list)


class SolverResult(BaseModel):
    """homework.solver 单题解"""

    question_no: int
    final_answer: str = ""
    solution_plan: str = ""
    detailed_solution: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    model_used: str = ""


class ParallelSolverResult(BaseModel):
    """homework.parallel_solver 单题独立解"""

    question_no: int
    final_answer: str = ""
    solution_plan: str = ""
    detailed_content: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    model_used: str = ""


class ReviewedItem(BaseModel):
    """explainer 改写后的题目(含 evidence 与 teaching)"""

    question_no: int
    final_answer: str = ""
    solution_plan: str = ""
    detailed_solution: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    model_used: str = ""
    parallel_solution: str = ""
    teaching: str = ""
    evidence: List[Evidence] = Field(default_factory=list)
    evidence_status: str = "ok"
    block_reason: str = ""


class VisionResult(BaseModel):
    """vision_reader 单图识别结果"""

    image: str = ""
    text: str = ""
    confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    chunk_id: Optional[int] = None


class ReviewResult(BaseModel):
    """critic 审查结果"""

    score: float = Field(ge=0.0, le=1.0)
    issues: List[str] = Field(default_factory=list)
    suggestion: str = ""


def validate_or_raise(model_cls: type[BaseModel], payload: dict) -> BaseModel:
    """校验 dict 符合 schema。失败时抛 SchemaValidationError 让 DAG 触发 fallback"""
    if not isinstance(payload, dict):
        raise SchemaValidationError(f"{model_cls.__name__}: payload 不是 dict")
    try:
        return model_cls.model_validate(payload)
    except Exception as e:
        raise SchemaValidationError(f"{model_cls.__name__} 校验失败: {e}") from e


def safe_validate(model_cls: type[BaseModel], payload: dict) -> Optional[BaseModel]:
    """校验失败时返回 None 而非抛错——用于演示/stub 路径"""
    try:
        return model_cls.model_validate(payload)
    except Exception:
        return None