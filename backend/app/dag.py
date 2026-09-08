"""
DAG 执行引擎（V5.4 重写）。

对照审查报告整改：
- 显式区分 local / llm 节点（kind = 'local' | 'api'）。
- 构建时校验依赖节点存在，缺失依赖启动即失败。
- 真正按拓扑层级并发执行（同层无相互依赖节点并行）。
- solver 与 parallel_solver 可并发；adjudicator/explainer 同时依赖两者。
- BusinessError 不重试、不换模型，直接失败。
- 仅网络超时 / 429 / 部分 5xx（RetryableModelError）可换模型重试。
- Schema 错误先做一次修复请求，再切换模型。
- 401 / 403（AuthError）、400（BadRequestError）直接失败。
- 每次模型尝试都记录独立 attempt。
- 结果写入全量 output_json（不截断到 2000 字符）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from .database import execute, fetch_one, insert

logger = logging.getLogger(__name__)


# ---- 错误分类 ----------------------------------------------------------------------
class RetryableModelError(Exception):
    """可恢复的上游错误（网络超时 / 429 / 502/503/504）。可重试或切换 fallback 模型。"""


class SchemaValidationError(Exception):
    """模型输出不符合预期 schema。先做一次修复请求，仍失败再切换模型。"""


class AuthError(Exception):
    """401 / 403 鉴权错误。直接失败，不重试、不换模型。"""


class BadRequestError(Exception):
    """400 请求格式非法。直接失败。"""


class BusinessError(Exception):
    """业务级错误。直接失败，绝不重试。"""


class RunCancelledError(BaseException):
    """运行被用户取消（方案 3.4）。

    继承 BaseException：避免被节点 handler 的 `except Exception` 吞掉或误转成
    BusinessError，确保取消语义穿透整棵 DAG。
    """


# ---- 运行状态机 ----------------------------------------------------------------------
ALLOWED_RUN_TRANSITIONS: dict[str, set[str]] = {
    "queued":     {"running", "cancelled", "failed"},
    "running":    {"completed", "failed", "cancelled", "interrupted"},
    "completed":  set(),
    "failed":     set(),
    "cancelled":  set(),
    "interrupted": {"queued"},   # 重启后由 recover_interrupted_tasks 恢复
}


def _can_transition(old: str, new: str) -> bool:
    if old == new:
        return True
    return new in ALLOWED_RUN_TRANSITIONS.get(old, set())


# 本地节点传入的占位模型 ID（handler 不应真去调用网关）
LOCAL_MODEL = "_local_"


@dataclass
class DAGNode:
    name: str
    agent_role: str
    handler: Callable[["DAGContext", str], Awaitable[dict]]
    preferred_models: list[str] = field(default_factory=list)
    fallback_models: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    kind: str = "api"  # "api" | "local"

    @property
    def is_local(self) -> bool:
        return self.kind == "local"


class DAG:
    """有依赖的节点集合；按拓扑层级并发执行。"""

    def __init__(self, name: str, mode: str):
        self.name = name
        self.mode = mode
        self.nodes: dict[str, DAGNode] = {}
        self._validated = False

    def add(self, node: DAGNode) -> "DAG":
        self.nodes[node.name] = node
        return self

    def _validate(self):
        for name, node in self.nodes.items():
            missing = [d for d in node.depends_on if d not in self.nodes]
            if missing:
                raise RuntimeError(f"DAG {self.name}: 节点 {name} 依赖缺失: {missing}")
        visiting, done = set(), set()

        def visit(n: str):
            if n in done:
                return
            if n in visiting:
                raise RuntimeError(f"DAG {self.name}: 存在环 -> {n}")
            visiting.add(n)
            for d in self.nodes[n].depends_on:
                visit(d)
            visiting.remove(n)
            done.add(n)

        for n in self.nodes:
            visit(n)
        self._validated = True

    def topological_layers(self) -> list[list[str]]:
        if not self._validated:
            self._validate()
        indegree = {n: 0 for n in self.nodes}
        adj: dict[str, list[str]] = {n: [] for n in self.nodes}
        for name, node in self.nodes.items():
            for dep in node.depends_on:
                if dep in self.nodes:
                    adj[dep].append(name)
                    indegree[name] += 1
        layers: list[list[str]] = []
        remaining = set(self.nodes)
        ready = [n for n in self.nodes if indegree[n] == 0]
        while ready:
            layer = [n for n in ready if n in remaining]
            layers.append(layer)
            for n in layer:
                remaining.discard(n)
            nxt = []
            for n in layer:
                for m in adj[n]:
                    indegree[m] -= 1
                    if indegree[m] == 0:
                        nxt.append(m)
            ready = nxt
        if remaining:
            raise RuntimeError(f"DAG {self.name}: 仍存在环，无法分层")
        return layers

    async def run(self, ctx: "DAGContext", reuse_outputs: Optional[dict[str, dict]] = None,
                  reuse_until: Optional[str] = None) -> dict[str, dict]:
        """
        按拓扑层级并发执行。

        reuse_outputs + reuse_until（方案 3.5 重试）：从某个节点的层级之前全部复用
        已成功产物（记录为 reused 节点，审计可见），该层级起正常执行。
        """
        if not self._validated:
            self._validate()
        outputs: dict[str, dict] = {}
        reuse_map = dict(reuse_outputs or {})
        layers = self.topological_layers()
        cut_idx = len(layers)
        if reuse_until is not None:
            if reuse_until not in self.nodes:
                raise BusinessError(f"重试起点节点不存在: {reuse_until}")
            for i, layer in enumerate(layers):
                if reuse_until in layer:
                    cut_idx = i
                    break
        for li, layer in enumerate(layers):
            await ctx.raise_if_cancelled()          # 取消检查：每拓扑层开始前
            if li < cut_idx and reuse_map:
                coros = [
                    self._run_reused(name, ctx, reuse_map) if name in reuse_map
                    else self._run_node_wrapped(name, ctx)
                    for name in layer
                ]
            else:
                coros = [self._run_node_wrapped(name, ctx) for name in layer]
            results = await asyncio.gather(*coros)
            for name, out in zip(layer, results):
                outputs[name] = out
                ctx.set_output(name, out)
        return outputs

    async def _run_reused(self, name: str, ctx: "DAGContext", reuse_map: dict[str, dict]) -> dict:
        """复用上一轮已成功产物，写一条 reused 节点记录（审计链不丢）。"""
        node = self.nodes[name]
        started = time.time()
        node_id = ctx.start_node(node, LOCAL_MODEL, datetime.now().isoformat(), attempt=1)
        out = dict(reuse_map.get(name) or {})
        ctx.finish_node(node_id, "reused", out, started=started, attempt=1,
                        latency_msf=0)
        return out

    async def _run_node_wrapped(self, name: str, ctx: "DAGContext") -> dict:
        await ctx.raise_if_cancelled()                  # 取消检查：每节点开始前
        node = self.nodes[name]
        if node.is_local:
            return await self._run_node(node, LOCAL_MODEL, ctx, attempt=1)
        return await self._run_with_models(node, ctx)

    async def _run_with_models(self, node: DAGNode, ctx: "DAGContext") -> dict:
        primary = ctx.resolve_model(node.agent_role)
        if not primary and node.preferred_models:
            primary = node.preferred_models[0]
        candidates = list(dict.fromkeys(
            [primary] + list(node.fallback_models) + list(node.preferred_models)
        ))
        candidates = [m for m in candidates if m]
        last_err: Optional[BaseException] = None
        for idx, model_id in enumerate(candidates):
            attempt = idx + 1
            await ctx.raise_if_cancelled()              # 取消检查：模型重试前
            try:
                return await self._run_node(node, model_id, ctx, attempt)
            except (AuthError, BadRequestError, BusinessError):
                raise
            except SchemaValidationError as se:
                last_err = se
                # 先做一次修复请求（同一模型，fix 语义由 handler 自行识别），再不成功换模型
                try:
                    return await self._run_node(node, model_id, ctx, attempt, fix=True)
                except Exception as fe:
                    last_err = fe
                    logger.warning(f"节点 {node.name} schema 修复失败，切换模型: {fe}")
            except (RetryableModelError, asyncio.TimeoutError) as re_:
                last_err = re_
                logger.warning(f"节点 {node.name} 模型 {model_id} 可恢复错误: {re_}")
            except Exception as e:
                last_err = e
                logger.warning(f"节点 {node.name} 模型 {model_id} 异常: {e}")
        raise RuntimeError(
            f"节点 {node.name} 所有候选模型均失败: {last_err}"
        )

    async def _run_node(self, node: DAGNode, model_id: str, ctx: "DAGContext",
                        attempt: int, fix: bool = False) -> dict:
        """单次模型尝试：记录 started/finished 节点，写入全量 output_json。"""
        from .gateway import set_call_context, reset_call_context
        started = time.time()
        started_iso = datetime.now().isoformat()
        node_id = ctx.start_node(node, model_id, started_iso, attempt)
        ctx.fix_requested = fix
        token = set_call_context(run_id=ctx.run_id, node_id=node.name, attempt=attempt)
        try:
            result = await node.handler(ctx, model_id)
            result.setdefault("node_name", node.name)
            result.setdefault("agent_role", node.agent_role)
            result.setdefault("model_used", model_id)
            ctx.finish_node(node_id, "success", result, started=started, attempt=attempt,
                            latency_msf=int((time.time() - started) * 1000))
            return result
        except (asyncio.CancelledError, RunCancelledError):
            # 取消也要落节点记录，避免 run_nodes 永远停留在 running
            ctx.finish_node(node_id, "cancelled", result={}, started=started,
                            attempt=attempt, error="run cancelled",
                            latency_msf=int((time.time() - started) * 1000))
            raise
        except Exception as e:
            ctx.finish_node(node_id, "failed", result={}, started=started,
                            attempt=attempt, error=str(e),
                            latency_msf=int((time.time() - started) * 1000))
            raise
        finally:
            reset_call_context(token)

    async def rerun_node(self, ctx: "DAGContext", name: str, model_id: Optional[str] = None) -> dict:
        node = self.nodes[name]
        mid = model_id or ctx.resolve_model(node.agent_role) or (node.preferred_models[0] if node.preferred_models else LOCAL_MODEL)
        return await self._run_node(node, mid, ctx, attempt=1)


class DAGContext:
    """一次工作流的执行上下文。"""

    def __init__(self, input_data: Optional[dict] = None, **kwargs):
        self.input: dict[str, Any] = dict(input_data or {})
        self.input.update(kwargs)
        self.outputs: dict[str, dict] = {}
        self.run_id: Optional[int] = None
        self.model_routes: dict[str, str] = {}

    def create_run(self, workflow: str, mode: str, course_id=None, lesson_id=None, chapter_id=None) -> int:
        self.run_id = insert(
            "INSERT INTO workflow_runs (workflow, mode, course_id, lesson_id, chapter_id, status, input_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?, 'queued', ?, datetime('now','localtime'), datetime('now','localtime'))",
            (workflow, mode, course_id, lesson_id, chapter_id, json.dumps(self.input, ensure_ascii=False, default=str)),

)
        return self.run_id

    def set_output(self, name: str, value: dict):
        self.outputs[name] = value

    def resolve_model(self, agent_role: str) -> str:
        if self.model_routes.get(agent_role):
            return self.model_routes[agent_role]
        return DEFAULT_ROUTES.get(agent_role, "")

    def update_model_routes(self, routes: dict[str, str]):
        self.model_routes.update(routes)

    def mark_running(self):
        self._write_status("running")

    def mark_completed(self, output: dict):
        self._write_status("completed", output=output)

    def mark_failed(self, error: str):
        self._write_status("failed", error=error)

    def mark_cancelled(self):
        self._write_status("cancelled")

    def mark_interrupted(self, error: str = "interrupted_by_shutdown"):
        self._write_status("interrupted", error=error)

    async def raise_if_cancelled(self):
        """取消检查点（方案 3.4）：运行任一节点/层/模型重试前调用。"""
        if not self.run_id:
            return
        row = fetch_one(
            "SELECT cancel_requested FROM workflow_runs WHERE id=?", (self.run_id,)
        )
        if row and row.get("cancel_requested"):
            raise RunCancelledError(f"run {self.run_id} 已请求取消")

    def _write_status(self, status: str, output: dict = None, error: str = ""):
        cur = fetch_one("SELECT status FROM workflow_runs WHERE id=?", (self.run_id,))
        old = cur.get("status", "") if cur else ""
        if cur and not _can_transition(old, status):
            logger.warning(f"非法状态转换 {old} -> {status} 被拒绝")
            return
        execute(
            "UPDATE workflow_runs SET status=?, output_json=?, error=?, updated_at=datetime('now','localtime') WHERE id=?",
            (status, json.dumps(output or {}, ensure_ascii=False, default=str), (error or "")[:2000], self.run_id),
        )

    def start_node(self, node: DAGNode, model_id: str, started_iso: str, attempt: int = 1) -> int:
        return insert(
            "INSERT INTO run_nodes (run_id, node_name, status, agent_role, model, attempt, input_ref, "
            " started_at, tokens_in, tokens_out, latency_ms, error) "
            "VALUES (?,?,?,?,?,?,?,?,0,0,0,'')",
            (self.run_id, node.name, "running", node.agent_role, model_id or LOCAL_MODEL_ID,
             attempt, str(uuid.uuid4()), started_iso),

)

    def finish_node(self, node_id: int, status: str, result: dict,
                    started: float = None, attempt: int = 1, error: str = "",
                    latency_msf: Optional[int] = None):
        finished = datetime.now().isoformat()
        tokens_in = result.get("tokens_in", 0) if isinstance(result, dict) else 0
        tokens_out = result.get("tokens_out", 0) if isinstance(result, dict) else 0
        latency = latency_msf if latency_msf is not None else \
            (int((time.time() - started) * 1000) if started else 0)
        execute(
            "UPDATE run_nodes SET status=?, output_json=?, output_ref=?, finished_at=?, tokens_in=?, tokens_out=?, "
            " latency_ms=?, attempt=?, error=? WHERE id=?",
            (status, json.dumps(result or {}, ensure_ascii=False, default=str), None, finished,
             tokens_in, tokens_out, latency, attempt, (error or "")[:4000], node_id),
        )


# ---- 默认模型路由（与 config.DEFAULT_ROUTE 对齐） -------------------------------
DEFAULT_ROUTES: dict[str, str] = {
    "student_simulator": "deepseek_v4_free",
    "note_writer": "deepseek_v4_free",
    "critic": "qwen3_8_27b",
    "evidence_auditor": "qwen3_8_27b",
    "scope_auditor": "qwen3_8_27b",
    "solver": "deepseek_v4_free",
    "parallel_solver": "minimax_m3",
    "solution_explainer": "qwen3_8_27b",
    "error_analyst": "deepseek_v4_free",
    "review_writer": "deepseek_v4_free",
    "self_test_writer": "qwen3_flash",
    "vision_reader": "qwen3_vl",
    "transcriber_splitter": "qwen3_flash",
    "lesson_structurer": "qwen3_flash",
    "adjudicator": "qwen3_8_27b",
}