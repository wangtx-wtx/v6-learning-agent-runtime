"""V5.6.4 模型网关客户端（统一 fake / replay / live 入口）。

V5.6.4 改动要点：
- 保留 ``GatewayClient`` 公共签名，内部委托 ``gateway_engines`` 的对应 Engine。
- ``GatewayClient.chat()`` 新增必填参数 ``contract``；DAG 节点必须显式传入。
- 移除模块级 httpx.AsyncClient 池（懒加载语义改到 ``LiveEngine`` 内）。
- Fake / Replay Engine 永不触碰网络；``_audit_model_call`` 增加
  ``gateway_mode / replay_key / replay_hit`` 三列写入。
- 兼容：模块级 ``aclose()`` 与 ``RetryableTimeout`` 别名保留。
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import GATEWAY_API_KEY, GATEWAY_BASE_URL, GATEWAY_TIMEOUT
from .dag import AuthError, BadRequestError, RetryableModelError
from .gateway_engines import CONTRACT_TABLE, FakeEngine, LiveEngine, ReplayEngine
from .gateway_mode import (
    ReplayMissError,
    live_required_env,
    resolve_mode,
)
from .models_registry import get_model

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Engine 单例（懒加载）
# ---------------------------------------------------------------------------
_ENGINE_LOCK = threading.Lock()
_ENGINE = None  # (mode_str, engine_instance)


def _build_engine() -> tuple[str, Any]:
    mode = resolve_mode()
    if mode == "fake":
        return mode, FakeEngine()
    if mode == "replay":
        replay_dir = (os.environ.get("V5_GATEWAY_REPLAY_DIR") or "").strip()
        if not replay_dir:
            raise RuntimeError(
                "V5_GATEWAY_MODE=replay 必须设 V5_GATEWAY_REPLAY_DIR"
            )
        return mode, ReplayEngine(Path(replay_dir))
    # live
    url, key, timeout = live_required_env()
    return mode, LiveEngine(url, key, timeout)


def _get_engine() -> tuple[str, Any]:
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                _ENGINE = _build_engine()
    return _ENGINE


def reset_engine_for_test() -> None:
    """测试用：清空 engine 缓存。允许下次访问时重新解析 mode。"""
    global _ENGINE
    with _ENGINE_LOCK:
        _ENGINE = None


def _map_status_error(status_code: int) -> None:
    """HTTP 状态码 → DAG 异常（V5.6.1 兼容层；测试 test_core 引用）。"""
    if status_code in (401, 403):
        raise AuthError(f"网关鉴权失败: HTTP {status_code}")
    if status_code == 400:
        raise BadRequestError(f"网关 400 非法: HTTP {status_code}")
    if status_code >= 500 or status_code == 429:
        raise RetryableModelError(f"网关不可恢复: HTTP {status_code}")
    raise RetryableModelError(f"未知状态码: {status_code}")


# ---------------------------------------------------------------------------
# 公共客户端（保留所有现有调用点兼容）
# ---------------------------------------------------------------------------
class GatewayClient:
    """兼容层：保留旧签名，内部委托给当前 Engine。"""

    def __init__(self, base_url: str = GATEWAY_BASE_URL,
                 timeout: float = GATEWAY_TIMEOUT,
                 api_key: str = GATEWAY_API_KEY):
        # 这些字段保留只为兼容旧代码引用（如 gateway.api_key / gateway.base_url）；
        # 实际请求由 Engine 内的 LiveEngine 使用。
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self._closed = False

    @property
    def mode(self) -> str:
        return _get_engine()[0]

    async def chat(self, model_id: str, messages: list[dict], contract: str,
                   temperature: float = 0.7, max_tokens: Optional[int] = None,
                   response_format: Optional[dict] = None) -> dict:
        if not contract:
            raise RuntimeError(
                "gateway.chat 必须显式传 contract='lesson/note_writer' 等"
            )
        if contract not in CONTRACT_TABLE:
            raise RuntimeError(
                f"未知 contract={contract!r}；合法：{sorted(CONTRACT_TABLE)}"
            )
        mode, engine = _get_engine()
        started = time.time()
        messages_text = json.dumps(messages, ensure_ascii=False, default=str)
        trace = ""
        error_code = ""
        try:
            result = await engine.chat(
                contract, model_id, messages, temperature, max_tokens,
                response_format,
            )
            trace = result.get("trace_id", "")
            self._audit(
                model_id, contract, mode, messages_text, started, "ok", "",
                result.get("tokens_in", 0), result.get("tokens_out", 0),
                trace, result.get("replay_key", ""),
                bool(result.get("replay_hit", False)),
            )
            return result
        except ReplayMissError as e:
            error_code = "replay_miss"
            self._audit(
                model_id, contract, mode, messages_text, started, "error",
                error_code, 0, 0, "", "", False,
            )
            raise
        except RetryableModelError as e:
            error_code = type(e).__name__
            self._audit(
                model_id, contract, mode, messages_text, started, "error",
                error_code, 0, 0, "", "", False,
            )
            raise
        except AuthError as e:
            error_code = "auth_error"
            self._audit(
                model_id, contract, mode, messages_text, started, "error",
                error_code, 0, 0, "", "", False,
            )
            raise
        except BadRequestError as e:
            error_code = "bad_request"
            self._audit(
                model_id, contract, mode, messages_text, started, "error",
                error_code, 0, 0, "", "", False,
            )
            raise
        except Exception as e:
            error_code = type(e).__name__
            self._audit(
                model_id, contract, mode, messages_text, started, "error",
                error_code, 0, 0, "", "", False,
            )
            raise

    async def get_usage(self) -> dict:
        _, engine = _get_engine()
        return await engine.get_usage()

    async def embedding_single(self, text: str) -> list[float]:
        _, engine = _get_engine()
        return await engine.embedding_single(text)

    async def rerank(self, query: str, documents: list[str], top_n: int | None = None) -> list[dict]:
        _, engine = _get_engine()
        return await engine.rerank(query, documents, top_n)

    async def ocr_document(self, file_value: str) -> dict:
        _, engine = _get_engine()
        return await engine.ocr_document(file_value)

    async def post_json(self, path: str, payload: dict, retries: int = 2):
        """兼容旧调用：live 模式才允许；fake/replay 拒绝。

        V5.6.4 收口：业务代码不应绕过 Engine 直接调 httpx；保留是为了兼容
        旧 monkey-patch 测试；live 之外模式直接抛错。
        """
        mode, engine = _get_engine()
        if mode != "live":
            raise RuntimeError(
                f"{mode} 模式不允许 gateway.post_json() 直接调用；请用 chat()/embedding_single()"
            )
        # live 模式：委托给 LiveEngine 私有通路
        return await engine._post_with_retry(path, payload)

    async def aclose(self) -> None:
        """关闭 LiveEngine 持有的 httpx 连接池（如果有）。"""
        try:
            _, engine = _get_engine()
            await engine.aclose()
        except Exception as e:
            logger.warning("网关连接关闭异常: %s", e)
        self._closed = True

    # ------------------------------------------------------------------ audit
    def _audit(self, model_id, contract, mode, messages_text, started,
               status, error_code, tokens_in, tokens_out, trace,
               replay_key, replay_hit) -> None:
        try:
            from .database import insert
            info = _call_context.get() or {}
            insert(
                "INSERT INTO model_calls (run_id, node_id, attempt, model_id, "
                " gateway_model, trace_id, prompt_name, prompt_version, "
                " tokens_in, tokens_out, latency_ms, status, error_code, "
                " input_digest, input_chars, created_at, "
                " gateway_mode, replay_key, replay_hit) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (info.get("run_id"), info.get("node_id"),
                 info.get("attempt", 1),
                 model_id, model_id, trace,
                 info.get("prompt_name"), info.get("prompt_version"),
                 tokens_in, tokens_out,
                 int((time.time() - started) * 1000),
                 status, (error_code or "")[:64],
                 hashlib.sha256((messages_text or "").encode()).hexdigest()[:16],
                 len(messages_text or ""),
                 datetime.now().isoformat(timespec="seconds"),
                 mode, replay_key or "", 1 if replay_hit else 0),
            )
        except Exception as e:
            logger.warning(f"model_calls 审计写入失败: {e}")


gateway = GatewayClient()


# ---------------------------------------------------------------------------
# 模型调用审计上下文（V5.6.4 扩展 contract / schema_name / schema_version）
# ---------------------------------------------------------------------------
_call_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "model_call_context", default=None)


def set_call_context(**fields) -> contextvars.Token:
    """合并式设置当前调用的审计上下文（在协程任务内使用）。

    V5.6.4 新增字段：``contract / schema_name / schema_version``。
    """
    cur = dict(_call_context.get() or {})
    cur.update(fields)
    return _call_context.set(cur)


def reset_call_context(token: contextvars.Token) -> None:
    _call_context.reset(token)


def _audit_model_call(model_id: str, gateway_model: str, messages_text: str,
                      started: float, status: str, error_code: str,
                      tokens_in: int, tokens_out: int, trace: str) -> None:
    """向后兼容的旧入口；新代码应走 ``GatewayClient._audit``。

    内部将 ``gateway_mode`` 设为当前 resolve_mode，便于历史代码调用统计。
    """
    try:
        mode = resolve_mode()
    except Exception:
        mode = "live"
    gateway._audit(
        model_id, contract="", mode=mode, messages_text=messages_text,
        started=started, status=status, error_code=error_code,
        tokens_in=tokens_in, tokens_out=tokens_out, trace=trace,
        replay_key="", replay_hit=False,
    )


async def aclose() -> None:
    """模块级 aclose（兼容层）。"""
    await gateway.aclose()


# 兼容别名
RetryableTimeout = RetryableModelError
