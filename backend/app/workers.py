"""
V5.4 应用内后台任务队列（SQLite 持久化）+ Worker。

- 启动/运行时可恢复 queued 任务（应用重启不丢任务）。
- 一次消费一个 run 的 DAG，asmylimited 并发 LLM。
- 支持取消（run 置 cancelled，worker 下一个节点前检查）。
- 支持从失败节点重跑（rerun_node）。
- 幂等：同一 run_id 不会重复入队（run_tasks 主键去重）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

from .database import execute, fetch_one

logger = logging.getLogger(__name__)

MAX_CONCURRENT_RUNS = 2
_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
_worker_started = False
_parse_worker_started = False


# --------------------------------------------------------------------------- workflow
def enqueue_workflow(run_id: int) -> None:
    """把工作流 run 加入后台队列（幂等：主键去重）。"""
    execute(
        "INSERT OR IGNORE INTO run_tasks (run_id, status, created_at) "
        "VALUES (?, 'queued', datetime('now','localtime'))",
        (run_id,),
    )
    _ensure_worker()


def _ensure_worker():
    global _worker_started
    if _worker_started:
        return
    threading.Thread(target=_run_worker_blocking, daemon=True).start()
    _worker_started = True


def _run_worker_blocking():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_worker_loop())


async def _worker_loop():
    while True:
        try:
            task = fetch_one(
                "SELECT run_id FROM run_tasks WHERE status='queued' ORDER BY id LIMIT 1"
            )
            if task is None:
                await asyncio.sleep(1.0)
                continue
            run_id = task["run_id"]
            row = fetch_one("SELECT status FROM workflow_runs WHERE id=?", (run_id,))
            if not row:
                execute("DELETE FROM run_tasks WHERE run_id=?", (run_id,))
                continue
            if row.get("status") == "cancelled":
                execute("UPDATE run_tasks SET status='cancelled' WHERE run_id=?", (run_id,))
                continue
            execute("UPDATE run_tasks SET status='running' WHERE run_id=?", (run_id,))
            async with _semaphore:
                try:
                    await execute_run(run_id)
                    execute(
                        "UPDATE run_tasks SET status='done', finished_at=datetime('now','localtime') WHERE run_id=?",
                        (run_id,),
                    )
                except Exception as e:
                    logger.exception("run %s 执行失败", run_id)
                    execute(
                        "UPDATE run_tasks SET status='failed', error=?, finished_at=datetime('now','localtime') WHERE run_id=?",
                        (str(e)[:500], run_id),
                    )
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.exception("worker 循环异常")
            await asyncio.sleep(3.0)


async def execute_run(run_id: int) -> None:
    """执行单个 run 的 DAG（worker 主流程）。"""
    from .dag import DAGContext, DAG
    from .gateway import gateway
    from .routing import load_usage_from_gateway, make_route_for_workflow

    run = fetch_one("SELECT * FROM workflow_runs WHERE id=?", (run_id,))
    if not run:
        return
    workflow = run.get("workflow") or ""
    dag = build_flow(workflow)

    import json as _json
    try:
        inp = _json.loads(run.get("input_json") or "{}")
    except Exception:
        inp = {}
    ctx = DAGContext()
    ctx.run_id = run_id
    ctx.input = dict(inp or {})
    ctx.mark_running()
    try:
        usage = await load_usage_from_gateway(gateway)
        routes = make_route_for_workflow(workflow, _quota_pct(usage))
        ctx.update_model_routes(routes)
        outputs = await dag.run(ctx)
        ctx.mark_completed(outputs)
    except asyncio.CancelledError:
        ctx.mark_cancelled()
        raise
    except Exception as e:
        ctx.mark_failed(str(e))
        raise


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


def build_flow(workflow: str):
    from .dag_lesson import build_lesson_dag
    from .dag_homework import build_homework_dag
    from .dag_error import build_error_dag
    from .dag_review import build_review_dag
    mapping = {
        "lesson": build_lesson_dag,
        "homework": build_homework_dag,
        "error": build_error_dag,
        "review": build_review_dag,
    }
    return mapping[workflow]()


# --------------------------------------------------------------------------- 材料解析
def enqueue_parse(material_id: int) -> None:
    execute(
        "INSERT OR IGNORE INTO parse_tasks (material_id, status, created_at) "
        "VALUES (?, 'queued', datetime('now','localtime'))",
        (material_id,),
    )
    _ensure_parse_worker()


def _ensure_parse_worker():
    global _parse_worker_started
    if _parse_worker_started:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        _parse_worker_started = True
        loop.create_task(_parse_worker_loop())
    else:
        _parse_worker_started = True
        threading.Thread(target=_run_parse_worker_blocking, daemon=True).start()


def _run_parse_worker_blocking():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_parse_worker_loop())


async def _parse_worker_loop():
    from .material_parser import run_parse_material
    while True:
        task = fetch_one(
            "SELECT id, material_id FROM parse_tasks WHERE status='queued' ORDER BY id LIMIT 1"
        )
        if task is None:
            await asyncio.sleep(1.0)
            continue
        execute("UPDATE parse_tasks SET status='running' WHERE id=?", (task["id"],))
        try:
            await run_parse_material(task["material_id"])
            execute(
                "UPDATE parse_tasks SET status='done', finished_at=datetime('now','localtime') WHERE id=?",
                (task["id"],),
            )
        except Exception as e:
            logger.exception("材料解析失败")
            execute(
                "UPDATE parse_tasks SET status='failed', error=?, finished_at=datetime('now','localtime') WHERE id=?",
                (str(e)[:500], task["id"]),
            )
        await asyncio.sleep(0.2)


def restore_pending_tasks():
    """启动时把遗留 queued/running 的 run_tasks 置回 queued，由 worker 重新领取。"""
    conn = _get_conn_raw()
    try:
        conn.execute("UPDATE run_tasks SET status='queued' WHERE status IN ('running','queued')")
        conn.commit()
    finally:
        conn.close()


def _get_conn_raw():
    import sqlite3
    from .config import DB_PATH
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn