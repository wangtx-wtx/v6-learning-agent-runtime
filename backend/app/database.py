"""
V5.5 数据库层：线程本地连接 + 版本化迁移 + 明确的写入语义。

对比 V5.4 的整改（方案 2.1-2.5）：
- 【2.1】外键修正：lessons.chapter_id REFERENCES chapters(id)（旧 schema 错指向 lessons(id)）。
- 【2.2】Schema 版本化：backend/migrations/0001..0006 按版本执行并记录 schema_migrations
  （含 checksum 校验）；不再使用运行时 ALTER 充当迁移工具；遗留库拒绝启动并要求执行
  影子迁移工具（backend/tools/migrate_database.py）。
- 【2.4】连接管理：每线程独立连接（threading.local），WAL + busy_timeout=15000；
  写事务 BEGIN IMMEDIATE，SQLITE_BUSY 最多重试 3 次；禁止跨线程共享连接。
- 【2.5】写入语义拆分：insert() 返回 lastrowid（仅 INSERT）；execute() 返回 rowcount
  （UPDATE/DELETE/DDL）；executemany() 返回影响行数。查询一律返回 dict。
- 事务上下文 transaction()：块内 execute/insert 不自行 commit，由上下文统一提交/回滚。
- 测试可注入临时库：configure_db(path) + reset_connections()。
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import DB_PATH, DATA_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 版本化迁移（方案 2.2）
# ---------------------------------------------------------------------------
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


class MigrationRequiredError(RuntimeError):
    """遗留数据库（无 schema_migrations）：必须先运行影子迁移工具。"""


class SchemaOutdatedError(RuntimeError):
    """迁移文件比已应用版本新：启动前需应用（或运行迁移工具）。"""


class SchemaTamperedError(RuntimeError):
    """已应用迁移的 checksum 与文件不符：迁移文件被改动。"""


class NotFoundError(LookupError):
    """UPDATE/DELETE 影响 0 行时的业务语义错误。"""


def _migration_files() -> list[dict]:
    """返回 [{version, name, checksum, sql}]，按版本号升序。缺失目录视为空（纯内存测试）。"""
    if not MIGRATIONS_DIR.exists():
        return []
    out = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        stem = p.stem  # e.g. 0001_baseline
        try:
            version = int(stem.split("_", 1)[0])
        except ValueError:
            raise RuntimeError(f"迁移文件名必须以版本号开头: {p.name}")
        sql = p.read_text(encoding="utf-8")
        out.append({
            "version": version,
            "name": stem,
            "checksum": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            "sql": sql,
        })
    return out


def _split_statements(sql: str) -> list[str]:
    """
    把迁移 SQL 拆成单条语句（迁移文件仅允许 DDL/DML，不允许触发器与
    字符串内分号——executescript 会隐式 COMMIT，无法用于事务化迁移）。
    """
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    cleaned = "\n".join(lines)
    return [s.strip() for s in cleaned.split(";") if s.strip()]


def _apply_migration(conn: sqlite3.Connection, mig: dict) -> None:
    """在单个事务中应用一个迁移并记录版本。失败整体回滚并向上抛出（拒绝启动）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for stmt in _split_statements(mig["sql"]):
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
            "VALUES (?, ?, ?, datetime('now','localtime'))",
            (mig["version"], mig["name"], mig["checksum"]),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def ensure_schema(conn: sqlite3.Connection) -> dict:
    """
    校验并按需应用迁移。返回报告 dict。
    - 全新库（无用户表）：按版本顺序应用全部迁移。
    - 已应用库：校验 checksum；有未应用的新版本 → 自动前向应用（lifespan 内、服务前）。
    - 遗留库（有表但无 schema_migrations）→ MigrationRequiredError（拒绝启动）。
    """
    conn.executescript(SCHEMA_MIGRATIONS_DDL)
    conn.commit()
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    applied = {
        r["version"]: dict(r) for r in
        conn.execute("SELECT * FROM schema_migrations").fetchall()
    }
    files = _migration_files()

    # checksum 校验：已应用版本不得被修改
    for mig in files:
        if mig["version"] in applied and applied[mig["version"]]["checksum"] != mig["checksum"]:
            raise SchemaTamperedError(
                f"迁移 {mig['version']}({mig['name']}) 的 checksum 与已应用记录不一致，"
                "已应用的迁移文件不允许修改"
            )

    user_tables = tables - {"schema_migrations"}
    if not user_tables and not applied:
        # 全新库
        for mig in files:
            _apply_migration(conn, mig)
        conn.commit()
        return {"mode": "fresh_init", "applied": [m["version"] for m in files]}

    if not applied:
        raise MigrationRequiredError(
            "检测到遗留数据库（无 schema_migrations 记录）。"
            "请先运行: python -m tools.migrate_database 执行影子迁移；"
            "应用拒绝在未迁移的库上启动。"
        )

    pending = [m for m in files if m["version"] not in applied]
    if pending:
        for mig in pending:
            _apply_migration(conn, mig)
        conn.commit()
        logger.info("前向迁移完成: %s", [m["version"] for m in pending])
    return {"mode": "ok", "applied_versions": sorted(applied.keys()),
            "newly_applied": [m["version"] for m in pending]}


# ---------------------------------------------------------------------------
# 连接管理（方案 2.4：查询线程本地；写入/事务独占短连接）
# ---------------------------------------------------------------------------
# 设计约束：FastAPI 异步端点全部运行在同一事件循环线程，多个协程会交错执行；
# 若协程共享同一线程连接，各自 BEGIN IMMEDIATE 会互相冲突。因此：
# - 读（fetch_*）：线程本地只读连接（WAL 下读者不阻塞）；
# - 写（execute/insert/executemany）：每次调用独占短连接，语句级原子；
# - 多步事务（transaction()）：块内独占一条短连接，统一提交/回滚。
_thread_local = threading.local()
_db_path_override: Optional[Path] = None
_override_lock = threading.Lock()


