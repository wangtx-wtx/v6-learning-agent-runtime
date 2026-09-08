"""
V5.5 真影子迁移（方案 2.3）。

流程：
  v5.db
    → SQLite 在线备份到 pre_migration.db
    → 创建 v5.shadow.db，应用完整目标 Schema（版本化迁移 0001..000N）
    → 分表复制（列交集，保留 id；materials 按 sha256 回填 file_blobs）
    → 校验：行数一致 / 材料文件存在 / 关键外键关系 / integrity_check / foreign_key_check
    → 全部通过：原子替换（旧库改名 v5.pre_v55.db，shadow 顶替 v5.db）
    → 失败：不替换原库，保留 shadow 供诊断，输出 JSON 迁移报告

必须在应用停止时运行；禁止在应用对外提供 API 后执行结构迁移。
用法：python -m tools.migrate_database [--db PATH] [--dry-run]
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .config import DB_PATH, DATA_DIR

# 复制顺序：无关紧要（复制时关闭外键，最后统一 foreign_key_check），
# 但按依赖序排列便于人工排查。
COPY_TABLES = [
    "courses", "chapters", "lessons", "homeworks", "materials", "source_chunks",
    "notes", "questions", "answer_items", "errors", "reviews", "review_attempts",
    "workflow_runs", "run_nodes", "run_tasks", "parse_tasks", "sync_jobs",
    "graph_nodes", "graph_edges", "academic_calendar", "evidence_links",
]

# 缺失即致命的核心表；其余表缺失时记 0 行并继续（极简旧库容错）
REQUIRED_SOURCE_TABLES = {
    "courses", "chapters", "lessons", "materials", "source_chunks", "workflow_runs", "run_nodes",
}


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()]


def _qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sha256_file(path: str) -> Optional[str]:
    try:
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _backfill_file_blobs(shadow: sqlite3.Connection, report: dict) -> None:
    """把历史 materials 按 sha256 去重回填到 file_blobs，并写 blob_id / ref_count。
    缺失 sha256 的旧记录从物理文件现算（文件存在性已由校验环节把关）。"""
    rows = shadow.execute(
        "SELECT id, sha256, file_hash, file_path, size_bytes, mime FROM materials ORDER BY id"
    ).fetchall()
    blob_ids: dict[str, int] = {}
    computed = 0
    for r in rows:
        sha = (r["sha256"] or r["file_hash"] or "").strip()
        if not sha and r["file_path"] and Path(r["file_path"]).exists():
            sha = _sha256_file(r["file_path"]) or ""
            if sha:
                computed += 1
                shadow.execute("UPDATE materials SET sha256=? WHERE id=?", (sha, r["id"]))
        if not sha:
            continue  # 无法建立 blob 关联的旧记录保持 NULL（GC 不会动它）
        if sha not in blob_ids:
            cur = shadow.execute(
                "INSERT INTO file_blobs (sha256, storage_path, size_bytes, mime, ref_count) "
                "VALUES (?,?,?,?,0)",
                (sha, r["file_path"] or "", r["size_bytes"] or 0, r["mime"]),
            )
            blob_ids[sha] = int(cur.lastrowid)
        shadow.execute(
            "UPDATE file_blobs SET ref_count = ref_count + 1 WHERE id=?", (blob_ids[sha],)
        )
        shadow.execute("UPDATE materials SET blob_id=? WHERE id=?", (blob_ids[sha], r["id"]))
    report["file_blobs_created"] = len(blob_ids)
    report["materials_linked_to_blob"] = len(blob_ids) and sum(
        1 for r in rows if ((r["sha256"] or r["file_hash"] or "").strip() or r["file_path"])
    )
    report["sha256_computed_from_files"] = computed


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> int:
    """列交集复制，保留主键 id。返回复制行数。"""
    src_cols = _table_columns(src, table)
    dst_cols = _table_columns(dst, table)
    if not src_cols:
        raise RuntimeError(f"源库缺少表 {table}")
    common = [c for c in dst_cols if c in src_cols]
    if not common:
        raise RuntimeError(f"表 {table} 源/目标无共同列")
    collist = ", ".join(_qident(c) for c in common)
    n = 0
    batch = 500
    max_rowid = 0
    while True:
        rows = src.execute(
            f"SELECT {collist} FROM {_qident(table)} ORDER BY id LIMIT ? OFFSET ?", (batch, n)
        ).fetchall()
        if not rows:
            break
        ph = ",".join("?" for _ in common)
        dst.executemany(
            f"INSERT INTO {_qident(table)} ({collist}) VALUES ({ph})",
            [tuple(r) for r in rows],
        )
        n += len(rows)
        max_rowid = rows[-1]["id"]
    if n:
        # 让 AUTOINCREMENT 续号
        dst.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
        dst.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?,?)", (table, max_rowid))
    return n


def _dedup_key(row: sqlite3.Row, keys: list[str]) -> tuple:
    return tuple((row[k] if row[k] is not None else "\x00NULL") for k in keys)


def _copy_table_dedup(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    dedup_keys: list[str],
    null_out_for_dups: Optional[list[str]] = None,
) -> tuple[int, dict, int]:
    """
    带去重复制：同一 dedup_keys 组保留最小 id，其余行删除（或按 null_out_for_dups
    置空冲突列后保留）。返回 (复制行数, 被去重id→保留id 重映射表, 去重行数)。
    """
    src_cols = _table_columns(src, table)
    dst_cols = _table_columns(dst, table)
    common = [c for c in dst_cols if c in src_cols]
    collist = ", ".join(_qident(c) for c in common)
    rows = src.execute(f"SELECT {collist} FROM {_qident(table)} ORDER BY id").fetchall()
    groups: dict[tuple, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(_dedup_key(r, dedup_keys), []).append(r)
    remap: dict[int, int] = {}
    dup_neutralized = 0
    removed = 0
    n = 0
    max_rowid = 0
    for key, grp in groups.items():
        keep = grp[0]
        dups = grp[1:]
        if dups and null_out_for_dups:
            # 冲突列置空后仍保留行（例如 materials.file_hash 重复 → 置空但保留材料）
            dst.execute(
                f"INSERT INTO {_qident(table)} ({collist}) VALUES ({','.join('?' for _ in common)})",
                tuple(keep[c] for c in common),
            )
            n += 1
            max_rowid = max(max_rowid, keep["id"])
            for dup in dups:
                vals = []
                for c in common:
                    v = dup[c]
                    if c in null_out_for_dups:
                        v = None
                        dup_neutralized += 1
                    vals.append(v)
                dst.execute(
                    f"INSERT INTO {_qident(table)} ({collist}) VALUES ({','.join('?' for _ in common)})",
                    tuple(vals),
                )
                n += 1
                max_rowid = max(max_rowid, dup["id"])
        elif dups:
            # 整行去重：保留首行，其余删除并记录重映射
            dst.execute(
                f"INSERT INTO {_qident(table)} ({collist}) VALUES ({','.join('?' for _ in common)})",
                tuple(keep[c] for c in common),
            )
            n += 1
            max_rowid = max(max_rowid, keep["id"])
            for dup in dups:
                remap[dup["id"]] = keep["id"]
                removed += 1
        else:
            dst.execute(
                f"INSERT INTO {_qident(table)} ({collist}) VALUES ({','.join('?' for _ in common)})",
                tuple(keep[c] for c in common),
            )
            n += 1
            max_rowid = max(max_rowid, keep["id"])
    if n:
        dst.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
        dst.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?,?)", (table, max_rowid))
    # removed：整行删除数（用于行数守恒）；dup_neutralized：仅冲突列被置空（行仍保留）
    return n, remap, removed


def _apply_remap(
    dst: sqlite3.Connection, table: str, column: str, remap: dict[int, int]
) -> int:
    """把子表中被去重 id 的引用改写为保留 id。返回改写行数。"""
    if not remap:
        return 0
    touched = 0
    for old, new in remap.items():
        cur = dst.execute(
            f"UPDATE {_qident(table)} SET {_qident(column)}=? WHERE {_qident(column)}=?",
            (new, old),
        )
        touched += int(cur.rowcount or 0)
    return touched


def shadow_migrate(db_path: str | Path | None = None, dry_run: bool = False) -> dict:
    """
    执行影子迁移。返回 JSON 兼容报告；失败时抛出异常且绝不替换原库。
    """
    target = Path(db_path) if db_path else Path(DB_PATH)
    data_dir = target.parent
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    pre_migration = backup_dir / "pre_migration.db"
    pre_archive = target.with_name(target.stem + ".pre_v55.db")
    shadow_path = data_dir / "v5.shadow.db"

    report: dict = {
        "tool": "v55-shadow-migrate",
        "created_at": ts,
        "source_db": str(target),
        "pre_migration_backup": str(pre_migration),
        "archive_db": str(pre_archive),
        "shadow_db": str(shadow_path),
        "dry_run": dry_run,
        "copied": {},
        "file_blobs_created": 0,
        "materials_linked_to_blob": 0,
        "validations": {},
        "replaced": False,
        "success": False,
    }

    for p in (shadow_path,):
        if p.exists():
            p.unlink()  # 上次失败残留的 shadow 直接重建

    src: Optional[sqlite3.Connection] = None
    shadow: Optional[sqlite3.Connection] = None
    try:
        # 1) 在线备份（第一道保险）
        src = sqlite3.connect(str(target), timeout=30.0)
        src.row_factory = sqlite3.Row
        bak = sqlite3.connect(str(pre_migration))
        src.backup(bak)
        bak.close()
        try:
            src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

        # 2) 构建 shadow：完整目标 Schema（fresh 路径走版本化迁移）
        from .database import create_connection, ensure_schema, _migration_files

        shadow = create_connection(shadow_path)
        # 复制期间关闭外键强制（最后统一校验），避免复制顺序问题
        shadow.execute("PRAGMA foreign_keys=OFF")
        schema_report = ensure_schema(shadow)
        report["schema"] = schema_report
        report["migrations_applied"] = [m["version"] for m in _migration_files()]

        # 3) 分表复制（含遗留数据清理：唯一键去重 + 子表引用重映射）
        src_tables = {
            r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        src_tables = {t for t in src_tables if not t.startswith("chunks_fts")}

        chapter_remap: dict[int, int] = {}
        course_remap: dict[int, int] = {}
        for table in COPY_TABLES:
            if table not in src_tables:
                if table not in REQUIRED_SOURCE_TABLES:
                    report["copied"][table] = {"source": 0, "copied": 0, "deduped": 0,
                                               "missing_in_source": True}
                    continue
                raise RuntimeError(f"源库缺少必需表 {table}")
            src_n = _count(src, table)
            if table == "chapters":
                n, chapter_remap, dups = _copy_table_dedup(
                    src, shadow, "chapters", ["course_id", "chapter_no"]
                )
            elif table == "courses":
                n, course_remap, dups = _copy_table_dedup(
                    src, shadow, "courses", ["name", "semester"]
                )
            elif table == "materials":
                # 同一 file_hash 的旧材料保留首行值，其余置空（V5.5 blob 模型下
                # file_hash 唯一性不再由 materials 承担）
                n, _, dups = _copy_table_dedup(
                    src, shadow, "materials", ["file_hash"], null_out_for_dups=["file_hash"]
                ) if _count(src, "materials") else (_count(src, "materials"), {}, 0)
            else:
                n = _copy_table(src, shadow, table)
                dups = 0
            report["copied"][table] = {"source": src_n, "copied": n, "deduped": dups}

        # 子表引用重映射（先高层级后低层级，链式 remap：dup→keep 可能又是指向更深 dup 的 keep）
        def _chain(mapping: dict[int, int]) -> dict[int, int]:
            out = {}
            for old, new in mapping.items():
                while new in mapping:
                    new = mapping[new]
                out[old] = new
            return out

        chapter_remap = _chain(chapter_remap)
        course_remap = _chain(course_remap)
        remap_stats = {}
        for t in ("lessons", "materials", "source_chunks", "notes", "errors", "reviews", "homeworks"):
            if t in report["copied"] and chapter_remap:
                remap_stats[f"{t}.chapter_id"] = _apply_remap(shadow, t, "chapter_id", chapter_remap)
        for t in ("chapters", "lessons", "materials", "source_chunks", "notes", "errors",
                  "reviews", "homeworks", "workflow_runs", "graph_nodes", "academic_calendar"):
            if t in report["copied"] and course_remap:
                remap_stats[f"{t}.course_id"] = _apply_remap(shadow, t, "course_id", course_remap)
        report["remapped_refs"] = remap_stats
        shadow.commit()

        # 4) V5.5 语义回填：materials → file_blobs
        _backfill_file_blobs(shadow, report)
        shadow.commit()

        # 5) 校验
        checks: dict = {}

        # 5.1 行数守恒：copied + deduped == source
        count_mismatch = {}
        for table, st in report["copied"].items():
            if st["source"] and (st["copied"] + st["deduped"] != st["source"]):
                count_mismatch[table] = st
        checks["row_counts_conserved"] = not count_mismatch
        if count_mismatch:
            checks["row_count_mismatch"] = count_mismatch

        # 5.2 材料物理文件存在
        missing_files = [
            r["id"] for r in shadow.execute(
                "SELECT id, file_path FROM materials WHERE file_path IS NOT NULL AND file_path != ''"
            ).fetchall()
            if not Path(r["file_path"]).exists()
        ]
        checks["material_files_exist"] = not missing_files
        if missing_files:
            checks["missing_material_files"] = missing_files[:50]

        # 5.3 关键引用关系
        orphan_run_nodes = shadow.execute(
            "SELECT COUNT(*) FROM run_nodes r LEFT JOIN workflow_runs w ON w.id=r.run_id "
            "WHERE w.id IS NULL"
        ).fetchone()[0]
        orphan_answer_items = shadow.execute(
            "SELECT COUNT(*) FROM answer_items a LEFT JOIN questions q ON q.id=a.question_id "
            "WHERE q.id IS NULL"
        ).fetchone()[0]
        invalid_evidence = shadow.execute(
            "SELECT COUNT(*) FROM evidence_links e "
            "LEFT JOIN source_chunks c ON c.id=e.chunk_id "
            "WHERE e.chunk_id IS NOT NULL AND c.id IS NULL"
        ).fetchone()[0]
        bad_notes = shadow.execute(
            "SELECT COUNT(*) FROM notes n LEFT JOIN courses c ON c.id=n.course_id "
            "LEFT JOIN chapters ch ON ch.id=n.chapter_id LEFT JOIN lessons l ON l.id=n.lesson_id "
            "WHERE (n.course_id IS NOT NULL AND c.id IS NULL) "
            "OR (n.chapter_id IS NOT NULL AND ch.id IS NULL) "
            "OR (n.lesson_id IS NOT NULL AND l.id IS NULL)"
        ).fetchone()[0]
        checks["run_nodes_run_valid"] = orphan_run_nodes == 0
        checks["answer_items_question_valid"] = orphan_answer_items == 0
        checks["evidence_chunk_valid"] = invalid_evidence == 0
        checks["notes_refs_valid"] = bad_notes == 0
        checks["orphans"] = {
            "run_nodes": orphan_run_nodes,
            "answer_items": orphan_answer_items,
            "evidence_links": invalid_evidence,
            "notes": bad_notes,
        }

        # 5.4 全库完整性
        shadow.execute("PRAGMA foreign_key_check")  # 先刷新连接状态
        fk_violations = shadow.execute("PRAGMA foreign_key_check").fetchall()
        checks["foreign_key_check_empty"] = len(fk_violations) == 0
        checks["foreign_key_violations"] = [tuple(v) for v in fk_violations[:50]]
        checks["integrity_check"] = shadow.execute("PRAGMA integrity_check").fetchone()[0]

        report["validations"] = checks
        ok = (
            checks["row_counts_conserved"]
            and checks["material_files_exist"]
            and checks["run_nodes_run_valid"]
            and checks["answer_items_question_valid"]
            and checks["evidence_chunk_valid"]
            and checks["notes_refs_valid"]
            and checks["foreign_key_check_empty"]
            and checks["integrity_check"] == "ok"
        )
        report["success"] = ok
        if not ok:
            _write_report(report, backup_dir, ts)
            raise RuntimeError(f"影子迁移校验失败：{json.dumps(checks, ensure_ascii=False)[:800]}")

        if dry_run:
            report["note"] = "dry-run：shadow 已生成并通过校验，未替换原库"
            return report

        # 6) 原子替换（关闭全部连接后）
        shadow.commit()
        shadow.close()
        shadow = None
        src.close()
        src = None

        if pre_archive.exists():
            pre_archive.unlink()
        os.replace(str(target), str(pre_archive))          # 旧库归档
        for suffix in ("-wal", "-shm"):
            stale = target.with_name(target.name + suffix)
            if stale.exists():
                stale.unlink()                              # 旧库 WAL 残留必须清除
        os.replace(str(shadow_path), str(target))           # shadow 顶替
        report["replaced"] = True

        # 7) 重建 FTS（派生索引不入 schema_migrations）
        try:
            from .rag import rebuild_fts_all
            newconn = create_connection(target)
            report["fts_rebuild"] = rebuild_fts_all(newconn)
            newconn.close()
        except Exception as e:  # FTS 属派生数据，失败不回滚迁移
            report["fts_rebuild"] = f"deferred: {e}"

        _write_report(report, backup_dir, ts)
        return report

    except Exception as e:
        report["success"] = False
        report["error"] = str(e)
        if shadow is not None:
            try:
                shadow.close()
            except Exception:
                pass
        if src is not None:
            try:
                src.close()
            except Exception:
                pass
        _write_report(report, backup_dir, ts)
        raise
    finally:
        if src is not None:
            src.close()
        if shadow is not None:
            shadow.close()


def _write_report(report: dict, backup_dir: Path, ts: str) -> None:
    path = backup_dir / f"migration_report_{ts}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report.setdefault("report_path", str(path))


def main() -> int:  # pragma: no cover - CLI 入口
    import argparse
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="V5.5 影子迁移工具")
    parser.add_argument("--db", default=None, help="目标数据库路径（默认 backend/data/v5.db）")
    parser.add_argument("--dry-run", action="store_true", help="只构建 shadow 并校验，不替换")
    args = parser.parse_args()
    try:
        report = shadow_migrate(args.db, dry_run=args.dry_run)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
