"""V5.6.1 官方隔离测试入口。

在独立临时数据根下运行全部后端测试，确保任何测试都**不读取/迁移/备份/写入**
正式数据库 ``backend/data/v5.db``。

职责：
1. 创建独立临时数据根（V5_TEST_DATA_ROOT）。
2. 设置 V5_ENV=test + V5_TEST_DATA_ROOT。
3. 在子进程运行 ``unittest discover``（envin 传给多进程 spawn 子测试）。
4. 记录正式库测试前后 SHA-256；任何变化即使测试全通过也返回失败。
5. 测试结束清理临时目录。
6. 正式备份目录在生产运行中不受影响。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROD_DB = (BACKEND_DIR / "data" / "v5.db").resolve()


def _sha256(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="在隔离临时数据根运行全部后端测试（防误碰正式库）"
    )
    parser.add_argument("--keep-tmp", action="store_true",
                        help="保留临时目录（调试用）")
    parser.add_argument("--no-env-required", action="store_true",
                        help="【仅供 CI 兼容】仍要求 V5_TEST_DATA_ROOT")
    args, unittest_argv = parser.parse_known_args()

    # 正式库前后 SHA
    sha_before = _sha256(PROD_DB)
    print(f"[isolate] V5_ENV=test · 正式库 SHA={sha_before[:16]}…",
          file=sys.stderr)

    tmp = Path(tempfile.mkdtemp(prefix="v5_test_root_"))
    print(f"[isolate] 测试数据根: {tmp}", file=sys.stderr)

    # 设置测试环境（子进程继承）
    env = dict(os.environ)
    env["V5_ENV"] = "test"
    env["V5_TEST_DATA_ROOT"] = str(tmp)
    # 强制测试根，禁止任何指向正式的后门
    env.pop("V5_DATA_ROOT", None)
    env.pop("V5_UPLOAD_DIR", None)
    env.pop("V5_BACKUP_DIR", None)
    env.pop("V5_OBSIDIAN_VAULT", None)
    env.pop("V5_QUARANTINE_DIR", None)

    cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
    cmd += unittest_argv

    try:
        proc = subprocess.run(cmd, cwd=str(BACKEND_DIR), env=env)
        rc = proc.returncode
    finally:
        if not args.keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)
            print(f"[isolate] 已清理测试根 {tmp}", file=sys.stderr)

    sha_after = _sha256(PROD_DB)
    print(f"[isolate] 测试后正式库 SHA={sha_after[:16]}…", file=sys.stderr)

    if sha_before != sha_after:
        print(
            f"[isolate] FAIL: 正式库在测试前后哈希不一致！\n"
            f"  before={sha_before}\n  after ={sha_after}\n"
            "  测试进程污染了正式数据库，返回失败。",
            file=sys.stderr,
        )
        return 1

    if rc != 0:
        print(f"[isolate] FAIL: 测试子进程退出码 {rc}", file=sys.stderr)
        return rc

    print("[isolate] PASS: 隔离测试通过，正式库未被修改", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())