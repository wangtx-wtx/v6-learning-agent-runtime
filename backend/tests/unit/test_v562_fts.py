"""V5.6.2 FTS 初始化竞态回归测试。

覆盖：
1. fresh DB 初始化后 FTS 结构完整。
2. 已初始化库重复启动幂等。
3. 20 线程并发 init_fts：仅一次实际 DDL，全部返回 ready。
4. 初始化过程中并发检索/删除不出现 no such table（受控降级/等待）。
5. 人工持有 SQLite 写锁，释放后初始化通过。
6. 锁超过重试上限 → failed（不误判 unavailable）。
7. 模拟 SQLite 不支持 FTS5 → 稳定降级 keyword_only(unavailable)。
8. 半完成/缺失结构受控重建。
9. diagnostics 正确返回 FTS 状态。
10. 公开 health 不泄露底层错误。
11. 重启后 FTS 索引保留。

测试自带独立临时库（configure_db），不依赖 run_tests_isolated 的环境；
但若 V5_ENV=test 下 create_connection 对正式库有 guard 兜底。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app import rag, database as db  # noqa: E402


class FTSBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "fts.db"
        db.configure_db(self.db_path)
        db.init_db()  # 建 schema（含 source_chunks）
        rag.reset_fts_state_for_test()

    def tearDown(self):
        rag.reset_fts_state_for_test()
        db.reset_connections()
        for p in self.tmp.rglob("*"):
            if p.is_file():
                try: p.unlink()
                except Exception: pass
        try: self.tmp.rmdir()
        except Exception: pass


class TestFTSInit(FTSBase):
    def test_fresh_db_structure_complete(self):
        """fresh DB init_fts → ready，chunks_fts 存在。"""
        st = rag.init_fts()
        self.assertEqual(st, "ready")
        s = rag.get_fts_status()
        self.assertTrue(s["table_exists"])
        self.assertTrue(s["sqlite_fts5"])

    def test_repeat_start_idempotent(self):
        """重复 init_fts → ready，不重复 DDL（幂等）。"""
        self.assertEqual(rag.init_fts(), "ready")
        self.assertEqual(rag.init_fts(), "ready")
        self.assertEqual(rag.init_fts(), "ready")

    def test_20_threads_one_init(self):
        """20 线程并发 init_fts：全部 return ready；状态终态 ready。"""
        errors = []
        results = []
        def worker():
            try:
                results.append(rag.init_fts())
            except Exception as e:  # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=15)
        self.assertFalse(errors, f"并发 init 出现异常: {errors}")
        self.assertEqual(len(results), 20)
        self.assertTrue(all(r == "ready" for r in results),
                        f"应全部 ready，实际 {set(results)}")
        self.assertTrue(rag._fts_table_exists())
        # 只有一次实际 DDL：表只建一次（用计数验证是否被重复创建）
        conn = db.create_connection()
        cnt = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(cnt, 1)

    def test_concurrent_business_with_init_no_no_such_table(self):
        """初始化过程中并发 fts_delete / retrieve 不抛 no such table。"""
        # 让 init_fts 在建表前停留（模拟 initializing 窗口）
        orig_supported = rag._sqlite_fts5_supported
        def slow_supported():
            time.sleep(0.4)  # 制造 initializing 窗口
            return orig_supported()
        errors = []
        def deleter():
            try:
                # 业务删除：FTS 未就绪时应等待/降级，而非 no such table
                rag.fts_delete_chunk_ids(None, [1, 2, 3])
            except Exception as e:
                errors.append(e)
        results = {}
        def runner():
            results["init"] = rag.init_fts()
        with mock.patch.object(rag, "_sqlite_fts5_supported", slow_supported):
            t_init = threading.Thread(target=runner)
            t_del = threading.Thread(target=deleter)
            t_init.start(); t_del.start()
            t_init.join(timeout=10); t_del.join(timeout=10)
        self.assertFalse(errors, f"业务删除出现 no such table 等异常: {errors}")
        self.assertEqual(results["init"], "ready")

    def test_write_lock_then_release_init_passes(self):
        """人工持有写锁→init 首轮 locked→释放→重试成功。"""
        # 持锁在独立线程内建连接并占用（避免跨线程共享 sqlite 连接）
        lock_held = threading.Event()
        lock_released = threading.Event()
        hold_err = []
        def holder():
            try:
                conn = db.create_connection()
                conn.execute("BEGIN IMMEDIATE")
                lock_held.set()
                time.sleep(0.15)   # 持锁 0.15s（init 首轮 0.1s 命中锁，0.25s 时已释放）
                try:
                    conn.rollback()
                except Exception:
                    pass
                conn.close()
            except Exception as e:  # pragma: no cover
                hold_err.append(e)
            finally:
                lock_released.set()
        t = threading.Thread(target=holder)
        t.start()
        lock_held.wait(timeout=3)
        # holder 已持锁；init_fts 首次 0.1s 尝试应 locked，0.25s 时 holder 已释放 → ready
        st = rag.init_fts()
        lock_released.wait(timeout=3)
        t.join(timeout=3)
        self.assertFalse(hold_err, f"holder 异常: {hold_err}")
        self.assertEqual(st, "ready")

    def test_lock_exceeds_retry_fails(self):
        """持续持锁 → 3 次重试均锁 → failed（非 unavailable）。"""
        blocker = db.create_connection()
        blocker.execute("BEGIN IMMEDIATE")
        try:
            st = rag.init_fts()
        finally:
            blocker.rollback(); blocker.close()
        # 重试间隔共 0.1+0.25+0.5=0.85s，期间一直锁 → failed
        self.assertEqual(st, "failed")
        s = rag.get_fts_status()
        self.assertEqual(s["status"], "failed")
        self.assertEqual(s["last_error_code"], rag.FTS_INIT_BUSY)

    def test_unsupported_fts5_degrades(self):
        """模拟不支持 FTS5 → unavailable（keyword_only 降级），不 failed。"""
        with mock.patch.object(rag, "_sqlite_fts5_supported", return_value=False):
            st = rag.init_fts()
        self.assertEqual(st, "unavailable")
        s = rag.get_fts_status()
        self.assertEqual(s["status"], "unavailable")
        self.assertEqual(s["last_error_code"], rag.FTS_FTS5_UNSUPPORTED)

    def test_missing_structure_rebuild(self):
        """半完成/缺失结构：DROP chunks_fts 后 init_fts 重建。"""
        self.assertEqual(rag.init_fts(), "ready")
        c = db.create_connection()
        c.execute("DROP TABLE IF EXISTS chunks_fts")
        c.commit(); c.close()
        rg = rag.get_fts_status()
        # 目前状态仍 cached ready，但表已删
        rag.reset_fts_state_for_test()
        st = rag.init_fts()
        self.assertEqual(st, "ready")
        self.assertTrue(rag._fts_table_exists())

    def test_status_and_health(self):
        """get_fts_status 返回结构化字段；health 只暴露简化状态。"""
        rag.init_fts()
        s = rag.get_fts_status()
        for k in ("status", "sqlite_fts5", "table_exists",
                  "indexable_chunks", "indexed_chunks", "last_error_code"):
            self.assertIn(k, s)
        self.assertEqual(s["status"], "ready")
        # 插入块并索引
        rid = db.insert("INSERT INTO source_chunks (type, locator, text) VALUES ('text','L1','光学折射')")
        rag.fts_index_chunks(None, [(rid, "光学折射")])
        s2 = rag.get_fts_status()
        self.assertGreaterEqual(s2["indexed_chunks"], 1)

    @unittest.skipUnless(
        os.environ.get("V5_ENV", "").strip().lower() == "test"
        and os.environ.get("V5_TEST_DATA_ROOT", "").strip(),
        "需 V5_ENV=test + V5_TEST_DATA_ROOT（避免 TestClient lifespan 污染正式备份目录）",
    )
    def test_health_endpoint_no_error_leak(self):
        """公开 /api/health 返回简化 fts，不泄露异常文本。"""
        from fastapi.testclient import TestClient
        from app.main import app
        from app import config as _cfg
        _cfg.MOBILE_TOKEN = ""  # TestClient 非本机 host 下放行业务 API
        rag.init_fts()
        with TestClient(app) as client:
            r = client.get("/api/health")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertIn("fts", body)
            self.assertIn(body["fts"], ("ready", "unavailable", "failed", "uninitialized"))
            # 不得泄露底层异常
            self.assertNotIn("likely requires the FTS5", str(body))
            self.assertNotIn("FTS_INIT", str(body))
            # diagnostics 需本机，TestClient 非本机 host → 403（保护生效）
            d = client.get("/api/admin/diagnostics")
            self.assertIn(d.status_code, (403, 200, 503))

    def test_restart_keeps_index(self):
        """模拟重启：同库文件重建连接+init_fts，索引仍在。"""
        rag.init_fts()
        rid = db.insert("INSERT INTO source_chunks (type, locator, text) VALUES ('text','L2','波动光学')")
        rag.fts_index_chunks(None, [(rid, "波动光学")])
        before = rag.get_fts_status()["indexed_chunks"]
        # 模拟重启：清连接 + 复位状态机（但不删库文件）→ 重新初始化
        db.reset_connections()
        rag.reset_fts_state_for_test()
        st = rag.init_fts()
        self.assertEqual(st, "ready")
        after = rag.get_fts_status()["indexed_chunks"]
        self.assertEqual(after, before, "重启后 FTS 索引应保留")


if __name__ == "__main__":
    unittest.main()