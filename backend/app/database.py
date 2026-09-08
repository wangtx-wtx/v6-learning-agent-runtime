"""
V5.3 SQLite 数据库:WAL 模式 + 事务上下文 + 连接复用 + 索引补齐 + 路径安全校验。

V5.3 改造要点(对照 ChatGPT 审查报告):
- PRAGMA journal_mode=WAL:并发读写不互相阻塞
- PRAGMA busy_timeout=5000:避免 SQLITE_BUSY 短暂失败
- 单例连接 + check_same_thread=False:不每次 open/close
- transaction() 上下文管理器:保证多步写入原子性
- 12 个性能索引(chapters/lessons/materials/chunks/errors/run_nodes/runs)
- resolve_material_path() + resolve_materials():P0 安全边界
  客户端只能传 material_id,后端从 DB 取真实路径并做 UPLOAD_ROOT containment 校验
"""
from __future__ import annotations
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .config import DB_PATH, DATA_DIR

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT, semester TEXT, teacher TEXT, schedule_json TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    chapter_no INTEGER, title TEXT NOT NULL, syllabus_ref TEXT,
    status TEXT DEFAULT 'not_started',
    review_status TEXT DEFAULT 'none',
    reviewed_at TEXT, completed_at TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    lesson_no TEXT, title TEXT, date TEXT,
    status TEXT DEFAULT 'not_started',
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    file_path TEXT NOT NULL, file_hash TEXT, type TEXT, kind TEXT,
    name TEXT, parser_status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS source_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER REFERENCES materials(id) ON DELETE CASCADE,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE CASCADE,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    type TEXT, locator TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE CASCADE,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    title TEXT, body TEXT, evidence_json TEXT, model_used TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS homeworks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, status TEXT DEFAULT 'pending',
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE CASCADE,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    homework_id INTEGER REFERENCES homeworks(id) ON DELETE CASCADE,
    question_no INTEGER, text TEXT
);
CREATE TABLE IF NOT EXISTS answer_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER REFERENCES questions(id) ON DELETE CASCADE,
    final_answer TEXT, solution_plan TEXT, detailed_solution TEXT,
    confidence REAL, model_used TEXT, parallel_solution TEXT,
    teaching TEXT, evidence_json TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
    question_text TEXT, image_file TEXT,
    student_answer TEXT, correct_answer TEXT, user_explanation TEXT,
    ai_error_json TEXT, final_error_json TEXT,
    status TEXT DEFAULT 'provisional',
    next_review_at TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    kind TEXT, outline TEXT, review_materials TEXT, self_test TEXT,
    auditor_result TEXT, score REAL,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow TEXT NOT NULL, mode TEXT,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
    status TEXT DEFAULT 'pending',
    input_json TEXT, output_json TEXT, error TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS run_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    node_name TEXT, agent_role TEXT, model TEXT, status TEXT,
    started_at TEXT, finished_at TEXT,
    tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
    input_ref TEXT, output_ref TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS sync_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT, status TEXT, path TEXT, last_run_at TEXT, error TEXT
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
    relation TEXT
);
CREATE TABLE IF NOT EXISTS academic_calendar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    event_type TEXT, title TEXT, date TEXT, detail TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_chapters_course ON chapters(course_id);
CREATE INDEX IF NOT EXISTS idx_lessons_chapter ON lessons(chapter_id);
CREATE INDEX IF NOT EXISTS idx_lessons_course ON lessons(course_id);
CREATE INDEX IF NOT EXISTS idx_materials_lesson ON materials(lesson_id);
CREATE INDEX IF NOT EXISTS idx_materials_chapter ON materials(chapter_id);
CREATE INDEX IF NOT EXISTS idx_materials_course ON materials(course_id);
CREATE INDEX IF NOT EXISTS idx_chunks_chapter ON source_chunks(chapter_id);
CREATE INDEX IF NOT EXISTS idx_chunks_lesson ON source_chunks(lesson_id);
CREATE INDEX IF NOT EXISTS idx_chunks_material ON source_chunks(material_id);
CREATE INDEX IF NOT EXISTS idx_errors_chapter_status ON errors(chapter_id, status);
CREATE INDEX IF NOT EXISTS idx_run_nodes_run ON run_nodes(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_workflow ON workflow_runs(workflow, status);
CREATE INDEX IF NOT EXISTS idx_questions_homework ON questions(homework_id);
CREATE INDEX IF NOT EXISTS idx_answer_items_question ON answer_items(question_id);
CREATE INDEX IF NOT EXISTS idx_notes_chapter ON notes(chapter_id);
"""

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_init_done: bool = False


def _get_conn() -> sqlite3.Connection:
    global _conn, _init_done
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(
                str(DB_PATH), check_same_thread=False, timeout=10.0,
            )
            _conn.row_factory = sqlite3.Row
            if not _init_done:
                _conn.executescript(SCHEMA_SQL)
                # Phase 5:V5.3 启用 WAL + 优化并发
                _conn.executescript(
                    "PRAGMA journal_mode=WAL;"
                    "PRAGMA synchronous=NORMAL;"
                    "PRAGMA foreign_keys=ON;"
                    "PRAGMA busy_timeout=5000;"
                    "PRAGMA temp_store=MEMORY;"
                )
                _init_done = True
        return _conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """
    V5.3 事务上下文:保证多步写入原子性(全部成功/全部回滚)。
    用于:课程导入 / material + chunks 批量 / workflow publish / migration 等。
    """
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


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return _get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    return _get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = (), *, returning_lastrowid: bool = False):
    """
    V5.3 单次执行:在 _get_conn() 持有的连接上直接 execute + commit。
    短事务(<1ms)可直接用,多步原子操作用 transaction()。
    """
    cur = _get_conn().execute(sql, params)
    _get_conn().commit()
    if returning_lastrowid:
        return cur.lastrowid
    return cur.lastrowid if cur.lastrowid else cur.rowcount


def init_db() -> None:
    _get_conn()


# ============== V5.3 P0 路径安全校验 ==============
def _upload_root() -> Path:
    """上传根目录,所有 materials.file_path 必须在此目录下。"""
    return (Path(DATA_DIR) / "uploads").resolve()


def resolve_material_path(material_id: int) -> tuple[str, str, str]:
    """
    P0 安全:把客户端传入的 material_id 解析为真实路径,做 containment 校验。

    返回: (resolved_path, type, kind) 三元组。
    抛出 BusinessError 当:
      - material_id 不存在
      - 路径为空
      - 路径解析失败
      - 路径不在 uploads/ 下(防止越权)
      - 文件不存在
    """
    from .dag import BusinessError
    row = query_one(
        "SELECT file_path, type, kind FROM materials WHERE id=?",
        (material_id,),
    )
    if not row:
        raise BusinessError(f"material_id={material_id} 不存在")
    file_path = row["file_path"]
    if not file_path:
        raise BusinessError(f"material_id={material_id} 缺少 file_path")
    try:
        resolved = Path(file_path).resolve()
    except Exception as e:
        raise BusinessError(f"material_id={material_id} 路径解析失败: {e}")
    upload_root = _upload_root()
    try:
        resolved.relative_to(upload_root)
    except ValueError:
        raise BusinessError(
            f"material_id={material_id} 路径越权:{resolved} 不在 {upload_root} 内"
        )
    if not resolved.exists():
        raise BusinessError(f"material_id={material_id} 文件不存在:{resolved}")
    return str(resolved), (row["type"] or ""), (row["kind"] or "")


def resolve_materials(material_ids: list[int] | None) -> list[dict]:
    """批量解析:返回 [{id, path, type, kind}, ...];空列表 / None 返回 []"""
    out = []
    for mid in (material_ids or []):
        path, mtype, kind = resolve_material_path(mid)
        out.append({"id": mid, "path": path, "type": mtype, "kind": kind})
    return out


def resolve_images(image_ids: list[int] | None) -> list[str]:
    """
    图片解析:支持 [int(material_id) | str(data_url | path)]
    仅当 item 是整数时才走 DB 校验;字符串视为外部 URL 透传。
    """
    out = []
    for iid in (image_ids or []):
        if isinstance(iid, int):
            try:
                path, _, _ = resolve_material_path(iid)
                out.append(path)
            except Exception:
                out.append(iid)  # 解析失败时透传
        else:
            out.append(str(iid))
    return out
