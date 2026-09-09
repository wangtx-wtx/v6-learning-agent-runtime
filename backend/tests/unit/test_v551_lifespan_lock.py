"""E.7: 非 override 的 lifespan 启动测试。

验证真实调用单实例锁（不再出现 force 参数签名错误）：
- 第一次 lifespan 启动成功（真实取锁）；
- 第二个实例继续启动立即失败（RuntimeError，不等第一实例退出）；
- 第一个实例正常退出（释放锁）后可再次启动。

**测试隔离**：把 app.config 与各模块引用的 DATA_DIR/DB_PATH/备份/上传/vault
全部 patch 到临时目录，**绝不触碰正式库**。
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


class TestLifespanLockReal(unittest.TestCase):
    """用真实 FastAPI lifespan（非 override），数据库/锁指向临时目录。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        ub = self.tmp / "uploads"; ob = self.tmp / "obsidian_vault"
        bk = self.tmp / "backups"
        for d in (ub, ob, bk):
            d.mkdir(parents=True, exist_ok=True)

        # 1) 加载 app 包（若未加载）
        import app.config as config
        import app.database as database
        import app.main as app_main
        import app.backup as backup
        import app.obsidian as obsidian
        from app import instance_lock

        # 2) 暂存原始值以便还原
        self._orig = {}
        for mod, attr in [
            (config, "DATA_DIR"), (config, "DB_PATH"),
            (config, "DATA_ROOT"), (config, "UPLOAD_DIR"),
            (config, "BACKUP_DIR"), (config, "OBSIDIAN_VAULT_ROOT"),
            (database, "DB_PATH"), (database, "DATA_DIR"),
            (backup, "DB_PATH"), (backup, "DATA_DIR"),
            (obsidian, "OBSIDIAN_VAULT_ROOT"),
        ]:
            self._orig[(id(mod), attr)] = getattr(mod, attr)

        # 3) patch 到 tmp
        tmp_db = self.tmp / "v5.db"
        config.DATA_DIR = self.tmp
        config.DB_PATH = tmp_db
        config.DATA_ROOT = self.tmp
        config.UPLOAD_DIR = ub
        config.BACKUP_DIR = bk
        config.OBSIDIAN_VAULT_ROOT = ob
        database.DB_PATH = tmp_db
        database.DATA_DIR = self.tmp
        backup.DB_PATH = tmp_db
        backup.DATA_DIR = self.tmp
        obsidian.OBSIDIAN_VAULT_ROOT = ob
        # 关键：清除其它测试 configure_db 可能残留的 override，使 is_override()=False
        self._orig_override = database._db_path_override
        database._db_path_override = None
        # 锁路径指向 tmp
        self._orig_lock_path = instance_lock._lock_path
        instance_lock._lock_path = lambda: self.tmp / ".instance.lock"
        # 清理真实库可能的线程残留连接
        database.reset_connections()

        self.app_main = app_main
        self.instance_lock = instance_lock

    def tearDown(self):
        # 释放锁 + 还原
        try:
            self.instance_lock.release_instance_lock()
            self.instance_lock._global_lock = None
            self.instance_lock._lock_path = self._orig_lock_path
        except Exception:
            pass
        import app.config as config
        import app.database as database
        import app.backup as backup
        import app.obsidian as obsidian
        config.DATA_DIR = self._orig[(id(config), "DATA_DIR")]
        config.DB_PATH = self._orig[(id(config), "DB_PATH")]
        config.DATA_ROOT = self._orig[(id(config), "DATA_ROOT")]
        config.UPLOAD_DIR = self._orig[(id(config), "UPLOAD_DIR")]
        config.BACKUP_DIR = self._orig[(id(config), "BACKUP_DIR")]
        config.OBSIDIAN_VAULT_ROOT = self._orig[(id(config), "OBSIDIAN_VAULT_ROOT")]
        database.DB_PATH = self._orig[(id(database), "DB_PATH")]
        database.DATA_DIR = self._orig[(id(database), "DATA_DIR")]
        backup.DB_PATH = self._orig[(id(backup), "DB_PATH")]
        backup.DATA_DIR = self._orig[(id(backup), "DATA_DIR")]
        obsidian.OBSIDIAN_VAULT_ROOT = self._orig[(id(obsidian), "OBSIDIAN_VAULT_ROOT")]
        database._db_path_override = getattr(self, "_orig_override", None)
        database.reset_connections()
        # 清理 tmp
        try:
            for p in self.tmp.rglob("*"):
                if p.is_file():
                    try: p.unlink()
                    except Exception: pass
            for d in sorted([d for d in self.tmp.rglob("*") if d.is_dir()],
                            reverse=True):
                try: d.rmdir()
                except Exception: pass
            self.tmp.rmdir()
        except Exception:
            pass

    def _lock_file(self):
        return self.tmp / ".instance.lock"

    def test_first_start_second_fails_third_succeeds(self):
        # ---- 第一次启动：应成功（真实生命周期取锁）----
        app = self.app_main.app
        with TestClient(app) as client:
            r = client.get("/api/health")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["version"], "5.5.1")
            self.assertTrue(self._lock_file().exists(),
                            "非 override 启动后锁文件应存在")
            from app.instance_lock import read_instance_lock
            li = read_instance_lock()
            self.assertIsNotNone(li, "lifespan 应写入锁诊断")
            self.assertEqual(li["pid"], os.getpid())

            # ---- 第二次实例：持锁期间继续启动应立即失败 ----
            # 非 override 下同一进程重复 acquire 抛 RuntimeError（模拟第二实例
            # 立即失败，不阻塞等待第一实例退出）。
            from app.instance_lock import acquire_instance_lock
            with self.assertRaises(RuntimeError):
                acquire_instance_lock()
            # 验证第一次 TestClient 仍正常（第一实例未被扰动）
            r2 = client.get("/api/health")
            self.assertEqual(r2.status_code, 200)

        # ---- 正常退出：TestClient with 结束触发 lifespan shutdown → release ----
        # ---- 第三次启动（重启）：应成功 ----
        with TestClient(app) as client:
            r3 = client.get("/api/health")
            self.assertEqual(r3.status_code, 200)


if __name__ == "__main__":
    unittest.main()