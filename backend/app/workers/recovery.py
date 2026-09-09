"""恢复中断任务（方案 3.2）。

启动时统一恢复 run_tasks / parse_tasks / sync_jobs / workflow_runs：
- 只有 running / interrupted 会被恢复；completed / failed / cancelled 不动；
- 已超过 max_attempts 的任务转 failed（不再无限重试）；
- 每次领取时 attempts 自增，即恢复次数被记录；
- 恢复后确保每个 queued 运行都有对应 run_tasks 行。
"""
from __future__ import annotations

import logging

from ..database import fetch_all, transaction

logger = logging.getLogger(__name__)

RECOVERY_MARKER = "recovered_after_restart"


def recover_interrupted_tasks() -> dict:
    report = {"run_tasks_recovered": 0, "run_tasks_gave_up": 0,
              "parse_tasks_recovered": 0, "parse_tasks_gave_up": 0,
              "sync_jobs_recovered": 0, "runs_recovered": 0}

    with transaction() as conn:
        # 1) 超过最大尝试次数的任务直接判负，不再恢复
        cur = conn.execute(
            "UPDATE run_tasks SET status='failed', error='exceeded max attempts', "
            " finished_at=datetime('now','localtime'), updated_at=datetime('now','localtime') "
            "WHERE status IN ('running','interrupted') AND attempts >= max_attempts"
        )
        report["run_tasks_gave_up"] = int(cur.rowcount or 0)
        cur = conn.execute(
            "UPDATE parse_tasks SET status='failed', error='exceeded max attempts', "
            " finished_at=datetime('now','localtime'), updated_at=datetime('now','localtime') "
            "WHERE status IN ('running','interrupted') AND attempts >= max_attempts"
        )
        report["parse_tasks_gave_up"] = int(cur.rowcount or 0)

        # 2) interrupted 状态全部回到 queued（stop() 留下的标记）
        # V5.5.1: 不再重置所有 running 行——running 由 stale_leases() 单独
        # 按 lease_expires_at 判断是否回收；本机单实例假设下不会命中，
        # 但代码不再依赖该假设，理论支持"另一个实例仍持有租约"。
        cur = conn.execute(
            "UPDATE run_tasks SET status='queued', error=?, updated_at=datetime('now'), "
            " lease_owner=NULL, lease_expires_at=NULL, cancel_requested=0 "
            "WHERE status='interrupted'",
            (RECOVERY_MARKER,),
        )
        report["run_tasks_recovered"] = int(cur.rowcount or 0)
        cur = conn.execute(
            "UPDATE parse_tasks SET status='queued', error=?, updated_at=datetime('now'), "
            " lease_owner=NULL, lease_expires_at=NULL, cancel_requested=0 "
            "WHERE status='interrupted'",
            (RECOVERY_MARKER,),
        )
        report["parse_tasks_recovered"] = int(cur.rowcount or 0)
        cur = conn.execute(
            "UPDATE sync_jobs SET status='pending', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE status='running'"
        )
        report["sync_jobs_recovered"] = int(cur.rowcount or 0)

        # 3) workflow_runs 的 interrupted 回到 queued；running 留给 stale_leases
        cur = conn.execute(
            "UPDATE workflow_runs SET status='queued', updated_at=datetime('now') "
            "WHERE status='interrupted' AND id IN "
            "  (SELECT run_id FROM run_tasks WHERE status='queued')"
        )
        report["runs_recovered"] = int(cur.rowcount or 0)

        # 4) queued 但没有任务行的运行：补一个任务行（防丢）
        orphan_rows = conn.execute(
            "SELECT id FROM workflow_runs WHERE status='queued' AND id NOT IN "
            "  (SELECT run_id FROM run_tasks)"
        ).fetchall()
        for r in orphan_rows:
            conn.execute(
                "INSERT INTO run_tasks (run_id, status, created_at, updated_at) "
                "VALUES (?, 'queued', datetime('now'), datetime('now'))",
                (r["id"],),
            )
        report["runs_reenqueued"] = len(orphan_rows)

    if any(v for k, v in report.items() if k.endswith(("recovered", "gave_up", "reenqueued"))):
        logger.info("任务恢复: %s", report)
    return report


def stale_leases() -> int:
    """仅回收 lease_expires_at 已经过期的 running 任务（worker 崩溃遗留）。

    V5.5.1: 比较用 ``datetime('now')`` 与写入的 ``lease_expires_at`` 都
    是 SQLite UTC 字面量，跨时区不再漂移；不再无条件重置所有 running。
    """
    from ..database import execute
    n1 = execute(
        "UPDATE run_tasks SET status='queued', error='lease_expired', "
        " lease_owner=NULL, lease_expires_at=NULL, updated_at=datetime('now') "
        "WHERE status='running' AND lease_expires_at IS NOT NULL "
        "  AND lease_expires_at < datetime('now')"
    )
    n2 = execute(
        "UPDATE parse_tasks SET status='queued', error='lease_expired', "
        " lease_owner=NULL, lease_expires_at=NULL, updated_at=datetime('now') "
        "WHERE status='running' AND lease_expires_at IS NOT NULL "
        "  AND lease_expires_at < datetime('now')"
    )
    # workflow_runs 跟随 run_tasks 一起回到 queued
    try:
        execute(
            "UPDATE workflow_runs SET status='queued', updated_at=datetime('now') "
            "WHERE status='running' AND id IN ("
            "  SELECT run_id FROM run_tasks WHERE status='queued' AND error='lease_expired'"
            ")"
        )
    except Exception:
        pass
    return int(n1 or 0) + int(n2 or 0)


def recover_all() -> dict:
    report = recover_interrupted_tasks()
    report["stale_leases_reclaimed"] = stale_leases()
    return report
