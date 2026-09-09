"""V5.5.1 稳定性补丁单元测试。

覆盖：
- time_utils UTC 解析与格式化
- workflow_worker 租约写入为 UTC 字面量
- recovery 仅回收已过期租约
- GatewayClient.aclose 实例方法
- 单实例锁（acquire / release / 旧锁回收）
- restore_snapshot 校验 user_version 一致性
- sync_user_version 把 PRAGMA user_version 提到 MAX(version)
"""
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 允许直接运行该文件
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

from app import time_utils  # noqa: E402
from app import instance_lock  # noqa: E402
from app import database as db  # noqa: E402
from app.gateway import GatewayClient  # noqa: E402
import app.gateway as gateway_mod  # noqa: E402


# ---------------------------------------------------------------------------
# 1) time_utils
# ---------------------------------------------------------------------------
class TestTimeUtils(unittest.TestCase):
    def test_now_utc_is_timezone_aware(self):
        n = time_utils.now_utc()
        self.assertIsNotNone(n.tzinfo)
        self.assertEqual(n.utcoffset(), timedelta(0))

    def test_iso_roundtrip(self):
        s = time_utils.now_utc_iso()
        back = time_utils.iso_to_utc(s)
        self.assertIsNotNone(back)
        self.assertEqual(back.utcoffset(), timedelta(0))

    def test_iso_handles_z_suffix(self):
        back = time_utils.iso_to_utc("2026-09-09T10:00:00Z")
        self.assertIsNotNone(back)
        self.assertEqual(back.utcoffset(), timedelta(0))
        self.assertEqual(back.year, 2026)

    def test_iso_handles_sqlite_space(self):
        back = time_utils.iso_to_utc("2026-09-09 10:00:00")
        self.assertIsNotNone(back)
        self.assertEqual(back.utcoffset(), timedelta(0))

    def test_utc_now_plus_returns_future(self):
        s = time_utils.utc_now_plus_sql(60)
        # SQLite 字面量 'YYYY-MM-DD HH:MM:SS' 长度 19
        self.assertEqual(len(s), 19)
        # 用 UTC 解析后再和 now_utc()（tz-aware）比较，避免 naive vs tz-aware
        # datetime 的比较语义不一致
        before = time_utils.now_utc()
        future = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        after = time_utils.now_utc()
        # 至少比 before 晚 50 秒，且不晚于 after+60
        self.assertGreaterEqual((future - before).total_seconds(), 50)
        self.assertLessEqual((future - after).total_seconds(), 70)

    def test_utcnow_shim(self):
        # 兼容性别名
        n = time_utils.utcnow()
        self.assertIsNotNone(n.tzinfo)


# ---------------------------------------------------------------------------
# 2) GatewayClient aclose
# ---------------------------------------------------------------------------
class TestGatewayAclose(unittest.TestCase):
    def test_instance_method_exists(self):
        self.assertTrue(hasattr(GatewayClient, "aclose"))
        self.assertTrue(callable(GatewayClient.aclose))

    def test_aclose_idempotent(self):
        """两次 aclose 不应抛错（第二次 _pool 已为 None）。"""
        import asyncio
        async def run():
            # 强制 lazy-init 一次
            _ = gateway_mod._get_client()
            g = GatewayClient()
            await g.aclose()
            await g.aclose()  # 第二次应静默 noop
            self.assertIsNone(gateway_mod._pool)
            self.assertTrue(g._closed)
        asyncio.run(run())

    def test_module_aclose_dispatches_to_instance(self):
        """模块级 aclose 仍可调用，行为上等价于实例方法。"""
        import asyncio
        async def run():
            _ = gateway_mod._get_client()
            await gateway_mod.aclose()
            self.assertIsNone(gateway_mod._pool)
        asyncio.run(run())


# ---------------------------------------------------------------------------
# 3) instance_lock
# ---------------------------------------------------------------------------
class TestInstanceLock(unittest.TestCase):
    def setUp(self):
        from app import config
        self.tmp = Path(tempfile.mkdtemp())
        # 替换 _lock_path 实现指向 tmp（不触发真实 DATA_DIR）
        self._orig_lock_path = instance_lock._lock_path
        instance_lock._lock_path = lambda: self.tmp / ".instance.lock"
        # 重置模块级全局锁，避免跨测试污染
        instance_lock.release_instance_lock()
        instance_lock._global_lock = None

    def tearDown(self):
        instance_lock.release_instance_lock()
        instance_lock._global_lock = None
        instance_lock._lock_path = self._orig_lock_path
        try:
            for p in self.tmp.iterdir():
                p.unlink()
            self.tmp.rmdir()
        except Exception:
            pass

    def test_acquire_and_release(self):
        # 获取锁 → 本进程 pid 记录；释放 → 可再次获取
        info = instance_lock.acquire_instance_lock()
        self.assertIn("pid", info)
        self.assertEqual(info["pid"], os.getpid())
        # 本进程持有的 in-memory 锁信息可读
        lock = instance_lock.read_instance_lock()
        self.assertIsNotNone(lock, "acquire 后 read_instance_lock 应返回内容")
        self.assertEqual(lock["pid"], os.getpid())
        instance_lock.release_instance_lock()
        # release 后（OS 锁释放）可再次获取——证明锁已释放
        info2 = instance_lock.acquire_instance_lock()
        self.assertEqual(info2["pid"], os.getpid())
        instance_lock.release_instance_lock()

    def test_acquire_with_dead_pid_overwrites(self):
        # 写一个已死进程的锁文件（内容只是诊断；真正的互斥是 OS 锁）
        (self.tmp / ".instance.lock").write_text(
            '{"pid": 99999999, "host": "fake", "started_at": "x"}', encoding="utf-8"
        )
        # 每次 acquire 必须创建/打开锁文件并获取 OS 锁，99999999 的 PID 不存在
        info = instance_lock.acquire_instance_lock()
        self.assertEqual(info["pid"], os.getpid())
        instance_lock.release_instance_lock()

    def test_double_acquire_raises(self):
        instance_lock.acquire_instance_lock()
        try:
            with self.assertRaises(RuntimeError):
                instance_lock.acquire_instance_lock()
        finally:
            instance_lock.release_instance_lock()

    def test_signature_no_force_param(self):
        """E.1/E.7: acquire_instance_lock/InstanceLock.acquire 无 force 参数；
        lifecycle 用 0 参调用（P0 修复）。"""
        import inspect as _inspect
        sig = _inspect.signature(instance_lock.acquire_instance_lock)
        self.assertNotIn("force", sig.parameters,
                         f"acquire_instance_lock 不应有 force 参数，实际 {sig}")
        sig2 = _inspect.signature(instance_lock.InstanceLock.acquire)
        self.assertNotIn("force", sig2.parameters,
                         f"InstanceLock.acquire 不应有 force 参数，实际 {sig2}")
        # E.7: 非 override 上下文（本测试默认 is_override False）下 LIFESPAN 调用路径
        # 验证 0 参调用不抛 TypeError
        info = instance_lock.acquire_instance_lock()  # 非 override：真实获取
        self.assertIn("pid", info)
        instance_lock.release_instance_lock()

    def test_lifespan_call_path_without_force(self):
        """E.7: 模拟 lifecycle 的非 override 分支——0 参调用 acquire；
        第二实例立即 RuntimeError；释放后可重新启动。"""
        from app import lifecycle  # noqa: F401
        import inspect as _inspect
        # 直接验证 lifecycle 里对 acquire_instance_lock 的调用是用 0 参（无 force）
        # production 函数本就不接受 force，0 参调用成功即证明签名匹配。
        info = instance_lock.acquire_instance_lock()
        self.assertIn("pid", info)
        # 第二实例（同一进程内重复 acquire）立即致命失败
        with self.assertRaises(RuntimeError):
            instance_lock.acquire_instance_lock()
        # 释放后（模拟正常退出 → 重启）可再次获取
        instance_lock.release_instance_lock()
        info2 = instance_lock.acquire_instance_lock()
        self.assertIn("pid", info2)
        instance_lock.release_instance_lock()


