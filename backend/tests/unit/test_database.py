"""V5.5 阶段A数据库测试（方案 2.1/2.2/2.3/2.4/2.5 + 15.2）。

运行：python -m unittest discover -s tests -v
"""
import io
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
_ROOT = Path(__file__).resolve().parent.parent.parent  # backend/
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import database as db  # noqa: E402


class DatabaseTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        db.configure_db(self.tmp / "test.db")

    def tearDown(self):
        db.reset_connections()


class TestFreshDatabase(DatabaseTestBase):
    def test_fresh_db_init(self):
        db.init_db()
        tables = {r["name"] for r in db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        for expected in ("courses", "chapters", "lessons", "materials", "source_chunks",
                         "notes", "homeworks", "questions", "answer_items", "errors",
                         "reviews", "review_attempts", "workflow_runs", "run_nodes",
                         "sync_jobs", "file_blobs", "blob_gc_tasks", "error_events",
                         "retrieval_runs", "model_calls", "run_tasks", "parse_tasks",
                         "evidence_links", "schema_migrations"):
            self.assertIn(expected, tables)

    def test_schema_version(self):
        db.init_db()
        # V5.5.1: 0008 迁移落地后最大版本为 8
        self.assertEqual(db.schema_version(), 8)
        versions = sorted(r["version"] for r in db.fetch_all(
            "SELECT version FROM schema_migrations"))
        self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7, 8])
        # V5.5.1: PRAGMA user_version 应同步到 8
        uv = db.fetch_one("PRAGMA user_version")
        # fetch_one 对 PRAGMA 返回的是元组/Row；归一为 int
        if isinstance(uv, dict):
            uv_int = int(uv.get("user_version", 0))
        else:
            uv_int = int(uv[0] if uv else 0)
        self.assertEqual(uv_int, 8)


    def test_foreign_key_check_after_fresh_init(self):
        db.init_db()
        db.insert("INSERT INTO courses (name) VALUES ('物理')")
        cid = db.fetch_one("SELECT id FROM courses")["id"]
        db.insert("INSERT INTO chapters (course_id, chapter_no, title) VALUES (?,?,?)",
                  (cid, 1, "第一章"))
        chid = db.fetch_one("SELECT id FROM chapters")["id"]
        db.insert("INSERT INTO lessons (chapter_id, course_id, lesson_no, title) VALUES (?,?,?,?)",
                  (chid, cid, "L01", "光的反射"))
        self.assertEqual(db.fetch_all("PRAGMA foreign_key_check"), [])

    def test_lesson_chapter_fk_targets_chapters(self):
        """方案 2.1 核心：lessons.chapter_id 必须引用 chapters(id)。"""
        db.init_db()
        ddl = db.fetch_one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='lessons'"
        )["sql"]
        self.assertIn("REFERENCES chapters(id)", ddl)
        self.assertNotIn("chapter_id INTEGER REFERENCES lessons(id)", ddl)

    def test_fresh_database_foreign_keys(self):
        """全部外键指向正确目标（方案 2.1 表格）。"""
        db.init_db()
        expectations = {
            "chapters": {"course_id": "courses"},
            "lessons": {"chapter_id": "chapters", "course_id": "courses"},
            "materials": {"course_id": "courses", "chapter_id": "chapters", "lesson_id": "lessons"},
            "source_chunks": {"material_id": "materials"},
            "notes": {"lesson_id": "lessons"},
            "questions": {"homework_id": "homeworks"},
            "answer_items": {"question_id": "questions"},
            "evidence_links": {"chunk_id": "source_chunks"},
            "run_nodes": {"run_id": "workflow_runs"},
            "run_tasks": {"run_id": "workflow_runs"},
            "parse_tasks": {"material_id": "materials"},
        }
        for table, fks in expectations.items():
            ddl = db.fetch_one(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )["sql"] or ""
            for col, target in fks.items():
                self.assertIn(f"REFERENCES {target}(id)", ddl,
                              f"{table}.{col} 应引用 {target}(id)")


