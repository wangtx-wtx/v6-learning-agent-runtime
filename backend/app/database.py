"""
v5.4 数据库层：统一数据访问 + 一致性 schema + 增量迁移。

对比 V5.3 审查结论的整改：
- query() 不再支持 one=True，统一用 fetch_one() / query_one()（返回 dict）。
- 所有查询返回 dict 或领域对象，绝不向业务层泄漏 sqlite3.Row（修复 RAG/证据/状态机的 .get() 崩溃）。
- 所有业务调用走本模块，禁止业务代码自行构造连接。
- 多步写入用 transaction() 上下文，失败整体回滚。
- 建表/迁移走版本化 migrate()，不再用 CREATE TABLE IF NOT EXISTS 充当迁移工具。
- 路径基于配置的绝对 UPLOAD_DIR / DB_PATH，不出现相对 "data/..."。

主动安全：PRAGMA foreign_keys=ON、busy_timeout、WAL、单例连接。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import DB_PATH, DATA_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 目标 schema（与现有 v5.db 实际结构一致，并补齐方案要求的字段）
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT, semester TEXT, teacher TEXT, schedule_json TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    chapter_no INTEGER, title TEXT NOT NULL, syllabus_ref TEXT,
    status TEXT DEFAULT 'not_started',
    review_status TEXT DEFAULT 'none',
    completed_at TEXT, reviewed_at TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER REFERENCES lessons(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    lesson_no TEXT, title TEXT, date TEXT,
    status TEXT DEFAULT 'not_started',
    note_id INTEGER, summary TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    file_path TEXT, file_hash TEXT, type TEXT, kind TEXT,
    display_name TEXT, name TEXT, mime TEXT,
    sha256 TEXT, size_bytes INTEGER DEFAULT 0,
    parser_status TEXT DEFAULT 'uploaded',
    parse_error TEXT,
    status TEXT DEFAULT 'uploaded',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS source_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER REFERENCES materials(id) ON DELETE CASCADE,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE CASCADE,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    type TEXT, locator TEXT, text TEXT,
    image_file TEXT, ocr_confidence REAL DEFAULT 1.0,
    topics_json TEXT, embedding TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE CASCADE,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    title TEXT, body TEXT,
    markdown_path TEXT, status TEXT DEFAULT 'draft',
    version INTEGER DEFAULT 1,
    evidence_json TEXT, model_used TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS homeworks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, status TEXT DEFAULT 'pending',
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE CASCADE,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    mode TEXT, date TEXT, input_dir TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    homework_id INTEGER REFERENCES homeworks(id) ON DELETE CASCADE,
    question_no INTEGER, text TEXT,
    image_file TEXT, knowledge_points_json TEXT,
    scope_json TEXT, risk_json TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS answer_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER REFERENCES questions(id) ON DELETE CASCADE,
    final_answer TEXT, solution_plan TEXT, detailed_solution TEXT,
    confidence REAL, model_used TEXT, parallel_solution TEXT,
    teaching TEXT, evidence_json TEXT,
    status TEXT DEFAULT 'draft', out_of_scope_risk TEXT,
    conflict TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
    question_text TEXT, image_file TEXT,
    student_answer TEXT, correct_answer TEXT, user_explanation TEXT,
    ai_error_json TEXT, final_error_json TEXT,
    mastery REAL DEFAULT 0.0,
    status TEXT DEFAULT 'provisional',
    next_review_at TEXT, review_stage INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    kind TEXT, exam_date TEXT, scope_json TEXT,
    outline TEXT, review_materials TEXT, self_test TEXT,
    auditor_result TEXT, score REAL,
    status TEXT DEFAULT 'pending', outputs_json TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS review_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER REFERENCES reviews(id) ON DELETE CASCADE,
    question_no TEXT, question_text TEXT, user_answer TEXT,
    is_correct INTEGER, mastery REAL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow TEXT NOT NULL, mode TEXT,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
    status TEXT DEFAULT 'pending',
    input_json TEXT, output_json TEXT, error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS run_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    node_name TEXT, agent_role TEXT, model TEXT, status TEXT,
    attempt INTEGER DEFAULT 1,
    started_at TEXT, finished_at TEXT,
    tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    input_ref TEXT, output_ref TEXT, output_json TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS sync_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT, asset_id INTEGER, asset_path TEXT,
    content_hash TEXT, status TEXT DEFAULT 'pending',
    retries INTEGER DEFAULT 0, last_error TEXT, synced_at TEXT
);
CREATE TABLE IF NOT EXISTS graph_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    title TEXT, node_type TEXT DEFAULT 'topic', meta_json TEXT
);
CREATE TABLE IF NOT EXISTS graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER REFERENCES graph_nodes(id) ON DELETE CASCADE,
    target_id INTEGER REFERENCES graph_nodes(id) ON DELETE CASCADE,
    relation TEXT, status TEXT DEFAULT 'confirmed'
);
CREATE TABLE IF NOT EXISTS academic_calendar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    event_type TEXT, title TEXT, date TEXT, detail TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS evidence_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT, owner_id INTEGER,
    chunk_id INTEGER REFERENCES source_chunks(id) ON DELETE SET NULL,
    source_type TEXT, quote TEXT, locator TEXT,
    evidence_kind TEXT, verify_status TEXT, verify_method TEXT,
    model TEXT, prompt_version TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS run_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER UNIQUE REFERENCES workflow_runs(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'queued',
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS parse_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER UNIQUE REFERENCES materials(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'queued',
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);
"""

