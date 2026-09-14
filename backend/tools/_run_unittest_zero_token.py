"""V5.6.4 隔离测试子进程入口。

在 unittest 子进程内（不是父脚本）：

1. 强制 ``V5_GATEWAY_MODE=fake``，屏蔽 ``V5_GATEWAY_API_KEY``。
2. 安装 httpx 熔断（异步 + 同步），按 (host, port) 元组匹配禁用 origin。
3. 跑 ``unittest discover``；同时透传任何父进程传下来的额外 unittest 参数。
4. 任何一次真实网关访问 → 写违规清单 + 退出码 2。
5. 写报告文件 ``.zero_token_report.json``，父脚本读它做最终判定。

TestClient 走 starlette 内置 ASGI transport，根本不进 ``httpx.send``，不会被熔断；
真实 httpx 调用一旦命中禁用 origin 立即抛 RuntimeError。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from urllib.parse import urlparse

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(str(BACKEND_DIR))


# ---------------------------------------------------------------------------
# 1) 强制 fake + 清 Key（必须发生在 import app.gateway 之前）
# ---------------------------------------------------------------------------
os.environ["V5_GATEWAY_MODE"] = "fake"
os.environ.pop("V5_GATEWAY_API_KEY", None)
os.environ.pop("V5_ALLOW_LIVE_MODEL_TESTS", None)
ORIG_URL = os.environ.get("V5_GATEWAY_URL", "http://127.0.0.1:8317")
os.environ["V5_GATEWAY_URL"] = ORIG_URL  # 占位；fake 不会触碰


# ---------------------------------------------------------------------------
# 2) 解析禁用 origin（host + port 元组）
# ---------------------------------------------------------------------------
def _origin(url: str) -> tuple[str, int]:
    u = urlparse(url)
    return (
        u.hostname or "",
        u.port or (443 if u.scheme == "https" else 80),
    )


FORBIDDEN: set[tuple[str, int]] = {
    _origin("http://127.0.0.1:8080"),
    _origin("http://localhost:8080"),
    _origin("http://127.0.0.1:8317"),
    _origin("http://localhost:8317"),
}
# 显式禁用原 V5_GATEWAY_URL（即使被改写）
FORBIDDEN.add(_origin(ORIG_URL))
VIOLATIONS: list[dict] = []


def _is_forbidden(url) -> bool:
    """适配 httpx.URL（httpx>=0.23 有 host/port 属性）和字符串。"""
    if isinstance(url, str):
        host, port = _origin(url)
    else:
        host = getattr(url, "host", "")
        port = getattr(url, "port", None) or (
            443 if getattr(url, "scheme", "") == "https" else 80
        )
    return (host, port) in FORBIDDEN


# ---------------------------------------------------------------------------
# 3) 装 httpx 熔断（在 import app.gateway 之前）
# ---------------------------------------------------------------------------
import httpx  # noqa: E402

_orig_async = httpx.AsyncClient.send
_orig_sync = httpx.Client.send


async def _safe_async(self, request, *args, **kwargs):
    if _is_forbidden(request.url):
        VIOLATIONS.append({"kind": "async", "url": str(request.url)})
        raise RuntimeError(
            f"[zero-token] 测试中禁止访问真实网关: {request.url}"
        )
    return await _orig_async(self, request, *args, **kwargs)


def _safe_sync(self, request, *args, **kwargs):
    if _is_forbidden(request.url):
        VIOLATIONS.append({"kind": "sync", "url": str(request.url)})
        raise RuntimeError(
            f"[zero-token] 测试中禁止访问真实网关: {request.url}"
        )
    return _orig_sync(self, request, *args, **kwargs)


httpx.AsyncClient.send = _safe_async
httpx.Client.send = _safe_sync


# ---------------------------------------------------------------------------
# 4) 跑 unittest.discover（透传任何额外参数）
# ---------------------------------------------------------------------------
def _normalize_argv():
    """透传父进程传的 unittest 参数（去掉脚本名本身）。"""
    # _run_unittest_zero_token.py 自身可能被当成 argv[0]；跳过它。
    extra = sys.argv[1:]
    # 如果用户传的是 -v / --verbose 等 unittest 参数，原样保留
    return extra


def main() -> int:
    """运行测试并写零 Token 报告。

    必须只在脚本直接执行时调用；测试导入本模块仅检查网络拦截配置，
    不能递归启动整个测试集。
    """
    print(
        f"[zero-token] forbidden origins = {sorted(FORBIDDEN)}",
        file=sys.stderr,
    )
    extra_argv = _normalize_argv()
    sys.argv = [sys.argv[0]] + list(extra_argv)

    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=str(BACKEND_DIR / "tests"),
        top_level_dir=str(BACKEND_DIR),
    )
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    report = {
        "violations": VIOLATIONS,
        "test_was_successful": result.wasSuccessful(),
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
    }
    report_path = Path(
        os.environ.get(
            "V5_ZERO_TOKEN_REPORT",
            str(BACKEND_DIR / ".zero_token_report.json"),
        )
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if VIOLATIONS:
        print(
            f"[zero-token] FAIL: 拦截到 {len(VIOLATIONS)} 次真实网关访问：",
            file=sys.stderr,
        )
        for violation in VIOLATIONS:
            print(
                f"  - {violation['kind']}: {violation['url']}",
                file=sys.stderr,
            )
        return 2
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
