"""
额度路由：微信免费 DeepSeek 优先，官方 DeepSeek 兜底。
按 5h 用量做阈值路由。
"""
import json
import time
from typing import Optional

from .database import query

# 与 v5.1 方案一致
QUOTA_LIMITS = {"5h": 1200, "7d": 9000, "30d": 18000}
NORMAL_THRESHOLD = 70   # 5h 用量 < 70% 正常
CONSERVATIVE_THRESHOLD = 90  # 5h 用量 >= 90% 严格保守


def _compute_usage_5h(usage: dict) -> float:
    """根据 gateway /admin/api/usage 返回计算 5h 用量百分比"""
    if not usage or not usage.get("available", False):
        return 0.0
    data = usage.get("data") or usage
    # 尝试常见字段
    used = data.get("used_5h") or data.get("five_hour_usage") or 0
    limit = data.get("limit_5h") or data.get("five_hour_limit") or QUOTA_LIMITS["5h"]
    try:
        used = float(used)
        limit = float(limit)
    except (TypeError, ValueError):
        return 0.0
    if limit <= 0:
        return 0.0
    return min(100.0, used / limit * 100)


def route_for_role(agent_role: str, usage_5h_pct: float, risk: str = "low") -> str:
    """
    根据 5h 用量百分比返回该角色的首选模型 id。
    保守模式下 DeepSeek 任务切官方，轻量任务优先 qwen3_flash。
    """
    # 非 DeepSeek 角色的路由原则
    if agent_role in {"critic", "solution_explainer", "evidence_auditor", "scope_auditor"}:
        return "qwen3_8_27b"
    if agent_role in {"vision_reader"}:
        return "qwen3_vl"
    if agent_role in {"transcriber_splitter", "lesson_structurer", "self_test_writer"}:
        return "qwen3_flash"

    # DeepSeek 角色
    if usage_5h_pct < NORMAL_THRESHOLD:
        return "deepseek_v4_free"
    elif usage_5h_pct < CONSERVATIVE_THRESHOLD:
        # 70%-90%：正式写作/解题转官方
        if agent_role in {"student_simulator", "note_writer", "solver", "error_analyst", "review_writer"}:
            return "deepseek_v4_official"
        return "deepseek_v4_free"
    else:
        # >= 90%：全部 DeepSeek 转官方
        return "deepseek_v4_official"


async def load_usage_from_gateway(gateway_client) -> dict:
    """读取网关用量接口。失败时返回空 dict"""
    try:
        return await gateway_client.get_usage()
    except Exception as e:
        return {"available": False, "error": str(e), "data": {}}


def make_route_for_workflow(workflow_type: str, quota_pct: float) -> dict:
    """生成某个 workflow 的角色→模型 完整路由表"""
    routes = {}
    # 听课流
    if workflow_type in ("lesson", "attendance", "listen"):
        for role in ["transcriber_splitter", "lesson_structurer", "student_simulator", "note_writer"]:
            routes[role] = _resolve(role, quota_pct)
        routes["critic"] = "qwen3_8_27b"
        routes["evidence_auditor"] = "qwen3_8_27b"
        routes["scope_auditor"] = "qwen3_8_27b"
        routes["vision_reader"] = "qwen3_vl"

    # 作业流
    elif workflow_type in ("homework", "solve"):
        routes["vision_reader"] = "qwen3_vl"
        routes["transcriber_splitter"] = "qwen3_flash"
        routes["solver"] = _resolve("solver", quota_pct)
        routes["parallel_solver"] = "minimax_m3"
        routes["solution_explainer"] = "qwen3_8_27b"
        routes["evidence_auditor"] = "qwen3_8_27b"
        routes["scope_auditor"] = "qwen3_8_27b"

    # 错题流
    elif workflow_type in ("error", "wrong", "miss"):
        routes["vision_reader"] = "qwen3_vl"
        routes["error_analyst"] = _resolve("error_analyst", quota_pct)
        routes["critic"] = "qwen3_8_27b"

    # 复习流
    elif workflow_type in ("review", "exam", "revision"):
        routes["review_writer"] = _resolve("review_writer", quota_pct)
        routes["self_test_writer"] = "qwen3_flash"
        routes["critic"] = "qwen3_8_27b"
        routes["evidence_auditor"] = "qwen3_8_27b"
        routes["scope_auditor"] = "qwen3_8_27b"

    return routes


def _resolve(role: str, quota_pct: float) -> str:
    """仅用于 DeepSeek 系列角色"""
    if quota_pct < NORMAL_THRESHOLD:
        return "deepseek_v4_free"
    elif quota_pct < CONSERVATIVE_THRESHOLD:
        return "deepseek_v4_official"
    return "deepseek_v4_official"
