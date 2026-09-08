"""
模型注册表：按照 v5.1 方案定义所有模型。
"""
from dataclasses import dataclass, field
from typing import Optional

@dataclass(frozen=True)
class ModelSpec:
    id: str
    gateway_model: str
    provider: str
    family: str
    capability: str = "text"
    is_default_solver: bool = False
    is_default_reviewer: bool = False
    is_default_vision: bool = False
    is_default_light: bool = False


MODELS = {
    # DeepSeek 微信免费
    "deepseek_v4_free": ModelSpec(
        id="deepseek_v4_free",
        gateway_model="Deepseek-v4-flash",
        provider="wechat",
        family="deepseek",
        capability="text",
        is_default_solver=True,
    ),
    # DeepSeek 官方付费
    "deepseek_v4_official": ModelSpec(
        id="deepseek_v4_official",
        gateway_model="deepseekv4-flash",
        provider="deepseek",
        family="deepseek",
        capability="text",
    ),
    # Qwen 独立审查 / 教学化改写
    "qwen3_8_27b": ModelSpec(
        id="qwen3_8_27b",
        gateway_model="ecnu-plus",
        provider="qwen",
        family="qwen",
        capability="text",
        is_default_reviewer=True,
    ),
    # Qwen 轻量结构化
    "qwen3_flash": ModelSpec(
        id="qwen3_flash",
        gateway_model="qwen3.8-flash",
        provider="qwen",
        family="qwen",
        capability="text",
        is_default_light=True,
    ),
    # Qwen 视觉
    "qwen3_vl": ModelSpec(
        id="qwen3_vl",
        gateway_model="qwen3-vl-flash",
        provider="qwen",
        family="qwen",
        capability="vision",
        is_default_vision=True,
    ),
    # MiniMax M3 —— 高风险并行 / 备选审查
    "minimax_m3": ModelSpec(
        id="minimax_m3",
        gateway_model="minimax-m3",
        provider="minimax",
        family="minimax",
        capability="text",
    ),
    # GLM —— 不参与视觉，仅记录保留
    "glm_flash": ModelSpec(
        id="glm_flash",
        gateway_model="glm-5.3-flash",
        provider="zhipu",
        family="glm",
        capability="text",
    ),
    # 固定 embedding 与 rerank
    "embedding": ModelSpec(
        id="embedding",
        gateway_model="ecnu-embedding-small",
        provider="local",
        family="embedding",
        capability="embedding",
    ),
    "rerank": ModelSpec(
        id="rerank",
        gateway_model="ecnu-rerank",
        provider="local",
        family="rerank",
        capability="rerank",
    ),
}


def get_model(model_id: str) -> ModelSpec:
    if model_id not in MODELS:
        raise KeyError(f"未知模型 id: {model_id}")
    return MODELS[model_id]


def list_models() -> list[dict]:
    return [
        {
            "id": m.id,
            "gateway_model": m.gateway_model,
            "provider": m.provider,
            "family": m.family,
            "capability": m.capability,
            "is_default_solver": m.is_default_solver,
            "is_default_reviewer": m.is_default_reviewer,
            "is_default_vision": m.is_default_vision,
            "is_default_light": m.is_default_light,
        }
        for m in MODELS.values()
    ]


# 角色 → 候选模型（有序列表，优先使用第一个）
ROLE_MODELS: dict[str, list[str]] = {
    "vision_reader": ["qwen3_vl", "minimax_m3"],
    "transcriber_splitter": ["qwen3_flash", "deepseek_v4_free"],
    "lesson_structurer": ["qwen3_flash", "deepseek_v4_free"],
    "student_simulator": ["deepseek_v4_free", "deepseek_v4_official", "qwen3_8_27b"],
    "note_writer": ["deepseek_v4_free", "deepseek_v4_official", "qwen3_8_27b"],
    "critic": ["qwen3_8_27b", "minimax_m3"],
    "evidence_auditor": ["qwen3_8_27b", "qwen3_flash"],
    "scope_auditor": ["qwen3_8_27b", "minimax_m3"],
    "solver": ["deepseek_v4_free", "deepseek_v4_official"],
    "parallel_solver": ["minimax_m3"],
    "solution_explainer": ["qwen3_8_27b", "deepseek_v4_free"],
    "error_analyst": ["deepseek_v4_free", "deepseek_v4_official", "qwen3_8_27b"],
    "review_writer": ["deepseek_v4_free", "deepseek_v4_official"],
    "self_test_writer": ["qwen3_flash", "deepseek_v4_free"],
}
