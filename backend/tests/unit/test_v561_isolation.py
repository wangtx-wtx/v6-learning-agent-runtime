"""V5.6.1 测试环境硬隔离回归测试。

用独立 subprocess 验证 fail-closed（config 是模块级一次性导入，须在新解释器
设置不同 env 才有效），因此这里都以子进程方式断言：
- 缺 V5_TEST_DATA_ROOT → import 报错拒绝；
- 测试根位于正式 data 下 → 报错拒绝；
- 测试根 == 正式 v5.db → 报错拒绝；
- 正常测试根 → DB_PATH/UPLOAD/BACKUP/OBSIDIAN 都派生在临时根内，且与正式
  路径不同；
- 测试环境下 create_connection 直接连接正式库 → RuntimeError；
- backup / restore 指向正式库 → RuntimeError；
- 多进程锁子进程继承同一隔离环境，lock path 显式传递。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent.parent
PROD_DB = (_BACKEND / "data" / "v5.db").resolve()
PROD_DATA = (_BACKEND / "data").resolve()

_SNIPPET = r"""
import os, sys, json
sys.path.insert(0, {backend!r})
{body}
"""


def _run(body: str, env_extra: dict | None = None, timeout: float = 30):
    env = dict(os.environ)
    # 清掉可能遗留的测试 env，交给本用例控制
    env.pop("V5_ENV", None)
    env.pop("V5_TEST_DATA_ROOT", None)
    if env_extra:
        env.update(env_extra)
    code = _SNIPPET.format(backend=str(_BACKEND), body=body)
    # encoding='utf-8'：子进程中文报错在 cp1252 环境会被 UnicodeDecodeError 吞成 None
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       cwd=str(_BACKEND), env=env, timeout=timeout)
    return r


class TestIsolationFailClosed(unittest.TestCase):
    def test_missing_test_root_rejected(self):
        """V5_ENV=test 但缺 V5_TEST_DATA_ROOT → RuntimeError。"""
        r = _run("import app.config", env_extra={"V5_ENV": "test"})
        self.assertNotEqual(r.returncode, 0, "应失败")
        self.assertIn("V5_ENV=test 必须设置 V5_TEST_DATA_ROOT", r.stderr + r.stdout)

    def test_test_root_inside_prod_rejected(self):
        """V5_TEST_DATA_ROOT 位于正式 data 下 → 拒绝。"""
        r = _run("import app.config",
                 env_extra={"V5_ENV": "test", "V5_TEST_DATA_ROOT": str(PROD_DATA)})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("位于正式数据目录", r.stderr + r.stdout)

    def test_test_root_prod_db_rejected(self):
        """V5_TEST_DATA_ROOT == 正式 v5.db → 拒绝（经"位于正式 data 下"或"不能指向正式数据库"任一判断）。"""
        r = _run("import app.config",
                 env_extra={"V5_ENV": "test", "V5_TEST_DATA_ROOT": str(PROD_DB)})
        self.assertNotEqual(r.returncode, 0)
        combined = r.stderr + r.stdout
        self.assertTrue(("位于正式数据目录" in combined) or ("不能指向正式数据库" in combined))

    def test_normal_test_root_paths_isolated(self):
        """正常测试根：全部路径应派生在临时根内，且不同于正式。"""
        body = ("import app.config as c\n"
                "import json\n"
                "print(json.dumps({'DATA_DIR': str(c.DATA_DIR),\n"
                "  'DB_PATH': str(c.DB_PATH), 'UPLOAD': str(c.UPLOAD_DIR),\n"
                "  'BACKUP': str(c.BACKUP_DIR), 'OBSIDIAN': str(c.OBSIDIAN_VAULT_ROOT),\n"
                "  'ENV': c.ENV}))\n")
        with tempfile.TemporaryDirectory(prefix="v5_iso_") as td:
            r = _run(body, env_extra={"V5_ENV": "test", "V5_TEST_DATA_ROOT": td})
        self.assertEqual(r.returncode, 0, r.stderr)
        info = json.loads(r.stdout.strip().splitlines()[-1])
        root = Path(td).resolve()
        self.assertEqual(info["ENV"], "test")
        for k, v in [("DATA_DIR", "DATA_DIR"), ("DB_PATH", "DB_PATH"),
                     ("UPLOAD", "UPLOAD_DIR"), ("BACKUP", "BACKUP_DIR"),
                     ("OBSIDIAN", "OBSIDIAN_VAULT_ROOT")]:
            p = Path(info[k])
            self.assertTrue(p.is_absolute())
            try:
                p.relative_to(root)
            except ValueError:
                self.fail(f"{k}={p} 不在隔离根 {root} 下")
        # 关键：DB_PATH 不得等于正式库
        self.assertNotEqual(Path(info["DB_PATH"]).resolve(), PROD_DB)

    def test_create_connection_to_prod_rejected(self):
        """测试环境直接连接正式库 → RuntimeError。"""
        body = ("import app.config as c\n"
                "import app.database as db\n"
                "# 强行 override 到正式库（模拟误配）\n"
                "db.configure_db({prod!r})\n"
                "db.create_connection()\n"
                ).format(prod=str(PROD_DB))
        with tempfile.TemporaryDirectory(prefix="v5_iso_") as td:
            r = _run(body, env_extra={"V5_ENV": "test", "V5_TEST_DATA_ROOT": td})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("拒绝操作正式数据库", r.stderr + r.stdout)

    def test_backup_to_prod_db_rejected(self):
        body = ("import app.config as c\n"
                "import app.backup as b\n"
                "b.backup_database({prod!r})\n").format(prod=str(PROD_DB))
        with tempfile.TemporaryDirectory(prefix="v5_iso_") as td:
            r = _run(body, env_extra={"V5_ENV": "test", "V5_TEST_DATA_ROOT": td})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("拒绝", r.stderr + r.stdout)

    def test_restore_to_prod_rejected(self):
        # _assert_not_production 在 validate 之前执行，snapshot 无需合法
        body = ("import app.config as c\n"
                "from pathlib import Path\n"
                "from tools import restore_snapshot as rs\n"
                "rs.restore_snapshot(Path('__nonexistent_snap__'), db_path={prod!r})\n"
                ).format(prod=str(PROD_DB))
        with tempfile.TemporaryDirectory(prefix="v5_iso_") as td:
            r = _run(body, env_extra={"V5_ENV": "test", "V5_TEST_DATA_ROOT": td})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("拒绝恢复正式数据库", r.stderr + r.stdout)

    def test_upgrade_to_prod_db_rejected(self):
        """升级路径（restore_with_upgrade）目标指向正式库也要拒绝。"""
        body = ("import app.config as c\n"
                "from pathlib import Path\n"
                "from tools import restore_snapshot as rs\n"
                "rs.restore_with_upgrade(Path('__nonexistent_snap__'), db_path={prod!r})\n"
                ).format(prod=str(PROD_DB))
        with tempfile.TemporaryDirectory(prefix="v5_iso_") as td:
            r = _run(body, env_extra={"V5_ENV": "test", "V5_TEST_DATA_ROOT": td})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("拒绝恢复正式数据库", r.stderr + r.stdout)


if __name__ == "__main__":
    unittest.main()