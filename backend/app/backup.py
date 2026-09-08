"""
SQLite 自动备份：每次启动时生成时间戳备份。
仅保留最近 N=7 个备份，避免无限增长。

V5.2:使用 SQLite 原生 Connection.backup() API(原子、在线、不阻塞数据库)。
"""
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import DB_PATH, DATA_DIR

logger = logging.getLogger(__name__)
BACKUP_DIR_NAME = "backups"
BACKUP_KEEP = 7


def backup_database(db_path: Optional[Path] = None) -> Optional[Path]:
    """使用 SQLite backup API 备份数据库到 data/backups/ 目录。
    返回备份文件路径，失败返回 None。
    """
    db_path = Path(db_path or DB_PATH)
    if not db_path.exists():
        logger.info(f"数据库不存在，跳过备份: {db_path}")
        return None

    backup_dir = Path(DATA_DIR) / BACKUP_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"v5_{timestamp}.db"

    src = None
    dst = None
    try:
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(backup_path))
        src.backup(dst)
        dst.commit()
        logger.info(f"数据库已备份到: {backup_path}")
    except Exception as e:
        logger.error(f"数据库备份失败: {e}")
        return None
    finally:
        try:
            if dst is not None:
                dst.close()
        except Exception:
            pass
        try:
            if src is not None:
                src.close()
        except Exception:
            pass

    # 清理旧备份，保留最近 N 个
    backups = sorted(backup_dir.glob("v5_*.db"), key=lambda p: p.name, reverse=True)
    for old in backups[BACKUP_KEEP:]:
        try:
            old.unlink()
            logger.info(f"已清理过期备份: {old.name}")
        except Exception:
            pass

    return backup_path


def list_backups() -> list[dict]:
    """列出所有备份文件"""
    backup_dir = Path(DATA_DIR) / BACKUP_DIR_NAME
    if not backup_dir.exists():
        return []
    result = []
    for f in sorted(backup_dir.glob("v5_*.db"), key=lambda p: p.name, reverse=True):
        result.append({
            "name": f.name,
            "path": str(f),
            "size_bytes": f.stat().st_size,
            "created_at": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        })
    return result
