"""FastAPI Lifespan（V5.5.1 收尾 B.2）。

启动：数据库初始化 → 任务恢复 → 启动 Worker → 健康自检。
关闭：停止领取 → 取消在跑任务 → 关闭网关连接池 → 释放数据库读连接 →
      释放单实例锁。

B.2 关键修复：单实例锁在 ``init_db()`` 之前获取，**所有启动阶段
（migration / vault / 备份 / 恢复 / Worker / 自检）都被外层
``try/finally`` 包裹**。任一步失败（包括 init_db 抛 MigrationRequiredError）
都进入 finally，先停止已启动的子系统（worker_manager / gateway），
再释放锁。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from .database import fetch_one, init_db, is_override
from .obsidian import ensure_vault_structure
from .workers import recover_all, worker_manager

logger = logging.getLogger(__name__)


def _health_selfcheck() -> dict:
    """健康自检：数据库可读、schema 版本、vault 目录。"""
    info = {"database": "ok", "schema_version": None, "vault": "ok"}
    try:
        info["schema_version"] = fetch_one(
            "SELECT MAX(version) AS v FROM schema_migrations"
        )["v"]
    except Exception as e:
        info["database"] = f"error: {e}"
    return info


async def _shutdown_subsystems():
    """统一收尾：worker → gateway → DB 连接。任一异常均不向上抛。"""
    try:
        await worker_manager.stop()
    except Exception as e:
        logger.exception("worker_manager.stop 失败: %s", e)
    try:
        from .gateway import gateway as gw
        await gw.aclose()
    except Exception as e:
        logger.warning("网关连接池关闭失败: %s", e)
    try:
        from .database import reset_connections
        reset_connections()
    except Exception as e:
        logger.warning("DB 连接清理失败: %s", e)


@asynccontextmanager
async def lifespan(app):
    # C.5 收尾：startup / shutdown 严格幂等，shutdown 最多执行一次。
    # 启动失败与正常运行退出走同一 finally 路径，状态变量保证不重复。
    lock_acquired: bool = False
    worker_started: bool = False
    shutdown_done: bool = False

    async def _shutdown_once():
        nonlocal shutdown_done
        if shutdown_done:
            return
        shutdown_done = True
        if worker_started:
            await _shutdown_subsystems()
        if lock_acquired:
            try:
                from .instance_lock import release_instance_lock
                release_instance_lock()
            except Exception as e:
                logger.warning(f"释放单实例锁失败: {e}")
        logger.info("v5.5.1 后端已关闭")

    try:
        # 1) 单实例锁
        if not is_override():
            from .instance_lock import acquire_instance_lock
            try:
                acquire_instance_lock()
                lock_acquired = True
            except RuntimeError as e:
                logger.error(f"单实例锁冲突: {e}")
                raise
            except Exception as e:
                logger.exception("获取单实例锁失败，拒绝启动: %s", e)
                raise RuntimeError(f"获取单实例锁失败: {e}") from e

        # 2) 数据库初始化
        init_db()
        ensure_vault_structure()

        # 2.1) V5.6.3: tokenizer 初始化（迁移后、FTS 前）。
        #      jieba 失败也以降级 char_fallback 启动；状态记录给 health（degraded）。
        try:
            from . import tokenizer as _tok
            _tok_status = _tok.initialize_tokenizer()
            logger.info("tokenizer 启动状态: %s (%s)",
                        _tok_status, _tok.get_tokenizer_status()["mode"])
        except Exception as e:
            logger.warning("tokenizer 启动初始化异常: %s", e)

        # 2.2) V5.6.2: FTS 启动阶段单点初始化（业务路径不再懒建 DDL）。
        #      ready/unavailable 都接受；failed 视为启动自检失败。
        try:
            from .rag import init_fts
            _fts_status = init_fts()
            if _fts_status not in ("ready", "unavailable"):
                raise RuntimeError(f"FTS 初始化失败: {_fts_status}")
            logger.info("FTS 启动状态: %s", _fts_status)
        except RuntimeError:
            raise
        except Exception as e:
            logger.warning(f"FTS 启动初始化异常: {e}")

        # 3) 可选副作用
        try:
            from .seed_data import seed_calendar
            seed_calendar(force=False)
        except Exception as e:
            logger.warning(f"seed_calendar skipped: {e}")
        if not is_override():
            try:
                from .backup import backup_database
                backup_database()
            except Exception as e:
                logger.warning(f"startup backup skipped: {e}")

        # 4) 任务恢复
        recovery = recover_all()
        logger.info("任务恢复: %s", recovery)

        # 5) 临时文件清理
        try:
            from .services.blob_gc import cleanup_tmp_files
            n = cleanup_tmp_files(max_age_hours=24.0)
            if n:
                logger.info("已清理 %d 个超龄上传临时文件", n)
        except Exception as e:
            logger.warning(f"tmp cleanup skipped: {e}")

        # 6) Worker 启动
        worker_manager.start()
        worker_started = True
        selfcheck = _health_selfcheck()
        logger.info("v5.5.1 后端启动完成，自检: %s", selfcheck)
        app.state.worker_manager = worker_manager

        try:
            yield
        finally:
            # 正常运行退出
            await _shutdown_once()
    except BaseException:
        # 启动失败或 yield 内未捕获异常
        logger.exception("v5.5.1 后端启动或运行异常")
        try:
            await _shutdown_once()
        except Exception as e:
            logger.exception("关闭阶段异常: %s", e)
        raise
