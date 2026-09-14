"""V5.6.4 统一网关模式：fake / replay / live 收口。

设计原则（来自用户审查）：
1. 不重写 V5.6.1 run_tests_isolated.py；只新增子进程入口。
2. ``resolve_mode()`` 严格读 ``config.ENV``；test 默认 fake，dev/prod 默认 live。
3. live 模式守卫：缺 key / test 显式 live 需 ``V5_ALLOW_LIVE_MODEL_TESTS=1``。
4. Token 预算必须在网络请求**前**预留，请求后按实际 usage 结算。
5. LiveEngine 是唯一允许 ``import httpx`` 的地方。
"""
from __future__ import annotations

import os
from typing import Tuple
from urllib.parse import urlparse

VALID_MODES = ("fake", "replay", "live")


class ReplayMissError(RuntimeError):
    """回放未命中：绝不允许自动回退 live。"""


def resolve_mode() -> str:
    """解析最终 mode。失败立即抛 RuntimeError。

    优先级：
      V5_GATEWAY_MODE 显式值（若合法）
      否则按 ``config.ENV`` 默认：
        test        → fake
        development → live
        production  → live
      V5_ENV 未设置（未 import config）→ live（最保守默认）
    """
    raw = (os.environ.get("V5_GATEWAY_MODE") or "").strip().lower()
    if raw and raw not in VALID_MODES:
        raise RuntimeError(
            f"V5_GATEWAY_MODE 非法值 {raw!r}，允许 {VALID_MODES}"
        )

    # 延迟解析 config.ENV（避免循环 import）
    env_name = _config_env()

    if env_name == "test":
        if raw == "live":
            if os.environ.get("V5_ALLOW_LIVE_MODEL_TESTS") != "1":
                raise RuntimeError(
                    "V5_ENV=test + V5_GATEWAY_MODE=live 必须显式设 "
                    "V5_ALLOW_LIVE_MODEL_TESTS=1"
                )
        return raw or "fake"

    # development / production
    return raw or "live"


def _config_env() -> str:
    """读取 config.ENV，若 config 未导入则按 development 兜底。"""
    try:
        from . import config as _cfg  # noqa: WPS433
        return getattr(_cfg, "ENV", "development")
    except Exception:
        return "development"


def live_required_env() -> Tuple[str, str, float]:
    """live 模式前置依赖：env 检测；缺则启动期 fail。

    返回 (url, api_key, timeout)。"""
    # config 已负责从 backend/.env 安全读取网关 Key；环境变量仍有最高优先级。
    # 复用统一配置，避免一键启动后首次模型调用才报缺少 Key。
    from . import config as _cfg
    url = (
        os.environ.get("V5_GATEWAY_URL")
        or getattr(_cfg, "GATEWAY_BASE_URL", "")
        or ""
    ).strip()
    key = (
        os.environ.get("V5_GATEWAY_API_KEY")
        or getattr(_cfg, "GATEWAY_API_KEY", "")
        or ""
    ).strip()
    miss = ["V5_GATEWAY_URL"] if not url else []
    host = (urlparse(url).hostname or "").lower() if url else ""
    # The portable gateway intentionally permits keyless access while bound to
    # loopback.  Keep requiring a key for every non-local gateway URL.
    if not key and host not in {"127.0.0.1", "localhost", "::1"}:
        miss.append("V5_GATEWAY_API_KEY")
    if miss:
        raise RuntimeError(
            f"live 模式缺少环境变量: {miss}"
        )
    try:
        timeout = float(os.environ.get("V5_GATEWAY_TIMEOUT", "180") or "180")
    except (TypeError, ValueError):
        timeout = 180.0
    return url, key, timeout


# ---------------------------------------------------------------------------
# Live 计数器（仅 live 模式实际写入）
# ---------------------------------------------------------------------------
_state = {
    "calls": 0,        # 业务调用次数（chat/embedding/usage 各算一次）
    "tokens_in": 0,    # 实际结算的 input Token 累计
    "tokens_out": 0,   # 实际结算的 output Token 累计
    "reserved_in": 0,  # reserve 阶段累计预留的 input Token（预检用）
    "reserved_out": 0, # reserve 阶段累计预留的 output Token（预检用）
    "attempts": 0,     # HTTP 尝试次数（含 429/5xx 重试）
    "bytes_in": 0,     # 请求字节累计（预检用）
}


def _int_env(name: str, default: int = 0) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def reserve_budget(estimated_in: int, max_out: int, label: str = "") -> Tuple[int, int]:
    """请求前预留：检查调用次数 / 字节 / Token；fake/replay 直接返回 0。

    返回 ``(reserved_in, reserved_out)`` 元组；请求完成后调用 ``settle()``
    按实际 usage 覆盖预留值（不叠加，因为预留时已 +1）。
    """
    if resolve_mode() != "live":
        return (0, 0)
    max_calls = _int_env("V5_LIVE_MAX_CALLS", 0)
    max_bytes = _int_env("V5_LIVE_MAX_INPUT_CHARS", 0)
    max_tokens = _int_env("V5_LIVE_TOKEN_BUDGET_TOTAL", 0)

    if max_calls and _state["calls"] + 1 > max_calls:
        raise RuntimeError(
            f"V5_LIVE_MAX_CALLS 超限: {_state['calls'] + 1}>{max_calls} ({label})"
        )
    if max_bytes and estimated_in > max_bytes:
        raise RuntimeError(
            f"V5_LIVE_MAX_INPUT_CHARS 超限: {estimated_in}>{max_bytes} ({label})"
        )
    # 累计已预留 + 当前预估（reserved_in/reserved_out 在 reserve 阶段记入）
    reserved_so_far = _state["reserved_in"] + _state["reserved_out"]
    if max_tokens and reserved_so_far + max_out > max_tokens:
        raise RuntimeError(
            f"V5_LIVE_TOKEN_BUDGET_TOTAL 预检超限: "
            f"已预留 {reserved_so_far} + 本次 {max_out} > 上限 {max_tokens} ({label})"
        )
    _state["calls"] += 1
    _state["bytes_in"] += estimated_in
    _state["reserved_in"] += max(0, int(estimated_in or 0))
    _state["reserved_out"] += max(0, int(max_out or 0))
    return (estimated_in, max_out)


def settle(actual_in: int, actual_out: int) -> None:
    """请求后结算：用实际值覆盖预留值。

    reserve_budget 已递增 calls；这里仅累加 tokens。"""
    if resolve_mode() != "live":
        return
    _state["tokens_in"] += max(0, int(actual_in or 0))
    _state["tokens_out"] += max(0, int(actual_out or 0))


def note_attempt() -> None:
    """Live 每次 HTTP 尝试（含 429/5xx 重试）记一次。"""
    if resolve_mode() == "live":
        _state["attempts"] += 1


def reset_counters() -> None:
    """测试用：清零计数（仅 live 模式生效）。"""
    for k in _state:
        _state[k] = 0


def snapshot() -> dict:
    """诊断端点用的快照。无敏感字段。"""
    return {
        "mode": resolve_mode(),
        "network_allowed": resolve_mode() == "live",
        "replay_dir_configured": bool(
            (os.environ.get("V5_GATEWAY_REPLAY_DIR") or "").strip()
        ),
        "live_call_count": _state["calls"],
        "live_attempts": _state["attempts"],
        "live_tokens_consumed": _state["tokens_in"] + _state["tokens_out"],
    }
