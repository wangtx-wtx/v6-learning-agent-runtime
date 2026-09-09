"""快照恢复工具（V5.5.1 收尾 C.1/C.3/C.9/C.10）。

用法（在 backend/ 目录下）：
  python -m tools.restore_snapshot --latest                          # 恢复 data/backups/ 最新快照
  python -m tools.restore_snapshot --file backups/x.db             # 恢复指定快照
  python -m tools.restore_snapshot --latest --dry-run              # 只校验不落盘（绝不修改目标库）
  python -m tools.restore_snapshot --file v7.db --upgrade-to 8     # 旧 v7 备份升级到 v8 再恢复
  python -m tools.restore_snapshot --file v7.db --upgrade-to 8 --dry-run  # 升级 + 恢复全程不落盘

安全保证（C.1 / C.10）：
- 任何 --dry-run 命令绝不修改目标库，绝不执行 os.replace；
- dry_run=True 时不创建永久升级副本，临时文件 try/finally 保证清理；
- 任何异常（checksum 失败 / 校验失败 / 异常退出）都用 try/finally 删临时文件。
- 升级使用 ensure_schema(conn) 走正式迁移入口，已应用 migration 的 checksum
  会被严格校验（C.3）。
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DB_PATH  # noqa: E402

BACKUP_DIR = Path(DB_PATH).parent / "backups"


def _read_user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _integrity_check(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()


def validate_snapshot(path: Path) -> dict:
    """校验快照可用性。

    V5.5.1 收尾 C.3：要求 ``PRAGMA user_version == MAX(schema_migrations.version)``。
    不一致视为不合法快照（旧 v7 备份会触发此断言；用 --upgrade-to 升级）。
    """
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
        if not migrations:
            raise ValueError("快照 schema_migrations 为空")
        max_version = int(migrations[-1])
        schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if schema_version != max_version:
            raise ValueError(
                f"快照 PRAGMA user_version={schema_version} 与 "
                f"schema_migrations MAX(version)={max_version} 不一致；"
                f"若是旧版本备份（v{max_version} 之前 user_version 未启用），"
                f"请使用 --upgrade-to {max_version} 先升级。"
            )
        return {"schema_version": schema_version, "migrations": migrations,
                "tables": len(tables), "integrity": integrity}
    finally:
        conn.close()


def upgrade_snapshot_user_version(src: Path, target_version: int,
                                   dry_run: bool = False,
                                   overwrite: bool = False) -> dict:
    """C.3: 把旧 v7 备份（user_version=0）升级到 v8（user_version=8）。

    使用 ``app.database.ensure_schema(conn)`` 走正式迁移入口，自动：
    - 校验已应用 migration 的 checksum（C.3）；
    - 应用 pending migrations；
    - 同步 user_version。

    C.10 临时文件管理：
    - 整个流程 try/finally 包裹，异常 / dry_run 都会删临时文件；
    - 非 dry_run 模式下生成 ``<src>.upgraded_to_v<N>`` 永久副本；
    - overwrite=False 时若永久副本已存在则拒绝（避免覆盖）。
    """
    if not src.exists():
        raise ValueError(f"快照不存在: {src}")
    out_path = src.parent / f"{src.stem}.upgraded_to_v{target_version}{src.suffix}"
    if not dry_run and out_path.exists() and not overwrite:
        raise FileExistsError(
            f"升级副本已存在: {out_path}；显式传 --overwrite 才覆盖。"
        )

    tmpdir = Path(tempfile.gettempdir()) / "v5_restore_upgrade"
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmpdir / f"upgrade_{src.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.db"

    try:
        shutil.copyfile(src, tmp_path)

        # C.3: 走 ensure_schema 正式入口（自动 checksum 校验 + pending apply + user_version 同步）
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from app.database import ensure_schema
        conn = sqlite3.connect(str(tmp_path), timeout=15.0)
        conn.row_factory = sqlite3.Row  # ensure_schema 内部用 r["version"] 字典式访问
        try:
            ensure_schema(conn)  # 内含 sync_user_version → user_version=MAX(migrations)
            # 强制对齐到 target_version（如果与 schema_migrations 一致就无操作）
            conn.execute(f"PRAGMA user_version={target_version}")
            conn.commit()
        finally:
            conn.close()

        final = validate_snapshot(tmp_path)
        if final["schema_version"] != target_version:
            raise RuntimeError(
                f"升级失败：期望 user_version={target_version}，"
                f"实际 {final['schema_version']}"
            )

        if dry_run:
            # C.10: dry_run 不写永久副本
            return {"dry_run": True, "upgraded": None, "src": str(src), **final}

        if out_path.exists():
            out_path.unlink()
        shutil.copyfile(tmp_path, out_path)
        return {"dry_run": False, "upgraded": str(out_path), "src": str(src), **final}
    finally:
        # C.10: 任何路径（异常 / 成功 / dry_run）都清理临时文件
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def restore_snapshot(snapshot: Path, db_path: Path = DB_PATH,
                     dry_run: bool = False,
                     upgrade_to: int | None = None,
                     overwrite: bool = False) -> dict:
    """把 snapshot 恢复为 db_path（先做 pre_restore 备份，再原子替换）。

    C.1: dry_run=True 时不创建 pre_restore 备份、不执行 os.replace；
    只返回 ``validate_snapshot`` 的报告。
    """
    snapshot = Path(snapshot)
    db_path = Path(db_path)
    info = validate_snapshot(snapshot)

    if dry_run:
        return {"dry_run": True, "snapshot": str(snapshot), "target": str(db_path),
                "upgrade_to": upgrade_to, **info}

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

    # 2) 释放本进程连接
    try:
        from app.database import reset_connections
        reset_connections()
    except Exception:
        pass

    # 3) 原子替换主库
    tmp = db_path.with_suffix(".db.restore_tmp")
    shutil.copyfile(snapshot, tmp)
    os.replace(str(tmp), str(db_path))
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    # 4) 恢复后复核
    post = validate_snapshot(db_path)
    upgrade_note = None
    if upgrade_to is not None and post["schema_version"] != upgrade_to:
        upgrade_note = (
            f"WARN: 升级目标 user_version={upgrade_to}，"
            f"实际 {post['schema_version']}。请人工检查。"
        )
    return {"dry_run": False, "snapshot": str(snapshot), "target": str(db_path),
            "pre_backup": str(pre_backup), "upgrade_to": upgrade_to, **post,
            "upgrade_note": upgrade_note}


def restore_with_upgrade(snapshot: Path, db_path: Path = DB_PATH,
                          target_version: int = 8,
                          dry_run: bool = False,
                          overwrite: bool = False) -> dict:
    """C.1: 升级与恢复都接受 dry_run；非 dry_run 时才落永久文件 / 替换目标库。

    dry_run=True 时：
    - upgrade 阶段不创建永久副本（只保留临时副本到 validate 结束）；
    - restore 阶段不创建 pre_restore 备份、不执行 os.replace。
    """
    snapshot = Path(snapshot)
    try:
        validate_snapshot(snapshot)
        return restore_snapshot(snapshot, db_path=db_path, dry_run=dry_run)
    except ValueError as e:
        if "user_version" not in str(e) and "升级" not in str(e):
            raise
        upgrade_info = upgrade_snapshot_user_version(
            snapshot, target_version, dry_run=dry_run, overwrite=overwrite
        )
        if dry_run:
            # dry_run 模式下 restore 阶段也只校验
            return {
                "dry_run": True,
                "snapshot": str(snapshot),
                "target": str(db_path),
                "upgrade_to": target_version,
                "upgrade_info": upgrade_info,
            }
        upgraded_path = Path(upgrade_info["upgraded"])
        return restore_snapshot(upgraded_path, db_path=db_path,
                                upgrade_to=target_version, overwrite=overwrite)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="从快照恢复 v5 数据库")
    parser.add_argument("--latest", action="store_true", help="使用 data/backups/ 最新快照")
    parser.add_argument("--file", type=str, default=None, help="指定快照文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只校验快照与目标，不实际恢复")
    parser.add_argument("--upgrade-to", type=int, default=None,
                        help="旧快照升级目标 user_version（典型值 8，整数）")
    parser.add_argument("--upgrade-only", action="store_true",
                        help="只升级快照，不做 restore")
    parser.add_argument("--overwrite", action="store_true",
                        help="允许覆盖已存在的升级副本")
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

    target = args.upgrade_to or 8
    try:
        if args.upgrade_only:
            result = upgrade_snapshot_user_version(
                snapshot, target, dry_run=args.dry_run, overwrite=args.overwrite
            )
        elif args.upgrade_to is not None:
            result = restore_with_upgrade(
                snapshot, target_version=target,
                dry_run=args.dry_run, overwrite=args.overwrite
            )
        else:
            result = restore_snapshot(snapshot, dry_run=args.dry_run)
    except (ValueError, FileExistsError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print("RESTORE " + ("DRY-RUN OK" if result.get("dry_run") else "OK"))
    for k in ("snapshot", "target", "pre_backup", "schema_version", "migrations",
              "tables", "integrity", "upgraded", "upgrade_to", "upgrade_note"):
        if k in result and result[k] is not None:
            print(f"  {k}: {result[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
