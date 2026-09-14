"""V6 Learning Engine（Phase 1 — Material Integrity）。

本包只实现 Phase 1 范围::

    Material Domain → 材料规范化 → 唯一材料识别 → Source Map
    → Coverage Plan → Coverage Ledger → Coverage Auditor

不在本阶段实现（属于 Phase 2–6，禁止以占位代码冒充完成）::

    understand_segments / merge_lesson_understanding / student_simulator /
    Knowledge Composer V2 / Evidence V2 / Learning Loop

设计文档: docs/V6_LEARNING_ENGINE_IMPLEMENTATION_PLAN.md
"""
from __future__ import annotations

from .config import (  # noqa: F401
    ENGINE_MODES,
    engine_enabled,
    engine_mode,
    engine_version,
    publication_mode,
    real_notes_publish_enabled,
    shadow_mode,
)

__all__ = [
    "ENGINE_MODES",
    "engine_enabled",
    "engine_mode",
    "engine_version",
    "publication_mode",
    "real_notes_publish_enabled",
    "shadow_mode",
]
