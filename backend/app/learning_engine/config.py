"""V6 Learning Engine 配置开关。

Feature flag 语义（设计文档 §14）::

    V6_LEARNING_ENGINE=off     保持原 V5 行为（默认，Phase 1 推荐）
    V6_LEARNING_ENGINE=shadow  建立 Material Domain / Source Map / Coverage，
                               写入 V6 新表，但**不覆盖**现有 notes / document_artifacts
    V6_LEARNING_ENGINE=on      V6 全量主链与 Composer V2 正式出版

库级默认保留 ``off`` 以便旧部署安全升级；项目启动脚本在完整迁移与测试通过后
显式设置 ``on``。``shadow`` 仍可用于只审计、不接管正式笔记的对照运行。
"""
from __future__ import annotations

import os

ENGINE_MODES = ("off", "shadow", "on")
DEFAULT_MODE = "off"


def _read_mode() -> str:
    raw = (os.environ.get("V6_LEARNING_ENGINE") or "").strip().lower()
    if not raw:
        return DEFAULT_MODE
    if raw not in ENGINE_MODES:
        raise RuntimeError(
            f"无效 V6_LEARNING_ENGINE='{raw}'，允许: {'|'.join(ENGINE_MODES)}"
        )
    return raw


def engine_mode() -> str:
    """运行时解析引擎模式（每次读取环境变量，便于测试切换）。"""
    return _read_mode()


def engine_enabled() -> bool:
    """是否建立 V6 材料域与覆盖账本（shadow / on）。"""
    return engine_mode() != "off"


def shadow_mode() -> bool:
    """是否处于 shadow（不覆盖正式笔记与 document_artifacts）。"""
    return engine_mode() == "shadow"


def engine_version() -> str:
    """写入 material_domains / coverage_reports，用于区分 V5 / V6 运行。"""
    mode = engine_mode()
    if mode == "off":
        return "v5"
    return f"v6.0.0-{mode}"


def real_notes_publish_enabled() -> bool:
    """V6 是否接管正式笔记出版。

    Phase 5 Composer V2 已落地；只有显式 ``on`` 才接管正式笔记。
    """
    return engine_mode() == "on"


def publication_mode() -> str:
    """当前出版层语义（供 API / 前端 / 报告如实展示，不含糊）。"""
    if not engine_enabled():
        return "v5_legacy"
    if shadow_mode():
        return "v6_understanding_only_legacy_publication"
    return "v6_composer_v2"
