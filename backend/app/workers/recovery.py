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

        # 2) 其余中断任务恢复为 queued（原本就是 queued 的不重复修改）
        cur = conn.execute(
            "UPDATE run_tasks SET status='queued', error=?, updated_at=datetime('now','localtime'), "
            " lease_owner=NULL, lease_expires_at=NULL, cancel_requested=0 "
            "WHERE status IN ('running','interrupted')",
            (RECOVERY_MARKER,),
        )
        report["run_tasks_recovered"] = int(cur.rowcount or 0)
        cur = conn.execute(
            "UPDATE parse_tasks SET status='queued', error=?, updated_at=datetime('now','localtime'), "
            " lease_owner=NULL, lease_expires_at=NULL, cancel_requested=0 "
            "WHERE status IN ('running','interrupted')",
            (RECOVERY_MARKER,),
        )
        report["parse_tasks_recovered"] = int(cur.rowcount or 0)
        cur = conn.execute(
            "UPDATE sync_jobs SET status='pending', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE status='running'"
        )
        report["sync_jobs_recovered"] = int(cur.rowcount or 0)

        # 3) 运行/中断状态的工作流随任务一起回到 queued
        cur = conn.execute(
            "UPDATE workflow_runs SET status='queued', updated_at=datetime('now','localtime') "
            "WHERE status IN ('running','interrupted') AND id IN "
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
                "VALUES (?, 'queued', datetime('now','localtime'), datetime('now','localtime'))",
                (r["id"],),
            )
        report["runs_reenqueued"] = len(orphan_rows)

    if any(v for k, v in report.items() if k.endswith(("recovered", "gave_up", "reenqueued"))):
        logger.info("任务恢复: %s", report)
    return report


def stale_leases() -> int:
    """清理过期 lease 的 running 任务（worker 崩溃遗留），交还队列。"""
    from ..database import execute
    n1 = execute(
        "UPDATE run_tasks SET status='queued', error='lease_expired', "
        " lease_owner=NULL, lease_expires_at=NULL, updated_at=datetime('now','localtime') "
        "WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at < datetime('now','localtime')"
    )
    n2 = execute(
        "UPDATE parse_tasks SET status='queued', error='lease_expired', "
        " lease_owner=NULL, lease_expires_at=NULL, updated_at=datetime('now','localtime') "
        "WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at < datetime('now','localtime')"
    )
    return int(n1 or 0) + int(n2 or 0)


def recover_all() -> dict:
    report = recover_interrupted_tasks()
    report["stale_leases_reclaimed"] = stale_leases()
    return report