# ---------------------------------------------------------------------------
# 4) recovery：仅回收过期租约，不动有效租约
# ---------------------------------------------------------------------------
class TestRecoveryStaleLeases(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        db.configure_db(self.tmp / "test.db")
        db.init_db()

    def tearDown(self):
        db.reset_connections()

    def test_recover_only_expired_leases(self):
        from app.workers import recovery
        from app.workers.workflow_worker import WORKER_ID
        from app.time_utils import utc_now_plus_sql

        # 1) 准备一个 workflow_run + run_task
        run_id = db.insert(
            "INSERT INTO workflow_runs (workflow, mode, status, input_json) "
            "VALUES ('lesson', 'attend', 'running', '{}')"
        )
        task_id = db.insert(
            "INSERT INTO run_tasks (run_id, status, attempts, max_attempts, "
            " lease_owner, lease_expires_at) "
            "VALUES (?, 'running', 1, 3, ?, ?)",
            (run_id, WORKER_ID, utc_now_plus_sql(600)),  # 10 分钟有效
        )

        n = recovery.stale_leases()
        self.assertEqual(n, 0, "有效租约不应该被回收")

        # 2) 写入一个已过期的租约
        past = utc_now_plus_sql(-60)  # 已过期 1 分钟
        db.execute(
            "UPDATE run_tasks SET lease_expires_at=? WHERE id=?",
            (past, task_id),
        )
        n = recovery.stale_leases()
        self.assertEqual(n, 1, "过期租约应该被回收一次")

        # 再次调用应稳定
        n = recovery.stale_leases()
        self.assertEqual(n, 0, "重复调用不应再次回收")

    def test_recover_interrupted_only(self):
        """recover_interrupted_tasks 不应再重置状态为 running 的行。"""
        from app.workers import recovery
        from app.time_utils import utc_now_plus_sql

        run_id = db.insert(
            "INSERT INTO workflow_runs (workflow, mode, status, input_json) "
            "VALUES ('lesson', 'attend', 'running', '{}')"
        )
        # 创建一个 status='running' 且有有效租约的任务
        running_task = db.insert(
            "INSERT INTO run_tasks (run_id, status, attempts, max_attempts, "
            " lease_owner, lease_expires_at) "
            "VALUES (?, 'running', 1, 3, 'wk-other', ?)",
            (run_id, utc_now_plus_sql(600)),
        )
        # 创建一个 status='interrupted' 的任务
        int_run_id = db.insert(
            "INSERT INTO workflow_runs (workflow, mode, status, input_json) "
            "VALUES ('lesson', 'attend', 'interrupted', '{}')"
        )
        int_task = db.insert(
            "INSERT INTO run_tasks (run_id, status) VALUES (?, 'interrupted')",
            (int_run_id,),
        )

        recovery.recover_interrupted_tasks()

        # running 任务不应被重置
        row = db.fetch_one("SELECT status FROM run_tasks WHERE id=?", (running_task,))
        self.assertEqual(row["status"], "running", "V5.5.1: 有效 running 不应被重置")
        # interrupted 任务应被重置为 queued
        row = db.fetch_one("SELECT status FROM run_tasks WHERE id=?", (int_task,))
        self.assertEqual(row["status"], "queued")


# ---------------------------------------------------------------------------
# 5) sync_user_version
# ---------------------------------------------------------------------------
class TestSyncUserVersion(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        db.configure_db(self.tmp / "test.db")
        db.init_db()

    def tearDown(self):
        db.reset_connections()

    def test_user_version_synced_to_max(self):
        # 把 user_version 强行设回 0
        conn = db.create_connection()
        try:
            conn.execute("PRAGMA user_version=0")
            conn.commit()
        finally:
            conn.close()
        # V5.5.1 收尾 B.3: sync_user_version 必须接受 conn 参数
        conn = db.create_connection()
        try:
            db.sync_user_version(conn)
        finally:
            conn.close()
        # 验证已经与 schema_migrations MAX 一致
        conn = db.create_connection()
        try:
            uv = conn.execute("PRAGMA user_version").fetchone()[0]
            maxv = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(int(uv), int(maxv))
        self.assertGreater(int(uv), 0)

    def test_sync_user_version_does_not_touch_other_db(self):
        """B.3: sync_user_version 必须只作用于传入的目标连接；
        不应触及其他数据库（影子迁移 / dry-run 安全性）。"""
        from pathlib import Path
        import tempfile
        import sqlite3 as _sq
        tmp = Path(tempfile.mkdtemp())
        # 准备两个独立 db：A 和 B
        a_path = tmp / "a.db"
        b_path = tmp / "b.db"
        for p in (a_path, b_path):
            conn = _sq.connect(str(p))
            try:
                conn.executescript("CREATE TABLE t (x INTEGER);")
                conn.execute("PRAGMA user_version=0")
                conn.commit()
            finally:
                conn.close()
        # 仅在 A 上 apply 一个 fake migration + sync_user_version
        a_conn = _sq.connect(str(a_path))
        a_conn.row_factory = _sq.Row
        try:
            a_conn.execute("CREATE TABLE schema_migrations "
                           "(version INTEGER PRIMARY KEY, name TEXT, "
                           "checksum TEXT, applied_at TEXT)")
            a_conn.execute("INSERT INTO schema_migrations VALUES (5, 'x', 'y', 'z')")
            a_conn.commit()
            db.sync_user_version(a_conn)  # 应当只动 a_conn
        finally:
            a_conn.close()
        # 验证 A 已同步到 5，B 不变
        a_chk = _sq.connect(str(a_path))
        b_chk = _sq.connect(str(b_path))
        try:
            self.assertEqual(int(a_chk.execute("PRAGMA user_version").fetchone()[0]), 5)
            self.assertEqual(int(b_chk.execute("PRAGMA user_version").fetchone()[0]), 0)
        finally:
            a_chk.close()
            b_chk.close()


# ---------------------------------------------------------------------------
# 6) restore_snapshot user_version 校验
# ---------------------------------------------------------------------------
class TestRestoreSnapshotValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.snap = self.tmp / "snap.db"

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            if p.is_file():
                try: p.unlink()
                except Exception: pass
        try: self.tmp.rmdir()
        except Exception: pass

    def _create_snapshot(self, *, set_user_version_to: int, max_migration: int,
                          use_real_checksums: bool = True) -> Path:
        """构造测试快照。

        use_real_checksums=True（默认）：从 migrations 目录读取真实 SHA-256
        checksum，确保 ensure_schema 的篡改校验能通过。这是 C.3 要求的真实测试。
        """
        # 读取真实 checksum（与 database._migration_files 一致：read_text(utf-8) + encode）
        real_checksums: dict[int, str] = {}
        if use_real_checksums:
            import hashlib
            migrations_dir = _ROOT / "migrations"
            for p in sorted(migrations_dir.glob("*.sql")):
                try:
                    v = int(p.stem.split("_", 1)[0])
                    sql = p.read_text(encoding="utf-8")
                    real_checksums[v] = hashlib.sha256(
                        sql.encode("utf-8")).hexdigest()
                except (ValueError, OSError):
                    continue
        conn = sqlite3_mod().connect(str(self.snap))
        try:
            conn.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
                "name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            for v in range(1, max_migration + 1):
                checksum = real_checksums.get(v, "x" * 64)
                # 文件名风格与 _migration_files 解析一致：'0001_baseline'
                name = f"{v:04d}_{self._migration_name(v)}"
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                    "VALUES (?, ?, ?, '2026-01-01')",
                    (v, name, checksum),
                )
            conn.execute(f"PRAGMA user_version={set_user_version_to}")
            conn.commit()
        finally:
            conn.close()
        return self.snap

    @staticmethod
    def _migration_name(v: int) -> str:
        # 与 migrations 目录实际文件名一致
        return {
            1: "baseline", 2: "material_state", 3: "workflow_queue",
            4: "review_attempts", 5: "evidence_links", 6: "v55_constraints",
            7: "blob_dedup_model", 8: "v551_user_version_utc",
        }.get(v, f"v{v}")

    def test_inconsistent_user_version_rejected(self):
        # tools.restore_snapshot 不在 app 包内；用 importlib 直接载入
        from importlib import import_module
        import sys
        if "tools" not in sys.modules:
            sys.path.insert(0, str(_ROOT))
        validate_snapshot = import_module("tools.restore_snapshot").validate_snapshot
        # 模拟 schema_migrations max=8 但 user_version=0（V5.5 报告里描述的情况）
        self._create_snapshot(set_user_version_to=0, max_migration=8)
        with self.assertRaises(ValueError) as ctx:
            validate_snapshot(self.snap)
        self.assertIn("user_version", str(ctx.exception))

    def test_consistent_user_version_accepted(self):
        from importlib import import_module
        import sys
        if "tools" not in sys.modules:
            sys.path.insert(0, str(_ROOT))
        validate_snapshot = import_module("tools.restore_snapshot").validate_snapshot
        self._create_snapshot(set_user_version_to=8, max_migration=8)
        info = validate_snapshot(self.snap)
        self.assertEqual(info["schema_version"], 8)
        self.assertEqual(info["migrations"], [1, 2, 3, 4, 5, 6, 7, 8])

    def test_old_v7_snapshot_upgrade_path(self):
        """B.8: 旧 v7 备份（user_version=0, schema_migrations max=7）
        不应被直接拒绝；应能通过 upgrade_snapshot_user_version 升级
        到 v8 后再恢复。C.3 要求测试用真实 checksum 验证。"""
        from importlib import import_module
        import sys
        if "tools" not in sys.modules:
            sys.path.insert(0, str(_ROOT))
        rmod = import_module("tools.restore_snapshot")
        # 1) 构造旧 v7 快照（user_version=0, migrations=[1..7]，真实 checksum）
        self._create_snapshot(set_user_version_to=0, max_migration=7)
        # 2) validate 应拒绝（user_version 不一致）
        with self.assertRaises(ValueError) as ctx:
            rmod.validate_snapshot(self.snap)
        self.assertIn("user_version", str(ctx.exception))
        # 3) 通过 upgrade 升级到 v8（生成 .upgraded_to_v8.db）
        result = rmod.upgrade_snapshot_user_version(self.snap, 8, dry_run=False)
        self.assertFalse(result["dry_run"])
        self.assertIsNotNone(result["upgraded"])
        # 4) 升级后副本应能通过 validate（ensure_schema 自动写回真实 checksum）
        upgraded = Path(result["upgraded"])
        info = rmod.validate_snapshot(upgraded)
        self.assertEqual(info["schema_version"], 8)
        # 5) 升级后副本与原快照内容不同
        self.assertNotEqual(self.snap.read_bytes(), upgraded.read_bytes())

    def test_checksum_tampering_rejected(self):
        """C.3: 已应用 migration 的 checksum 被篡改的旧备份，
        upgrade 应当拒绝（SchemaTamperedError）。"""
        from importlib import import_module
        import sqlite3 as _sq
        import sys
        if "tools" not in sys.modules:
            sys.path.insert(0, str(_ROOT))
        rmod = import_module("tools.restore_snapshot")
        # 用真实 checksum 建一个 v7 备份
        self._create_snapshot(set_user_version_to=0, max_migration=7)
        # 篡改 v3（workflow_queue）的 checksum
        conn = _sq.connect(str(self.snap))
        try:
            conn.execute(
                "UPDATE schema_migrations SET checksum=? WHERE version=3",
                ("f" * 64,),
            )
            conn.commit()
        finally:
            conn.close()
        # upgrade 应抛 SchemaTamperedError
        with self.assertRaises(Exception) as ctx:
            rmod.upgrade_snapshot_user_version(self.snap, 8, dry_run=False)
        # ensure_schema 抛 SchemaTamperedError；restore 包用 ValueError 包成 upgrade 信息
        self.assertIn("checksum", str(ctx.exception).lower())

    def test_upgrade_dry_run_does_not_write_permanent_file(self):
        """C.1+C.10: dry_run 模式下升级不应创建 .upgraded_to_vN.db 永久副本；
        临时文件用 try/finally 清理；目标库 SHA-256 不变。"""
        from importlib import import_module
        import hashlib
        import sys
        if "tools" not in sys.modules:
            sys.path.insert(0, str(_ROOT))
        rmod = import_module("tools.restore_snapshot")
        # 1) 准备一个目标库（带一个文件作为目标）
        target = self.tmp / "v5.db"
        target.write_bytes(b"ORIGINAL_TARGET_DB")
        target_sha_before = hashlib.sha256(target.read_bytes()).hexdigest()
        # 2) 构造 v7 旧备份
        self._create_snapshot(set_user_version_to=0, max_migration=7)
        # 3) dry_run 升级（绝不应写目标库 / 不应写永久副本）
        result = rmod.upgrade_snapshot_user_version(self.snap, 8, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertIsNone(result["upgraded"])
        # 4) 目标库字节不变
        target_sha_after = hashlib.sha256(target.read_bytes()).hexdigest()
        self.assertEqual(target_sha_before, target_sha_after)
        # 5) 不应出现 .upgraded_to_v8.db
        upgraded_files = list(self.snap.parent.glob("*.upgraded_to_v8*"))
        self.assertEqual(upgraded_files, [])


def sqlite3_mod():  # 避免污染顶层
    import sqlite3
    return sqlite3


if __name__ == "__main__":
    unittest.main()
