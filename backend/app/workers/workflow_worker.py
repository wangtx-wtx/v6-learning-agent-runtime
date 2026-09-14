"""工作流 Worker（方案 3.3）：原子领取 + lease + 真并发执行。

V5.5.1: 租约时间统一使用 UTC ISO 8601（time_utils.utc_now_plus），
不再使用 naive datetime.utcnow()。这样与 SQLite datetime('now') 写入
的时区基准一致，跨时区不再发生 8 小时漂移导致的误过期。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Optional

from ..database import execute, fetch_one, transaction
from ..dag import DAGContext, RunCancelledError
from ..time_utils import now_utc, utc_now_plus_sql

logger = logging.getLogger(__name__)

LEASE_SECONDS = 600          # 单次租约时长；supervisor 心跳续租
WORKER_ID = "wk-" + uuid.uuid4().hex[:8]


def claim_next_run_task(max_attempts: int = 3) -> Optional[dict]:
    """
    原子领取一个 queued 任务（方案 3.3 伪流程）：
    BEGIN IMMEDIATE → SELECT → UPDATE running+lease → COMMIT。
    超过 max_attempts 的任务在此直接判负。

    V5.5.1: 租约统一 UTC（写入 SQLite datetime 字面量），比较用同格式。
    """
    lease_until = utc_now_plus_sql(LEASE_SECONDS)
    with transaction() as conn:
        row = conn.execute(
            "SELECT id, run_id, attempts, max_attempts, cancel_requested FROM run_tasks "
            "WHERE status='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        if (row["attempts"] or 0) >= (row["max_attempts"] or max_attempts):
            conn.execute(
                "UPDATE run_tasks SET status='failed', error='exceeded max attempts', "
                " finished_at=datetime('now'), updated_at=datetime('now') "
                "WHERE id=?",
                (row["id"],),
            )
            return None
        conn.execute(
            "UPDATE run_tasks SET status='running', lease_owner=?, lease_expires_at=?, "
            " started_at=COALESCE(started_at, datetime('now')), "
            " updated_at=datetime('now'), attempts=attempts+1 "
            "WHERE id=? AND status='queued'",
            (WORKER_ID, lease_until, row["id"]),
        )
        return {"task_id": row["id"], "run_id": row["run_id"],
                "attempts": (row["attempts"] or 0) + 1,
                "cancel_requested": row["cancel_requested"]}


def finish_run_task(run_id: int, status: str, error: str = "") -> None:
    """V5.5.1: 写时间统一 UTC（datetime('now')）以与租约基准一致。

    V6 Phase 1: ``status='done'`` 且带 ``error`` 表示「任务执行完毕但运行降级」
    —— 此时保留 error 作为 degraded 原因，不写空串。
    """
    if status == "done":
        execute(
            "UPDATE run_tasks SET status='done', error=?, finished_at=datetime('now'), "
            " updated_at=datetime('now'), lease_owner=NULL, lease_expires_at=NULL "
            "WHERE run_id=?",
            (error[:500] if error else "", run_id),
        )
    else:
        execute(
            "UPDATE run_tasks SET status=?, error=?, finished_at=datetime('now'), "
            " updated_at=datetime('now'), lease_owner=NULL, lease_expires_at=NULL "
            "WHERE run_id=?",
            (status, error[:500], run_id),
        )


def renew_lease(run_id: int) -> None:
    """V5.5.1: 续租时间统一 UTC（SQLite datetime 字面量）。"""
    lease_until = utc_now_plus_sql(LEASE_SECONDS)
    execute(
        "UPDATE run_tasks SET lease_expires_at=?, updated_at=datetime('now') "
        "WHERE run_id=? AND status='running' AND lease_owner=?",
        (lease_until, run_id, WORKER_ID),
    )


def _parent_success_outputs(parent_run_id: int) -> dict[str, dict]:
    """提取父运行每个节点最近一次 success 的 output（供重试复用，方案 3.5）。"""
    from ..database import query
    outputs: dict[str, dict] = {}
    rows = query(
        "SELECT node_name, output_json FROM run_nodes "
        "WHERE run_id=? AND status='success' "
        "ORDER BY id",
        (parent_run_id,),
    )
    for r in rows:
        try:
            outputs[r["node_name"]] = json.loads(r["output_json"]) if r["output_json"] else {}
        except Exception:
            continue
    return outputs


def is_cancel_requested(run_id: int) -> bool:
    row = fetch_one(
        "SELECT MAX(cancel_requested) AS c FROM ("
        "  SELECT cancel_requested FROM run_tasks WHERE run_id=?"
        "  UNION ALL SELECT cancel_requested FROM workflow_runs WHERE id=?)",
        (run_id, run_id),
    )
    return bool(row and row.get("c"))


async def execute_run(run_id: int) -> None:
    """执行单个 run 的 DAG（供 supervisor 的 asyncio.Task 调用）。"""
    from . import build_flow
    from ..gateway import gateway
    from ..routing import load_usage_from_gateway, make_route_for_workflow

    run = fetch_one("SELECT * FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        finish_run_task(run_id, "failed", "workflow_run missing")
        return
    if run.get("status") == "cancelled":
        finish_run_task(run_id, "cancelled", "cancelled before start")
        return
    workflow = run.get("workflow") or ""
    try:
        dag = build_flow(workflow)
    except KeyError:
        finish_run_task(run_id, "failed", f"unknown workflow: {workflow}")
        return

    try:
        inp = json.loads(run.get("input_json") or "{}")
    except Exception:
        inp = {}
    inp = inp if isinstance(inp, dict) else {}

    # 重试语义（方案 3.5）：parent 运行的已成功产物复用到 from_node 之前
    reuse_outputs = None
    reuse_until = None
    retry_meta = inp.pop("_retry", None) or {}
    if retry_meta.get("reuse") and retry_meta.get("from_node"):
        parent_id = int(retry_meta.get("parent_run_id") or 0)
        reuse_until = retry_meta.get("from_node")
        if parent_id:
            reuse_outputs = _parent_success_outputs(parent_id)

    ctx = DAGContext()
    ctx.run_id = run_id
    ctx.input = dict(inp or {})
    ctx.mark_running()
    heartbeat = asyncio.create_task(_heartbeat(run_id))
    try:
        usage = await load_usage_from_gateway(gateway)
        routes = make_route_for_workflow(workflow, _quota_pct(usage))
        ctx.update_model_routes(routes)
        outputs = await dag.run(ctx, reuse_outputs=reuse_outputs, reuse_until=reuse_until)
        # V6 Phase 1: 覆盖门禁未通过时不得写 completed（硬规则）。
        degradation = _coverage_degradation_reason(run_id)
        if degradation:
            ctx.mark_degraded(outputs, reason=degradation)
            finish_run_task(run_id, "done", degradation)
        else:
            ctx.mark_completed(outputs)
            finish_run_task(run_id, "done")
    except (asyncio.CancelledError, RunCancelledError):
        # 用户取消（cancel_requested=1）→ cancelled；仅停机中断 → interrupted
        if is_cancel_requested(run_id):
            ctx.mark_cancelled()
            finish_run_task(run_id, "cancelled", "cancelled by user")
        else:
            ctx.mark_interrupted()
            finish_run_task(run_id, "interrupted", "interrupted_by_shutdown")
        raise
    except Exception as e:
        ctx.mark_failed(str(e))
        finish_run_task(run_id, "failed", str(e))
        raise
    finally:
        heartbeat.cancel()


async def _heartbeat(run_id: int):
    """续租心跳：防止长任务 lease 过期被误回收。"""
    while True:
        await asyncio.sleep(LEASE_SECONDS / 3)
        renew_lease(run_id)


def _coverage_degradation_reason(run_id: int) -> Optional[str]:
    """V6 覆盖门禁结论（**fail-closed**）。

    ``V6_LEARNING_ENGINE=off`` 时返回 ``None``（保持原 V5 行为）。
    shadow/on 模式下，以下任一情况都必须阻断 completed：

    * 覆盖报告缺失（覆盖审计未跑完 / 表缺失 / run 被中断）
    * 报告读取抛错 / JSON 损坏 / gate 取值未知

    早期实现捕获异常后直接返回 ``None``，属于 **fail-open**：读取失败会让
    一个覆盖门禁根本没被判定过的运行写成 ``completed``。现在改为返回显式
    降级原因，运行进入 ``degraded``。
    """
    try:
        from ..learning_engine import config as v6cfg
        if not v6cfg.engine_enabled():
            return None
    except Exception as e:  # pragma: no cover - 连配置都读不到
        logger.warning("引擎模式读取失败（run=%s），按 fail-closed 处理: %s", run_id, e)
        return f"覆盖门禁不可判定（引擎模式读取失败: {type(e).__name__}），已按 fail-closed 降级"

    try:
        from ..learning_engine.coverage import get_degradation_reason
        reason = get_degradation_reason(run_id)
    except Exception as e:
        logger.warning("覆盖门禁读取失败（run=%s），按 fail-closed 处理: %s", run_id, e)
        return f"覆盖门禁不可判定（读取失败: {type(e).__name__}），已按 fail-closed 降级"

    if reason is None:
        # get_degradation_reason 返回 None 有两种可能：门禁 passed，
        # 或**根本没有报告**。后者必须 fail-closed，因此显式复查报告是否存在。
        try:
            from ..database import fetch_one
            row = fetch_one("SELECT gate FROM coverage_reports WHERE run_id=?", (run_id,))
        except Exception as e:
            logger.warning("覆盖报告存在性检查失败（run=%s）: %s", run_id, e)
            return f"覆盖门禁不可判定（报告检查失败: {type(e).__name__}），已按 fail-closed 降级"
        if not row:
            return "覆盖门禁未执行（缺少 coverage_report），已按 fail-closed 降级"
        gate = row.get("gate")
        if gate != "passed":
            return f"覆盖门禁 gate={gate}（未通过），已降级"
    if reason is None:
        try:
            if v6cfg.real_notes_publish_enabled():
                from ..learning_engine.quality import degradation_reason as quality_reason
                reason = quality_reason(run_id)
        except Exception as e:
            return f"质量门禁不可判定（{type(e).__name__}），已按 fail-closed 降级"
    return reason


def _quota_pct(usage) -> float:
    # usage 不可用时不默认解释为 0% 用量（否则可能误用免费路由而超支）；
    # 返回 100 → 走 conservative 路由（官方 DeepSeek / 安全默认）。
    if not usage or not isinstance(usage, dict) or not usage.get("available", True):
        return 100.0
    try:
        data = usage.get("data") if isinstance(usage.get("data"), dict) else usage
        used = data.get("used_5h", 0) or 0
        limit = data.get("limit_5h", 1200) or 1200
        return min(100.0, float(used) / max(float(limit), 1.0) * 100)
    except Exception:
        return 100.0
