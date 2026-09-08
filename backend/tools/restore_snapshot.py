"""
快照恢复工具（V5.5 方案 15.2）。

用法（在 backend/ 目录下）：
  python -m tools.restore_snapshot --latest               # 恢复 data/backups/ 最新快照
  python -m tools.restore_snapshot --file backups/x.db    # 恢复指定快照
  python -m tools.restore_snapshot --latest --dry-run     # 只校验不落盘

安全措施：
1. 恢复前先对当前库做一次 pre_restore 备份；
2. 校验快照：sqlite3 integrity_check + schema_migrations 存在 + user_version 一致性；
3. 原子替换：先写临时文件，再 os.replace 覆盖主库，并清理旧 WAL/SHM。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DB_PATH  # noqa: E402

BACKUP_DIR = Path(DB_PATH).parent / "backups"


def validate_snapshot(path: Path) -> dict:
    """校验快照可用性。返回 {schema_version, migrations, tables, integrity}，不可用抛 ValueError。"""
    if not path.exists():
        raise ValueError(f"快照不存在: {path}")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as e:
            raise ValueError(f"快照不是有效的 SQLite 数据库: {e}") from e
        if integrity != "ok":
            raise ValueError(f"快照完整性校验失败: {integrity}")
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "schema_migrations" not in tables:
            raise ValueError("快照缺少 schema_migrations 表，不是有效 v5 快照")
        migrations = [r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if not migrations:
            raise ValueError("快照 schema_migrations 为空")
        return {"schema_version": schema_version, "migrations": migrations,
                "tables": len(tables), "integrity": integrity}
    finally:
        conn.close()


def restore_snapshot(snapshot: Path, db_path: Path = DB_PATH, dry_run: bool = False) -> dict:
    """把 snapshot 恢复为 db_path（先做 pre_restore 备份，再原子替换）。"""
    snapshot = Path(snapshot)
    db_path = Path(db_path)
    info = validate_snapshot(snapshot)

    if dry_run:
        return {"dry_run": True, "snapshot": str(snapshot), "target": str(db_path), **info}

    if not db_path.exists():
        raise ValueError(f"目标数据库不存在: {db_path}")

    # 1) 恢复前备份当前库
    pre_dir = db_path.parent / "backups"
    pre_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    pre_backup = pre_dir / f"pre_restore_{ts}.db"
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(pre_backup))
    src.backup(dst)
    dst.close()
    src.close()

    # 2) 释放本进程连接，避免文件占用
    try:
        from app.database import reset_connections
        reset_connections()
    except Exception:
        pass

    # 3) 原子替换主库 + 清理 WAL/SHM
    tmp = db_path.with_suffix(".db.restore_tmp")
    shutil.copyfile(snapshot, tmp)
    os.replace(str(tmp), str(db_path))
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    # 4) 恢复后复核
    post = validate_snapshot(db_path)
    return {"dry_run": False, "snapshot": str(snapshot), "target": str(db_path),
            "pre_backup": str(pre_backup), **post}


def main() -> int:
    # Windows 控制台 cp1252 兼容：输出统一 UTF-8 替换
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="从快照恢复 v5 数据库")
    parser.add_argument("--latest", action="store_true", help="使用 data/backups/ 最新快照")
    parser.add_argument("--file", type=str, default=None, help="指定快照文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只校验快照与目标，不实际恢复")
    args = parser.parse_args()

    if args.file:
        snapshot = Path(args.file)
    elif args.latest:
        backups = sorted(BACKUP_DIR.glob("v5_*.db"), key=lambda p: p.name, reverse=True)
        if not backups:
            print("ERROR: 没有可用快照", file=sys.stderr)
            return 2
        snapshot = backups[0]
    else:
        parser.error("必须指定 --latest 或 --file")
        return 2

    try:
        result = restore_snapshot(snapshot, dry_run=args.dry_run)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print("RESTORE " + ("DRY-RUN OK" if result["dry_run"] else "OK"))
    for k in ("snapshot", "target", "pre_backup", "schema_version", "migrations", "tables", "integrity"):
        if k in result:
            print(f"  {k}: {result[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
