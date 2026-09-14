"""V5.6.4 官方隔离测试入口。

在独立临时数据根下运行全部后端测试，确保任何测试都**不读取/迁移/备份/写入**
正式数据库 ``backend/data/v5.db``，且**不发起真实网关请求**。

职责：
1. 创建独立临时数据根（V5_TEST_DATA_ROOT）。
2. 设置 V5_ENV=test + V5_TEST_DATA_ROOT。
3. 在子进程运行 ``tools/_run_unittest_zero_token.py``：
   - 子进程内强制 ``V5_GATEWAY_MODE=fake``、屏蔽 API Key；
   - 子进程内安装 httpx 熔断，按 (host, port) 拦截真实网关；
   - 子进程内跑 ``unittest discover``；
   - 任何真实访问 → 退出码 2，并写违规清单到 .zero_token_report.json。
4. 父脚本读 .zero_token_report.json 判定 violations + test result。
5. 记录正式库测试前后 SHA-256/LastWriteTime/Length；任一变化即使测试全通过也返回失败。
6. 测试结束清理临时目录 + 报告文件。
7. 正式备份目录在生产运行中不受影响。
"""
from __future__ import annotations

import argparse
import hashlib
import json
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


def _stat_triple(path: Path) -> tuple[str, float, int]:
    """SHA-256 / LastWriteTime / Length 三元组（V5.6.4 增强校验）。"""
    if not path.exists():
        return ("MISSING", 0.0, 0)
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        path.stat().st_mtime,
        path.stat().st_size,
    )


def _read_zero_token_report(path: Path) -> dict:
    if not path.exists():
        return {"violations": [{"error": "report_missing"}], "tests_run": 0,
                "failures": 0, "errors": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"violations": [{"error": f"report_unreadable: {e}"}],
                "tests_run": 0, "failures": 0, "errors": 0}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="在隔离临时数据根运行全部后端测试（防误碰正式库 + 拦截真实网关）"
    )
    parser.add_argument("--keep-tmp", action="store_true",
                        help="保留临时目录（调试用）")
    parser.add_argument("--no-env-required", action="store_true",
                        help="【仅供 CI 兼容】仍要求 V5_TEST_DATA_ROOT")
    args, unittest_argv = parser.parse_known_args()

    # 正式库三元组（前后对比：SHA-256 + LastWriteTime + Length）
    sha_before, mtime_before, len_before = _stat_triple(PROD_DB)
    print(
        f"[isolate] V5_ENV=test · 正式库 SHA={sha_before[:16]}… "
        f"mtime={mtime_before:.0f} length={len_before}",
        file=sys.stderr,
    )

    tmp = Path(tempfile.mkdtemp(prefix="v5_test_root_"))
    report_path = BACKEND_DIR / ".zero_token_report.json"
    print(f"[isolate] 测试数据根: {tmp}", file=sys.stderr)

    # 设置测试环境（子进程继承）
    env = dict(os.environ)
    env["V5_ENV"] = "test"
    env["V5_TEST_DATA_ROOT"] = str(tmp)
    env["V5_ZERO_TOKEN_REPORT"] = str(report_path)
    # Windows 重定向子进程 stderr 时可能采用本地代码页并把中文转成 \\uXXXX，
    # 会令安全测试的中文错误消息断言误判。统一强制 UTF-8，并传递给嵌套子进程。
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 强制测试根，禁止任何指向正式的后门
    env.pop("V5_DATA_ROOT", None)
    env.pop("V5_UPLOAD_DIR", None)
    env.pop("V5_BACKUP_DIR", None)
    env.pop("V5_OBSIDIAN_VAULT", None)
    env.pop("V5_QUARANTINE_DIR", None)
    # V5.6.4: 清掉可能干扰 fake 模式的 live 鉴权
    env.pop("V5_GATEWAY_API_KEY", None)
    env.pop("V5_ALLOW_LIVE_MODEL_TESTS", None)

    # V5.6.4: 子进程调 _run_unittest_zero_token.py（内部再调 unittest.discover）
    cmd = [sys.executable, str(BACKEND_DIR / "tools" / "_run_unittest_zero_token.py")]
    cmd += unittest_argv

    try:
        proc = subprocess.run(cmd, cwd=str(BACKEND_DIR), env=env)
        rc = proc.returncode
    finally:
        if not args.keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)
            print(f"[isolate] 已清理测试根 {tmp}", file=sys.stderr)

    # 读子进程写的报告
    report = _read_zero_token_report(report_path)
    violations = report.get("violations") or []
    tests_run = report.get("tests_run", 0)
    failures = report.get("failures", 0)
    errors = report.get("errors", 0)

    # 正式库三元组后置校验
    sha_after, mtime_after, len_after = _stat_triple(PROD_DB)
    print(
        f"[isolate] 测试后正式库 SHA={sha_after[:16]}… "
        f"mtime={mtime_after:.0f} length={len_after}",
        file=sys.stderr,
    )

    # 三元组任一变化 → 失败
    prod_unchanged = (
        sha_before == sha_after
        and mtime_before == mtime_after
        and len_before == len_after
    )

    failed = False
    if not prod_unchanged:
        print(
            f"[isolate] FAIL: 正式库在测试前后三元组不一致！\n"
            f"  before: SHA={sha_before[:16]} mtime={mtime_before:.0f} len={len_before}\n"
            f"  after : SHA={sha_after[:16]} mtime={mtime_after:.0f} len={len_after}\n"
            "  测试进程污染了正式数据库，返回失败。",
            file=sys.stderr,
        )
        failed = True

    if violations:
        print(
            f"[isolate] FAIL: 拦截到 {len(violations)} 次真实网关访问！",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        failed = True

    if rc != 0 and not failed:
        print(
            f"[isolate] FAIL: 测试子进程退出码 {rc}（tests={tests_run} "
            f"failures={failures} errors={errors}）",
            file=sys.stderr,
        )
        failed = True

    if not failed:
        print(
            f"[isolate] PASS: 隔离测试通过 "
            f"(tests={tests_run} failures={failures} errors={errors}, "
            f"真实网关访问=0, 正式库未变)",
            file=sys.stderr,
        )
        # 清报告文件，避免污染下次运行
        if report_path.exists():
            try:
                report_path.unlink()
            except Exception:
                pass
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