class TestWriteSemantics(DatabaseTestBase):
    """方案 2.5：insert 返回 lastrowid；execute 返回 rowcount。"""

    def test_execute_returns_rowcount(self):
        db.init_db()
        cid = db.insert("INSERT INTO courses (name) VALUES ('A')")
        self.assertIsInstance(cid, int)
        self.assertGreater(cid, 0)
        n = db.execute("UPDATE courses SET name='B' WHERE id=?", (cid,))
        self.assertEqual(n, 1)
        n = db.execute("UPDATE courses SET name='B' WHERE id=999999")
        self.assertEqual(n, 0)
        n = db.execute("DELETE FROM courses WHERE id=?", (cid,))
        self.assertEqual(n, 1)

    def test_executemany_rowcount(self):
        db.init_db()
        n = db.executemany(
            "INSERT INTO courses (name) VALUES (?)", [("a",), ("b",), ("c",)]
        )
        self.assertEqual(n, 3)

    def test_transaction_rollback(self):
        db.init_db()
        with self.assertRaises(RuntimeError):
            with db.transaction() as conn:
                conn.execute("INSERT INTO courses (name) VALUES ('tx1')")
                conn.execute("INSERT INTO courses (name) VALUES ('tx2')")
                raise RuntimeError("boom")
        self.assertEqual(len(db.fetch_all("SELECT * FROM courses")), 0)

    def test_transaction_commit_and_no_autocommit_inside(self):
        db.init_db()
        with db.transaction() as conn:
            conn.execute("INSERT INTO courses (name) VALUES ('t1')")
            conn.execute("INSERT INTO courses (name) VALUES ('t2')")
        self.assertEqual(len(db.fetch_all("SELECT * FROM courses")), 2)

    def test_multithreaded_write(self):
        """多线程并发写入不冲突（方案 2.4：事务独占短连接）。"""
        db.init_db()
        errors = []

        def worker(tid):
            try:
                for i in range(20):
                    # 事务块内使用上下文返回的连接（与 BEGIN IMMEDIATE 同一连接）
                    with db.transaction() as conn:
                        conn.execute(
                            "INSERT INTO courses (name) VALUES (?)", (f"t{tid}-{i}",)
                        )
                    # 语句级原子写：短连接，也应成功
                    db.execute(
                        "UPDATE courses SET name=? WHERE name=?",
                        (f"t{tid}-u{i}", f"t{tid}-{i}"),
                    )
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(len(db.fetch_all("SELECT * FROM courses")), 80)


