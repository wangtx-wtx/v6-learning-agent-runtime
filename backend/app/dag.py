"""
固定 DAG 执行框架。
每个节点是一个 agent role，可重跑、可降级、记录模型与 token。
"""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from .database import execute, query

logger = logging.getLogger(__name__)


class GatewayRetryableError(Exception):
    """网关/上游可恢复错误(5xx / 429 / 超时):DAGNode 应切换到 fallback 模型。"""

    pass


class SchemaValidationError(Exception):
    """模型输出 schema 不符合预期:DAGNode 应切换到 fallback 模型或重试。"""

    pass


class BusinessError(Exception):
    """业务级错误(用户输入不合法):不应触发 fallback,直接 fail。"""

    pass


# V5.2:工作流运行状态机白名单
ALLOWED_RUN_TRANSITIONS: dict[str, set[str]] = {
    "pending":   {"running", "cancelled"},
    "running":   {"completed", "failed", "cancelled"},
    "completed": set(),
    "failed":    set(),
    "cancelled": set(),
}


def _can_transition(old: str, new: str) -> bool:
    """检查 run 状态转换是否合法。同状态 / 未注册状态放行(允许重试)。"""
    if old == new:
        return True
    return new in ALLOWED_RUN_TRANSITIONS.get(old, set())


@dataclass
class DAGNode:
    name: str
    agent_role: str
    handler: Callable[["DAGContext", str], Awaitable[dict]]
    preferred_models: list[str] = field(default_factory=list)
    fallback_models: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)

    async def run(self, ctx: "DAGContext") -> dict:
        """
        模型选择顺序:
          1) primary = ctx.resolve_model(self.agent_role) —— quota 路由结果(若空则用 preferred_models[0])
          2) 候选链 = [primary] + fallback_models + preferred_models(去重保序)
          3) 依次尝试,任何一个成功即返回;全部抛 GatewayRetryableError 等可恢复异常才轮换
        """
        primary = ctx.resolve_model(self.agent_role)
        if not primary and self.preferred_models:
            primary = self.preferred_models[0]
        candidates = list(dict.fromkeys(
            [primary] + list(self.fallback_models) + list(self.preferred_models)
        ))
        candidates = [m for m in candidates if m]  # 去掉 None / 空串

        last_err = None
        for model_id in candidates:
            started_at = datetime.now().isoformat()
            node_id = ctx.begin_node(self.name, self.agent_role, model_id, started_at)
            try:
                result = await self.handler(ctx, model_id)
                result.setdefault("node_name", self.name)
                result.setdefault("agent_role", self.agent_role)
                result.setdefault("model_used", model_id)
                tokens_in = result.get("tokens_in", 0)
                tokens_out = result.get("tokens_out", 0)
                ctx.finish_node(
                    node_id, "success", started_at,
                    tokens_in=tokens_in, tokens_out=tokens_out,
                    output_ref=json.dumps(
                        {k: v for k, v in result.items() if k not in ("tokens_in", "tokens_out")},
                        ensure_ascii=False, default=str,
                    )[:2000],
                )
                return result
            except Exception as e:
                last_err = e
                ctx.finish_node(node_id, "failed", started_at, error=str(e))
                logger.warning(f"节点 {self.name} 使用 {model_id} 失败: {e}")
                continue
        raise RuntimeError(f"节点 {self.name} 所有候选模型均失败: {last_err}")


class DAG:
    def __init__(self, name: str, mode: str):
        self.name = name
        self.mode = mode
        self.nodes: dict[str, DAGNode] = {}
        self._build()

    def _build(self):
        """子类可重写；builder 模式可以不重写。"""
        pass

    def add(self, node: DAGNode):
        self.nodes[node.name] = node

    def execution_order(self) -> list[str]:
        order = []
        visited: set[str] = set()
        temp: set[str] = set()

        def visit(name: str):
            if name in visited:
                return
            if name in temp:
                raise RuntimeError(f"DAG 存在环: {name}")
            temp.add(name)
            node = self.nodes.get(name)
            if node:
                for dep in node.depends_on:
                    if dep in self.nodes:
                        visit(dep)
            temp.remove(name)
            visited.add(name)
            order.append(name)

        for name in self.nodes:
            visit(name)
        return order

    async def run(self, ctx: "DAGContext", node_names: Optional[list[str]] = None):
        names = node_names or self.execution_order()
        outputs: dict[str, dict] = {}
        for name in names:
            node = self.nodes[name]
            node_output = await node.run(ctx)
            outputs[name] = node_output
            ctx.set_output(name, node_output)
        return outputs


class DAGContext:
    """一次工作流的执行上下文"""

    def __init__(self, input_data: Optional[dict] = None, **kwargs):
        self.input: dict[str, Any] = dict(input_data or {})
        self.input.update(kwargs)
        self.outputs: dict[str, dict] = {}
        self.run_id: Optional[int] = None
        self.model_routes: dict[str, str] = {}
        self.node_records: list[dict] = []

    def create_run(self, workflow: str, mode: str, course_id=None, lesson_id=None, chapter_id=None) -> int:
        self.run_id = execute(
            "INSERT INTO workflow_runs (workflow, mode, course_id, lesson_id, chapter_id, status, input_json, created_at) "
            "VALUES (?,?,?,?,?, 'running', ?, datetime('now','localtime'))",
            (workflow, mode, course_id, lesson_id, chapter_id, json.dumps(self.input, ensure_ascii=False)),
            returning_lastrowid=True,
        )
        return self.run_id

    def resolve_model(self, agent_role: str) -> str:
        return self.model_routes.get(agent_role, "deepseek_v4_free")

    def set_output(self, name: str, value: dict):
        self.outputs[name] = value

    def update_model_routes(self, routes: dict[str, str]):
        self.model_routes.update(routes)

    def finish_run(self, status: str = "completed", output: Optional[dict] = None, error: Optional[str] = None):
        # V5.2:状态机白名单——禁止从终态回到非终态
        cur = query_one("SELECT status FROM workflow_runs WHERE id=?", (self.run_id,))
        if cur and not _can_transition(cur.get("status", ""), status):
            logger.warning(f"非法状态转换 {cur.get('status')} -> {status} 被拒绝")
            return
        execute(
            "UPDATE workflow_runs SET status=?, output_json=?, error=? WHERE id=?",
            (status, json.dumps(output or {}, ensure_ascii=False), error or "", self.run_id),
        )

    def begin_node(self, node_name: str, agent_role: str, model: str, started_at: str) -> int:
        node_id = execute(
            "INSERT INTO run_nodes (run_id, node_name, status, agent_role, model, input_ref, output_ref, started_at, "
            "tokens_in, tokens_out, error) VALUES (?,?,?,?,?,?,?,?,0,0,'')",
            (self.run_id, node_name, "running", agent_role, model, str(uuid.uuid4()), None, started_at),
            returning_lastrowid=True,
        )
        return node_id

    def finish_node(self, node_id: int, status: str, started_at: str, tokens_in: int = 0,
                    tokens_out: int = 0, output_ref: Optional[str] = None, error: Optional[str] = None):
        finished = datetime.now().isoformat()
        execute("UPDATE run_nodes SET status=?, output_ref=?, finished_at=?, tokens_in=?, tokens_out=?, error=? WHERE id=?",
                (status, output_ref, finished, tokens_in, tokens_out, error or "", node_id))