def _active_db_path() -> Path:
    with _override_lock:
        return Path(_db_path_override) if _db_path_override else Path(DB_PATH)


def configure_db(path: str | Path) -> None:
    """测试注入：切换数据库路径并清空连接池。"""
    global _db_path_override
    with _override_lock:
        _db_path_override = Path(path)
    reset_connections()


def is_override() -> bool:
    """当前是否处于测试注入路径（lifespan 据此跳过真实备份等副作用）。"""
    with _override_lock:
        return _db_path_override is not None


def reset_connections() -> None:
    """关闭当前线程的读连接（各写短连接随语句结束即关闭）。"""
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _thread_local.conn = None


def create_connection(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else _active_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=15.0)  # check_same_thread 默认 True：禁止跨线程共享
    conn.row_factory = sqlite3.Row
    # journal_mode 需要短暂排他锁：仅当尚未是 WAL 时设置，避免高并发下连接期报 locked
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "wal":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def get_connection() -> sqlite3.Connection:
    """当前线程的读连接（首次使用时建立并校验 schema 版本）。写操作请勿复用它。"""
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = create_connection()
        report = ensure_schema(conn)
        if report.get("newly_applied"):
            logger.info("schema 迁移: %s", report)
        _thread_local.conn = conn
    return conn


# ---------------------------------------------------------------------------
# 事务（独占短连接 + BEGIN IMMEDIATE + BUSY 重试）
# ---------------------------------------------------------------------------
_BUSY_RETRIES = 3


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """
    多步写入原子性：块内独占一条短连接，成功统一 COMMIT，失败 ROLLBACK 并关闭。
    支持 SQLITE_BUSY 指数退避重试（最多 3 次）。
    """
    last_err: Optional[Exception] = None
    for attempt in range(_BUSY_RETRIES):
        conn = create_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            conn.close()
            last_err = e
            if "locked" in str(e).lower() and attempt < _BUSY_RETRIES - 1:
                time.sleep(0.2 * (attempt + 1))
                continue
            raise
        try:
            yield conn
            conn.execute("COMMIT")
            return
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
    raise last_err or sqlite3.OperationalError("database busy")


# ---------------------------------------------------------------------------
# 统一数据访问（方案 2.5：语义拆分；写操作独占短连接，语句级原子）
# ---------------------------------------------------------------------------
def _write_conn() -> sqlite3.Connection:
    return create_connection()


def insert(sql: str, params: tuple = ()) -> int:
    """仅用于 INSERT。独占短连接，返回 lastrowid。"""
    conn = _write_conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def execute(sql: str, params: tuple = ()) -> int:
    """用于 UPDATE/DELETE/DDL。独占短连接，返回受影响行数（rowcount）。"""
    conn = _write_conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return int(cur.rowcount)
    finally:
        conn.close()


def executemany(sql: str, rows: list[tuple]) -> int:
    """批量执行。独占短连接，返回影响行数合计。"""
    conn = _write_conn()
    try:
        cur = conn.executemany(sql, rows)
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}


def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    rows = get_connection().execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def fetch_one(sql: str, params: tuple = ()) -> Optional[dict]:
    row = get_connection().execute(sql, params).fetchone()
    return _row_to_dict(row) if row is not None else None


def query(sql: str, params: tuple = ()) -> list[dict]:
    return fetch_all(sql, params)


def query_one(sql: str, params: tuple = ()) -> Optional[dict]:
    return fetch_one(sql, params)


def init_db() -> None:
    """建立/校验连接与 schema 版本。遗留库在此抛 MigrationRequiredError（拒绝启动）。"""
    get_connection()


def schema_version() -> int:
    rows = fetch_all("SELECT MAX(version) AS v FROM schema_migrations")
    return int(rows[0]["v"] or 0) if rows else 0


# ---------------------------------------------------------------------------
# 材料路径安全（P0，保持不变）
# ---------------------------------------------------------------------------
def resolve_material_path(material_id: int) -> tuple[str, str, str]:
    """P0 安全边界：把 material_id 解析为受控 uploads 根目录下的真实路径。"""
    from .dag import BusinessError

    row = fetch_one("SELECT file_path, type, kind FROM materials WHERE id=?", (material_id,))
    if not row:
        raise BusinessError(f"material_id={material_id} 不存在")
    file_path = row.get("file_path")
    if not file_path:
        raise BusinessError(f"material_id={material_id} 缺少 file_path")
    try:
        resolved = Path(file_path).resolve()
    except Exception as e:
        raise BusinessError(f"material_id={material_id} 路径解析失败: {e}")
    upload_root = (Path(DATA_DIR) / "uploads").resolve()
    try:
        resolved.relative_to(upload_root)
    except ValueError:
        raise BusinessError(f"material_id={material_id} 路径越权：{resolved} 不在 {upload_root} 内")
    if not resolved.exists():
        raise BusinessError(f"material_id={material_id} 文件不存在：{resolved}")
    return str(resolved), (row.get("type") or ""), (row.get("kind") or "")


def resolve_materials(material_ids: Optional[list[int]]) -> list[dict]:
    out = []
    for mid in (material_ids or []):
        path, mtype, kind = resolve_material_path(mid)
        out.append({"id": mid, "path": path, "type": mtype, "kind": kind})
    return out


def resolve_images(image_ids: Optional[list]) -> list[Any]:
    out = []
    for iid in (image_ids or []):
        if isinstance(iid, int):
            try:
                path, _, _ = resolve_material_path(iid)
                out.append(path)
            except Exception:
                out.append(iid)
        else:
            out.append(str(iid))
    return out
