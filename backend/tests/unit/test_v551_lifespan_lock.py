"""E.7/V5.6.1: 非 override 的 lifespan 启动测试（统一测试配置）。

V5.6.1 硬隔离：测试必须在 V5_ENV=test + V5_TEST_DATA_ROOT 下运行（由
``tools/run_tests_isolated.py`` 打包设置）。此时 config 已把 DATA_DIR/DB_PATH/
OBSIDIAN_VAULT_ROOT 全部指向临时根，本测试**不再**手工替换跨模块全局变量。

覆盖：
- 第一次 lifespan 启动成功（真实取锁，路径来自统一 config）
- 第二个实例立即失败（RuntimeError，不等第一实例退出）
- 正常退出（释放锁）后可重启
- diagnostics 非本机被拒（403 证明诊断不公开）
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi.testclient import TestClient  # noqa: E402


@unittest.skipUnless(
    os.environ.get("V5_ENV", "").strip().lower() == "test"
    and os.environ.get("V5_TEST_DATA_ROOT", "").strip(),
    "需 V5_ENV=test + V5_TEST_DATA_ROOT（请用 python tools/run_tests_isolated.py）",
)
class TestLifespanLockReal(unittest.TestCase):
    """用真实 FastAPI lifespan（非 override），路径来自统一隔离测试配置。"""

    def setUp(self):
        from app import config as _cfg
        self.data_root = Path(_cfg.DATA_DIR)
        self.db_path = Path(_cfg.DB_PATH)
        # 保证 is_override()=False（若前序测试残留 configure_db override 则清理）
        import app.database as database
        database._db_path_override = None
        database.reset_connections()
        from app import instance_lock  # noqa: F401

    def tearDown(self):
        import app.database as database
        database._db_path_override = None
        database.reset_connections()
        try:
            from app import instance_lock
            instance_lock.release_instance_lock()
            instance_lock._global_lock = None
        except Exception:
            pass

    def _lock_file(self):
        return self.data_root / ".instance.lock"

    def test_first_start_second_fails_third_succeeds(self):
        from app.main import app
        import app.database as database
        from app.database import is_override

        # 确认测试环境已隔离（非 override，路径指向临时根）
        self.assertFalse(is_override(), "lifespan 测试需非 override")
        self.assertEqual(self.data_root.resolve(),
                         Path(os.environ["V5_TEST_DATA_ROOT"]).resolve(),
                         "统一配置下 DATA_DIR 应等于 V5_TEST_DATA_ROOT")

        # ---- 第一次启动 ----
        with TestClient(app) as client:
            r = client.get("/api/health")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["version"], "6.0.0")
            self.assertTrue(self._lock_file().exists(),
                            "非 override 启动后锁文件应存在（临时根内）")
            from app.instance_lock import read_instance_lock
            li = read_instance_lock()
            self.assertIsNotNone(li)
            self.assertEqual(li["pid"], os.getpid())

            # 第二次实例：立即失败，不阻塞
            from app.instance_lock import acquire_instance_lock
            with self.assertRaises(RuntimeError):
                acquire_instance_lock()
            # 第一实例不受影响
            r2 = client.get("/api/health")
            self.assertEqual(r2.status_code, 200)

        # ---- 关闭后可重启 ----
        with TestClient(app) as client:
            r3 = client.get("/api/health")
            self.assertEqual(r3.status_code, 200)

    def test_diagnostics_nonlocal_forbidden(self):
        """diagnostics 必须无条件限制本机：TestClient 非本机 host → 403。"""
        from app.main import app
        with TestClient(app) as client:
            d = client.get("/api/admin/diagnostics")
            self.assertEqual(d.status_code, 403)


if __name__ == "__main__":
    unittest.main()
