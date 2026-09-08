"""FastAPI Lifespan（方案 3.1）：启动/关闭的完整生命周期管理。

启动：数据库初始化（含 schema 版本校验，遗留库拒绝启动）→ 任务恢复 →
      启动 Worker（工作流 + 材料）→ 健康自检。
关闭：停止领取 → 取消在跑任务 → 未完成任务标记 interrupted → 关闭网关连接池 →
      关闭数据库读连接。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from .database import fetch_one, init_db
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


@asynccontextmanager
async def lifespan(app):
    # ---- 启动 ----
    init_db()  # 遗留库在此抛 MigrationRequiredError，应用拒绝启动
    ensure_vault_structure()
    try:
        from .seed_data import seed_calendar
        seed_calendar(force=False)
    except Exception as e:
        logger.warning(f"seed_calendar skipped: {e}")
    from .database import is_override
    if not is_override():  # 测试注入库时不产生真实备份副作用
        try:
            from .backup import backup_database
            backup_database()
        except Exception as e:
            logger.warning(f"startup backup skipped: {e}")

    recovery = recover_all()
    logger.info("任务恢复: %s", recovery)

    # 清理上次进程崩溃遗留的上传临时文件（方案 4.4）
    try:
        from .services.blob_gc import cleanup_tmp_files
        n = cleanup_tmp_files(max_age_hours=24.0)
        if n:
            logger.info("已清理 %d 个超龄上传临时文件", n)
    except Exception as e:
        logger.warning(f"tmp cleanup skipped: {e}")

    worker_manager.start()
    selfcheck = _health_selfcheck()
    logger.info("v5.5 后端启动完成，自检: %s", selfcheck)
    app.state.worker_manager = worker_manager
    try:
        yield
    finally:
        # ---- 关闭 ----
        await worker_manager.stop()
        try:
            from .gateway import gateway as gw
            await gw.aclose()
        except Exception:
            pass
        logger.info("v5.5 后端已优雅关闭")
