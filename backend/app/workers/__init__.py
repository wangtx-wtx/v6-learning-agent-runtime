"""
V5.5 后台任务包（兼容原 app.workers 的对外 API）。

- enqueue_workflow / enqueue_parse：入队 + 唤醒 supervisor；
- execute_run：单个工作流执行（供 manager 的 asyncio.Task 调用）；
- build_flow / _quota_pct：与 V5.4 保持一致；
- worker_manager：全局 supervisor 单例（lifespan 启停）。
"""
from __future__ import annotations

from .manager import worker_manager  # noqa: F401
from .workflow_worker import (  # noqa: F401
    execute_run,
    finish_run_task,
    claim_next_run_task,
    is_cancel_requested,
    _quota_pct,
)
from .recovery import recover_all, recover_interrupted_tasks  # noqa: F401


def build_flow(workflow: str):
    from ..dag_lesson import build_lesson_dag
    from ..dag_homework import build_homework_dag
    from ..dag_error import build_error_dag
    from ..dag_review import build_review_dag
    mapping = {
        "lesson": build_lesson_dag,
        "homework": build_homework_dag,
        "error": build_error_dag,
        "review": build_review_dag,
    }
    return mapping[workflow]()


def enqueue_workflow(run_id: int) -> None:
    """把工作流 run 加入后台队列（幂等），并立即唤醒 supervisor。"""
    from ..database import insert
    insert(
        "INSERT OR IGNORE INTO run_tasks (run_id, status, created_at, updated_at) "
        "VALUES (?, 'queued', datetime('now','localtime'), datetime('now','localtime'))",
        (run_id,),
    )
    worker_manager.notify()


def enqueue_parse(material_id: int) -> None:
    from ..database import insert
    insert(
        "INSERT OR IGNORE INTO parse_tasks (material_id, status, created_at, updated_at) "
        "VALUES (?, 'queued', datetime('now','localtime'), datetime('now','localtime'))",
        (material_id,),
    )
    worker_manager.notify()
