"""
V5.4 模型网关客户端（只读访问 http://127.0.0.1:8080）。

可靠性增强（方案 9.3）：
- 单例连接池。
- 429 / 5xx 指数退避重试（默认最多 2 次）。
- 并发信号量限制同时 LLM 请求数。
- 熔断：连续失败后短暂搁置。
- 请求 trace id。
- 错误标准化：抛 RetryableModelError / AuthError / BadRequestError。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any, Optional

import httpx

from .config import GATEWAY_API_KEY, GATEWAY_BASE_URL, GATEWAY_TIMEOUT
from .dag import RetryableModelError, AuthError, BadRequestError
from .models_registry import get_model

logger = logging.getLogger(__name__)

_pool: Optional[httpx.AsyncClient] = None
_pool_lock = threading.Lock()
_LLM_SEMAPHORE = asyncio.Semaphore(6)

# 熔断状态
_circuit_counter = 0
_circuit_opened_until = 0.0
_CIRCUIT_FAILURES = 4
_CIRCUIT_OPEN_SECONDS = 20


def _circuit_allow() -> bool:
    global _circuit_opened_until
    return time.time() >= _circuit_opened_until


def _record_success():
    global _circuit_counter
    _circuit_counter = 0


def _record_failure():
    global _circuit_counter, _circuit_opened_until
    _circuit_counter += 1
    if _circuit_counter >= _CIRCUIT_FAILURES:
        _circuit_opened_until = time.time() + _CIRCUIT_OPEN_SECONDS
        _circuit_counter = 0
        logger.warning("网关熔断打开 %ss", _CIRCUIT_OPEN_SECONDS)


def _get_client() -> httpx.AsyncClient:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=GATEWAY_TIMEOUT, write=30.0, pool=10.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return _pool


def _trace_id() -> str:
    import uuid
    return "tr-" + uuid.uuid4().hex[:12]


def _map_status_error(code: int, body: str = "") -> None:
    if code in (401, 403):
        raise AuthError(f"gateway 鉴权失败 status={code}: {body[:120]}")
    if code == 400:
        raise BadRequestError(f"gateway 400 请求格式错误: {body[:120]}")
    if code == 429 or code in (502, 503, 504) or code >= 500:
        raise RetryableModelError(f"gateway 可恢复错误 status={code}")


async def _sleep(attempt: int) -> None:
    await asyncio.sleep(min(0.6 * (2 ** attempt), 6.0))


class GatewayClient:
    def __init__(self, base_url: str = GATEWAY_BASE_URL, timeout: float = GATEWAY_TIMEOUT, api_key: str = GATEWAY_API_KEY):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key

    def _headers(self, trace: str = "", extra: dict | None = None) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if trace:
            h["X-Trace-Id"] = trace
        if extra:
            h.update(extra)
        return h

    async def post_json(self, path: str, payload: dict, retries: int = 2) -> tuple[int, dict, str]:
        """POST + 重试，返回 (status_code, json_data, trace_id)。"""
        if not _circuit_allow():
            raise RetryableModelError("网关熔断中，暂缓请求")
        client = _get_client()
        trace = _trace_id()
        last: Optional[BaseException] = None
        for attempt in range(retries + 1):
            try:
                async with _LLM_SEMAPHORE:
                    resp = await client.post(self.base_url + path, json=payload, headers=self._headers(trace))
                if resp.status_code == 429 or resp.status_code >= 500:
                    _record_failure()
                    if attempt < retries:
                        await _sleep(attempt)
                        continue
                else:
                    _record_success()
                try:
                    data = resp.json()
                except Exception:
                    data = {}
                return resp.status_code, data, trace
            except (AuthError, BadRequestError):
                raise
            except RetryableModelError as e:
                if attempt < retries:
                    await _sleep(attempt)
                    last = e
                    continue
                raise
            except (httpx.TimeoutException, httpx.TransportError) as e:
                _record_failure()
                if attempt < retries:
                    await _sleep(attempt)
                    last = e
                    continue
                raise RetryableModelError(f"网关连接失败: {e}")
        raise RetryableModelError(f"网关请求失败(重试{retries}): {last}")

    async def chat(self, model_id: str, messages: list[dict], temperature: float = 0.7,
                   max_tokens: Optional[int] = None, response_format: Optional[dict] = None) -> dict:
        spec = get_model(model_id)
        payload: dict[str, Any] = {
            "model": spec.gateway_model, "messages": messages,
            "temperature": temperature, "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if response_format:
            payload["response_format"] = response_format
        started = time.time()
        code, data, trace = await self.post_json("/v1/chat/completions", payload)
        if code != 200:
            _map_status_error(code, json.dumps(data, ensure_ascii=False))
            # 若仍未抛出（理论上不会），兜底
            raise RetryableModelError(f"chat 非 200: {code}")
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        usage = data.get("usage", {}) or {}
        content = msg.get("content", "") or ""
        if not content.strip():
            raise RetryableModelError("网关返回空内容")
        elapsed = int((time.time() - started) * 1000)
        return {
            "content": content,
            "reasoning_content": msg.get("reasoning_content") or "",
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "model": spec.gateway_model,
            "elapsed_ms": elapsed,
        }

    async def get_usage(self) -> dict:
        client = _get_client()
        trace = _trace_id()
        try:
            resp = await client.get(self.base_url + "/admin/api/usage", headers=self._headers(trace))
            if resp.status_code == 404:
                return {"available": False, "detail": "端点不存在"}
            if resp.status_code == 429 or resp.status_code >= 500:
                _map_status_error(resp.status_code, resp.text)
            data = resp.json()
            data.setdefault("available", True)
            return data
        except httpx.TransportError:
            return {"available": False, "error": "gateway unreachable"}

    async def embedding_single(self, text: str) -> list[float]:
        model = get_model("embedding")
        code, data, trace = await self.post_json("/v1/embeddings", {"model": model.gateway_model, "input": text})
        if code != 200:
            return []
        return (data.get("data") or [{}])[0].get("embedding", [])


gateway = GatewayClient()


# 兼容别名
RetryableTimeout = RetryableModelError