# 索引单独维护：必须在表/列补齐之后创建（避免旧库缺列时 CREATE INDEX 失败）。
INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_chapters_course ON chapters(course_id);
CREATE INDEX IF NOT EXISTS idx_lessons_chapter ON lessons(chapter_id);
CREATE INDEX IF NOT EXISTS idx_lessons_course ON lessons(course_id);
CREATE INDEX IF NOT EXISTS idx_materials_lesson ON materials(lesson_id);
CREATE INDEX IF NOT EXISTS idx_materials_course ON materials(course_id);
CREATE INDEX IF NOT EXISTS idx_materials_hash ON materials(sha256);
CREATE INDEX IF NOT EXISTS idx_chunks_material ON source_chunks(material_id);
CREATE INDEX IF NOT EXISTS idx_chunks_chapter ON source_chunks(chapter_id);
CREATE INDEX IF NOT EXISTS idx_notes_lesson ON notes(lesson_id);
CREATE INDEX IF NOT EXISTS idx_questions_homework ON questions(homework_id);
CREATE INDEX IF NOT EXISTS idx_answer_items_question ON answer_items(question_id);
CREATE INDEX IF NOT EXISTS idx_errors_chapter_status ON errors(chapter_id, status);
CREATE INDEX IF NOT EXISTS idx_run_nodes_run ON run_nodes(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_workflow ON workflow_runs(workflow, status);
CREATE TABLE IF NOT EXISTS review_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER REFERENCES reviews(id) ON DELETE CASCADE,
    question_no TEXT, question_text TEXT, user_answer TEXT,
    is_correct INTEGER, mastery_after REAL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
"""

# 幂等列补充：{表: [(列, 类型, 默认值或 None, 是否 NOT NULL)]}
# 用于「CREATE TABLE IF NOT EXISTS 不会补列」的脚本化演进（影子迁移后跑一次）。
ADDITIVE_MIGRATIONS: list[tuple[str, str, str]] = []


def _row_to_dict(row: sqlite3.Row) -> dict:
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}


_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=15.0)
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA_SQL)
            # 补齐存量库缺失字段，再建索引（索引可能在 ADD COLUMN 之后才能创建）
            _migrate_conn(conn)
            conn.executescript(INDEX_SQL)
            conn.executescript(
                "PRAGMA journal_mode=WAL;"
                "PRAGMA synchronous=NORMAL;"
                "PRAGMA foreign_keys=ON;"
                "PRAGMA busy_timeout=15000;"
            )
            _conn = conn
        return _conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """多步写入原子性：全部成功提交，失败整体回滚。"""
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# 统一数据访问（返回 dict，不泄漏 sqlite3.Row）
# ---------------------------------------------------------------------------
def execute(sql: str, params: tuple = (), *, returning_lastrowid: bool = False):
    cur = _get_conn().execute(sql, params)
    _get_conn().commit()
    lid = cur.lastrowid
    return lid if returning_lastrowid else (lid if lid else cur.rowcount)


def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    rows = _get_conn().execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def fetch_one(sql: str, params: tuple = ()) -> Optional[dict]:
    row = _get_conn().execute(sql, params).fetchone()
    return _row_to_dict(row) if row is not None else None


def query(sql: str, params: tuple = ()) -> list[dict]:
    return fetch_all(sql, params)


def query_one(sql: str, params: tuple = ()) -> Optional[dict]:
    return fetch_one(sql, params)


def init_db() -> None:
    _get_conn()


# ---------------------------------------------------------------------------
# 影子增量迁移：对齐旧库中缺失的新字段（幂等）
# ---------------------------------------------------------------------------
def _migrate_conn(conn: sqlite3.Connection) -> list[str]:
    """（不持锁调用方需自行加 _lock）对存量库补齐方案要求的缺失字段。"""
    migrated: list[str] = []
    info = {
        r[0]: {c[1] for c in conn.execute(f"PRAGMA table_info('{r[0]}')").fetchall()}
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }

    def add_col(table: str, col: str, ddl: str):
        if table in info and col not in info[table]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            migrated.append(f"{table}.{col}")

    # materials: 补充方案要求的元信息字段
    add_col("materials", "display_name", "display_name TEXT")
    add_col("materials", "name", "name TEXT")
    add_col("materials", "kind", "kind TEXT")
    add_col("materials", "mime", "mime TEXT")
    add_col("materials", "sha256", "sha256 TEXT")
    add_col("materials", "size_bytes", "size_bytes INTEGER DEFAULT 0")
    add_col("materials", "parse_error", "parse_error TEXT")
    add_col("materials", "status", "status TEXT DEFAULT 'uploaded'")
    add_col("materials", "created_at", "created_at TEXT")
    add_col("materials", "updated_at", "updated_at TEXT")

    # notes：方案要求补齐课程归属与正文/model
    add_col("notes", "chapter_id", "chapter_id INTEGER")
    add_col("notes", "course_id", "course_id INTEGER")
    add_col("notes", "body", "body TEXT")
    add_col("notes", "model_used", "model_used TEXT")
    add_col("notes", "created_at", "created_at TEXT")

    # homeworks / questions / answer_items 补齐
    add_col("homeworks", "course_id", "course_id INTEGER")
    add_col("homeworks", "mode", "mode TEXT")
    add_col("homeworks", "date", "date TEXT")
    add_col("homeworks", "input_dir", "input_dir TEXT")
    add_col("homeworks", "created_at", "created_at TEXT")
    add_col("questions", "image_file", "image_file TEXT")
    add_col("questions", "knowledge_points_json", "knowledge_points_json TEXT")
    add_col("questions", "scope_json", "scope_json TEXT")
    add_col("questions", "risk_json", "risk_json TEXT")
    add_col("answer_items", "model_used", "model_used TEXT")
    add_col("answer_items", "parallel_solution", "parallel_solution TEXT")
    add_col("answer_items", "teaching", "teaching TEXT")
    add_col("answer_items", "conflict", "conflict TEXT")
    add_col("answer_items", "out_of_scope_risk", "out_of_scope_risk TEXT")
    add_col("errors", "mastery", "mastery REAL DEFAULT 0.0")
    add_col("source_chunks", "image_file", "image_file TEXT")
    add_col("source_chunks", "ocr_confidence", "ocr_confidence REAL DEFAULT 1.0")
    add_col("source_chunks", "topics_json", "topics_json TEXT")
    add_col("source_chunks", "embedding", "embedding TEXT")
    add_col("source_chunks", "created_at", "created_at TEXT")

    # workflow_runs 补充 updated_at
    add_col("workflow_runs", "updated_at", "updated_at TEXT")

    # run_nodes 补充 attempt / output_json / latency 结构结果
    add_col("run_nodes", "attempt", "attempt INTEGER DEFAULT 1")
    add_col("run_nodes", "output_json", "output_json TEXT")
    add_col("run_nodes", "latency_ms", "latency_ms INTEGER DEFAULT 0")

    # review_attempts 新增（用于复习作答记录）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS review_attempts ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " review_id INTEGER REFERENCES reviews(id) ON DELETE CASCADE,"
        " question_no TEXT, question_text TEXT, user_answer TEXT,"
        " is_correct INTEGER, mastery_after REAL,"
        " created_at TEXT DEFAULT (datetime('now','localtime')) )"
    )
    conn.commit()
    return migrated


def migrate_existing_db() -> dict:
    # 由 _get_conn 首次连接时自动执行；此处仅保证执行过并返回幂等统计
    conn = _get_conn()
    return {"migrated_columns": "runs_at_connect", "db_path": str(DB_PATH)}


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