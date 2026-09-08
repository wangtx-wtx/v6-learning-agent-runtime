"""
本地网关客户端：只读访问 http://127.0.0.1:8080
网关是 Unified API Gateway（纯转发代理，Node.js HTTP 服务），本身不是 LLM；
它负责把 v5 的请求转发到上游真实模型（DeepSeek/Qwen/MiniMax 等）。
只读访问，不写、不改、不重启网关。
"""
import json
import logging
import time
from typing import Any, Optional

import httpx

from .config import GATEWAY_API_KEY, GATEWAY_BASE_URL, GATEWAY_TIMEOUT
from .models_registry import get_model, ModelSpec

logger = logging.getLogger(__name__)


class GatewayClient:
    def __init__(self, base_url: str = GATEWAY_BASE_URL, timeout: float = GATEWAY_TIMEOUT, api_key: str = GATEWAY_API_KEY):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if extra:
            h.update(extra)
        return h

    async def chat(
        self,
        model_id: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict] = None,
    ) -> dict:
        """调用网关 chat/completions。返回 {content, reasoning_content, tokens_in, tokens_out, model_used}"""
        spec = get_model(model_id)
        payload: dict[str, Any] = {
            "model": spec.gateway_model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if response_format:
            payload["response_format"] = response_format

        started = time.time()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

        elapsed = time.time() - started
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})

        usage = data.get("usage", {})
        result = {
            "content": msg.get("content", ""),
            "reasoning_content": msg.get("reasoning_content") or "",
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "model": spec.gateway_model,
            "elapsed_ms": int(elapsed * 1000),
        }
        return result

    async def get_usage(self) -> dict:
        """只读获取网关用量 /admin/api/usage"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/admin/api/usage", headers=self._headers())
            if resp.status_code == 404:
                return {"available": False, "detail": "端点不存在"}
            resp.raise_for_status()
            data = resp.json()
        data.setdefault("available", True)
        return data

    async def embedding_single(self, text: str) -> list[float]:
        """调用 embedding 模型，返回向量"""
        model = get_model("embedding")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/embeddings",
                json={"model": model.gateway_model, "input": text},
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        item = data.get("data", [{}])[0]
        return item.get("embedding", [])


gateway = GatewayClient()