class TestLegacyShadowMigration(unittest.TestCase):
    """方案 2.3：真影子迁移（含去重、blob 回填、校验、原子替换）。"""

    def test_existing_db_migration(self):
        tmp = Path(tempfile.mkdtemp())
        legacy = tmp / "legacy.db"

        # 构造一个"旧式"库：错误外键指向 + 重复章节 + 材料
        lc = sqlite3.connect(str(legacy))
        lc.executescript("""
        CREATE TABLE courses (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, semester TEXT);
        CREATE TABLE chapters (id INTEGER PRIMARY KEY AUTOINCREMENT, course_id INTEGER,
            chapter_no INTEGER, title TEXT);
        CREATE TABLE lessons (id INTEGER PRIMARY KEY AUTOINCREMENT,
            chapter_id INTEGER REFERENCES lessons(id), course_id INTEGER, lesson_no TEXT, title TEXT);
        CREATE TABLE materials (id INTEGER PRIMARY KEY AUTOINCREMENT, lesson_id INTEGER,
            chapter_id INTEGER, course_id INTEGER, file_path TEXT, file_hash TEXT, sha256 TEXT,
            type TEXT, kind TEXT, display_name TEXT, name TEXT, mime TEXT, size_bytes INTEGER DEFAULT 0,
            parser_status TEXT DEFAULT 'uploaded', parse_error TEXT, status TEXT DEFAULT 'uploaded',
            created_at TEXT, updated_at TEXT);
        CREATE TABLE source_chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, material_id INTEGER,
            lesson_id INTEGER, chapter_id INTEGER, course_id INTEGER, type TEXT, locator TEXT,
            text TEXT, created_at TEXT);
        CREATE TABLE workflow_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, workflow TEXT, mode TEXT,
            course_id INTEGER, lesson_id INTEGER, chapter_id INTEGER, status TEXT DEFAULT 'pending',
            input_json TEXT, output_json TEXT, error TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE run_nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER,
            node_name TEXT, agent_role TEXT, model TEXT, status TEXT, attempt INTEGER DEFAULT 1,
            started_at TEXT, finished_at TEXT, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
            latency_ms INTEGER DEFAULT 0, input_ref TEXT, output_ref TEXT, output_json TEXT, error TEXT);
        """)
        # 1 门课、重复章节(第1章 x2)、1 课时、1 材料、1 运行
        lc.execute("INSERT INTO courses (name, semester) VALUES ('物理', '2026春')")
        lc.execute("INSERT INTO chapters (course_id, chapter_no, title) VALUES (1, 1, '第一章')")
        lc.execute("INSERT INTO chapters (course_id, chapter_no, title) VALUES (1, 1, '第一章(重复)')")
        lc.execute("INSERT INTO lessons (chapter_id, course_id, lesson_no, title) VALUES (2, 1, 'L01', '测试课时')")
        mat = tmp / "m1.txt"
        mat.write_text("材料内容ABC", encoding="utf-8")
        import hashlib
        sha = hashlib.sha256(mat.read_bytes()).hexdigest()
        lc.execute(
            "INSERT INTO materials (lesson_id, chapter_id, course_id, file_path, file_hash, sha256,"
            " type, kind, display_name, name, size_bytes, parser_status, status) "
            "VALUES (1, 2, 1, ?, ?, ?, 'text', 'text', 'm1', 'm1.txt', 10, 'ready', 'ready')",
            (str(mat), sha, sha),
        )
        lc.execute("INSERT INTO source_chunks (material_id, chapter_id, course_id, type, text) "
                   "VALUES (1, 2, 1, 'text', 'chunk')")
        lc.execute("INSERT INTO workflow_runs (workflow, course_id, status) VALUES ('lesson', 1, 'completed')")
        lc.execute("INSERT INTO run_nodes (run_id, node_name, status) VALUES (1, 'parse', 'success')")
        lc.commit()
        lessons_before = lc.execute("SELECT chapter_id FROM lessons").fetchone()[0]  # 2（重复章节）
        lc.close()

        from app.db_migrate import shadow_migrate
        report = shadow_migrate(legacy)

        self.assertTrue(report["success"])
        self.assertTrue(report["replaced"])
        self.assertEqual(report["copied"]["chapters"]["deduped"], 1)
        # 校验迁移后的库
        db.configure_db(legacy)
        self.assertEqual(db.schema_version(), 8)  # V5.5.1: 包含 0008 迁移
        self.assertEqual(db.fetch_all("PRAGMA foreign_key_check"), [])
        # 重复章节被合并，lesson.chapter_id 已重映射到保留的章节 id=1
        lesson = db.fetch_one("SELECT chapter_id FROM lessons")
        self.assertEqual(lesson["chapter_id"], 1)
        # 材料 blob 回填
        mat_row = db.fetch_one("SELECT blob_id, sha256 FROM materials")
        self.assertIsNotNone(mat_row["blob_id"])
        blob = db.fetch_one("SELECT ref_count FROM file_blobs WHERE id=?", (mat_row["blob_id"],))
        self.assertEqual(blob["ref_count"], 1)
        # 业务数据完整
        self.assertEqual(len(db.fetch_all("SELECT * FROM source_chunks")), 1)
        self.assertEqual(len(db.fetch_all("SELECT * FROM run_nodes")), 1)
        # chunk 的 chapter_id 也被重映射
        chunk = db.fetch_one("SELECT chapter_id FROM source_chunks")
        self.assertEqual(chunk["chapter_id"], 1)
        db.reset_connections()


if __name__ == "__main__":
    unittest.main()
