"""材料解析 Worker（方案 3.1/3.2/4.3）：原子领取 + lease + 取消 + 状态机。"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from ..database import execute, transaction

logger = logging.getLogger(__name__)

PARSE_LEASE_SECONDS = 300
_PARSE_WORKER_ID = "pw-" + uuid.uuid4().hex[:8]


def claim_next_parse_task(max_attempts: int = 3) -> Optional[dict]:
    now = datetime.utcnow()
    lease_until = (now + timedelta(seconds=PARSE_LEASE_SECONDS)).isoformat()
    with transaction() as conn:
        row = conn.execute(
            "SELECT id, material_id, attempts, max_attempts FROM parse_tasks "
            "WHERE status='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        if (row["attempts"] or 0) >= (row["max_attempts"] or max_attempts):
            conn.execute(
                "UPDATE parse_tasks SET status='failed', error='exceeded max attempts', "
                " finished_at=datetime('now','localtime'), updated_at=datetime('now','localtime') "
                "WHERE id=?",
                (row["id"],),
            )
            return None
        conn.execute(
            "UPDATE parse_tasks SET status='running', lease_owner=?, lease_expires_at=?, "
            " started_at=COALESCE(started_at, datetime('now','localtime')), "
            " updated_at=datetime('now','localtime'), attempts=attempts+1 "
            "WHERE id=? AND status='queued'",
            (_PARSE_WORKER_ID, lease_until, row["id"]),
        )
        return {"task_id": row["id"], "material_id": row["material_id"],
                "attempts": (row["attempts"] or 0) + 1}


def finish_parse_task(task_id: int, status: str, error: str = "") -> None:
    if status == "done":
        execute(
            "UPDATE parse_tasks SET status='done', error='', finished_at=datetime('now','localtime'), "
            " updated_at=datetime('now','localtime'), lease_owner=NULL, lease_expires_at=NULL "
            "WHERE id=?",
            (task_id,),
        )
    else:
        execute(
            "UPDATE parse_tasks SET status=?, error=?, finished_at=datetime('now','localtime'), "
            " updated_at=datetime('now','localtime'), lease_owner=NULL, lease_expires_at=NULL "
            "WHERE id=?",
            (status, error[:500], task_id),
        )


async def execute_parse(material_id: int) -> None:
    """执行单个材料解析（含取消支持，方案 3.4：分页阶段检查取消）。"""
    from ..material_parser import run_parse_material

    await run_parse_material(material_id)
