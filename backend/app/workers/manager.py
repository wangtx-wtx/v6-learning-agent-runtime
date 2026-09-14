"""
WorkerManager（方案 3.1/3.3）：Supervisor 模式真并发。

- 队列领取循环持续补位：只要有空位就领取新任务，不再"领一个等一个"；
- running_tasks: dict[run_id, asyncio.Task] 管理在跑任务；
- 领取使用事务内原子 UPDATE + lease，天然支持多进程/多 Worker 安全；
- stop()：停止领取 → 取消在跑任务 → 未完成任务标记 interrupted → 关闭数据库读连接。
"""
from __future__ import annotations

import asyncio
import logging

from ..database import execute, fetch_one, reset_connections
from .workflow_worker import claim_next_run_task, execute_run, finish_run_task, is_cancel_requested
from .material_worker import claim_next_parse_task, execute_parse, finish_parse_task
from ..material_parser import ParseCancelled

logger = logging.getLogger(__name__)

MAX_CONCURRENT_RUNS = 2
MAX_CONCURRENT_PARSES = 2
POLL_INTERVAL = 1.0


class WorkerManager:
    def __init__(self, max_runs: int = MAX_CONCURRENT_RUNS,
                 max_parses: int = MAX_CONCURRENT_PARSES,
                 poll_interval: float = POLL_INTERVAL):
        self.max_runs = max_runs
        self.max_parses = max_parses
        self.poll_interval = poll_interval
        self.running_tasks: dict[int, asyncio.Task] = {}      # run_id -> Task
        self.running_parses: dict[int, asyncio.Task] = {}     # material_id -> Task
        self._stopping = False
        self._wake = asyncio.Event()
        self._supervisor: asyncio.Task | None = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        # 循环绑定对象（Event/Task）必须在所属循环里重建：
        # 测试中 TestClient 每实例一个新循环，模块级单例不能跨循环复用。
        self._stopping = False
        self._wake = asyncio.Event()
        self._supervisor = asyncio.get_running_loop().create_task(self._supervisor_loop())
        logger.info("WorkerManager 启动（runs<=%d, parses<=%d）", self.max_runs, self.max_parses)

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._supervisor:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except (asyncio.CancelledError, Exception):
                pass
        # 取消在跑任务（触发 execute_run 内部的 interrupted/cancelled 落库）
        pending = list(self.running_tasks.values()) + list(self.running_parses.values())
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # 兜底：任何仍处 running 的任务标记 interrupted（V5.5.1: UTC 基准）
        execute(
            "UPDATE run_tasks SET status='interrupted', error='interrupted_by_shutdown', "
            " updated_at=datetime('now') WHERE status='running'"
        )
        execute(
            "UPDATE parse_tasks SET status='interrupted', error='interrupted_by_shutdown', "
            " updated_at=datetime('now') WHERE status='running'"
        )
        execute(
            "UPDATE workflow_runs SET status='interrupted', updated_at=datetime('now') "
            "WHERE status='running'"
        )
        reset_connections()
        logger.info("WorkerManager 已停止")

    def notify(self) -> None:
        """有新任务入队时唤醒 supervisor（立即领取而非等轮询）。"""
        self._wake.set()

    async def cancel(self, run_id: int) -> bool:
        """取消进程内任务；返回是否确实找到了仍在执行的 asyncio task。"""
        t = self.running_tasks.pop(run_id, None)
        if t and not t.done():
            t.cancel()
            return True
        return False

    # ---------------------------------------------------------------- supervisor
    async def _supervisor_loop(self):
        while not self._stopping:
            try:
                self._cleanup_finished()
                await self._fill_run_slots()
                await self._fill_parse_slots()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("supervisor 循环异常")
                await asyncio.sleep(3.0)

    def _cleanup_finished(self):
        for rid in list(self.running_tasks):
            if self.running_tasks[rid].done():
                self.running_tasks.pop(rid)
        for mid in list(self.running_parses):
            if self.running_parses[mid].done():
                self.running_parses.pop(mid)

    async def _fill_run_slots(self):
        while len(self.running_tasks) < self.max_runs and not self._stopping:
            task = await asyncio.to_thread(claim_next_run_task)
            if not task:
                break
            run_id = task["run_id"]
            if run_id in self.running_tasks:
                continue
            if is_cancel_requested(run_id):
                execute(
                    "UPDATE workflow_runs SET status='cancelled', updated_at=datetime('now') "
                    "WHERE id=? AND status IN ('queued','running')",
                    (run_id,),
                )
                finish_run_task(run_id, "cancelled", "cancelled before start")
                continue
            self.running_tasks[run_id] = asyncio.create_task(
                self._run_guarded(run_id), name=f"run-{run_id}"
            )
            logger.info("领取 run %s（attempts=%s）", run_id, task["attempts"])

    async def _fill_parse_slots(self):
        while len(self.running_parses) < self.max_parses and not self._stopping:
            task = await asyncio.to_thread(claim_next_parse_task)
            if not task:
                break
            mid = task["material_id"]
            if mid in self.running_parses:
                continue
            self.running_parses[mid] = asyncio.create_task(
                self._parse_guarded(mid, task["task_id"]), name=f"parse-{mid}"
            )

    async def _run_guarded(self, run_id: int):
        try:
            await execute_run(run_id)
        except (asyncio.CancelledError, Exception):
            # 状态落库已在 execute_run 内完成；此处仅保证任务不向 supervisor 抛出
            pass

    async def _parse_guarded(self, material_id: int, task_id: int):
        try:
            await execute_parse(material_id)
            finish_parse_task(task_id, "done")
        except asyncio.CancelledError:
            finish_parse_task(task_id, "interrupted", "interrupted_by_shutdown")
        except ParseCancelled:
            # 用户取消：任务与材料回到 queued，等待重新入队；不删除任何数据
            finish_parse_task(task_id, "cancelled", "cancelled by user")
            execute(
                "UPDATE materials SET parser_status='queued', status='queued', parse_error=NULL, "
                " updated_at=datetime('now','localtime') WHERE id=?",
                (material_id,),
            )
        except Exception as e:
            logger.exception("材料 %s 解析失败", material_id)
            finish_parse_task(task_id, "failed", str(e))
            execute(
                "UPDATE materials SET status='failed', parser_status='failed', parse_error=?, "
                " updated_at=datetime('now','localtime') WHERE id=?",
                (str(e)[:500], material_id),
            )


# 模块级单例（lifespan 与 API 共用）
worker_manager = WorkerManager()
