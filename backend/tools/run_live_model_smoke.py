"""V5.6.4 人工 live 模型冒烟入口。

四鉴权：
  V5_ENV                  必须 development
  V5_GATEWAY_MODE         必须 live
  V5_ALLOW_LIVE_MODEL_TESTS 必须 1
  V5_GATEWAY_API_KEY      必须存在

临时根：
  V5_DATA_ROOT = mkdtemp()；启动后立即断言 DB_PATH 在临时根内且 ≠ PROD_DB。

预算预检（来自 gateway_mode.reserve_budget）：
  V5_LIVE_MAX_CALLS / V5_LIVE_TOKEN_BUDGET_TOTAL / V5_LIVE_MAX_INPUT_CHARS
  任何一项超限 → 立即拒绝后续调用。

不会自动运行、不属于 unittest discover、不属于 CI。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _require_auth() -> None:
    """4 项鉴权链：任一不满足立即 fail，不显示任何指引性信息。"""
    if os.environ.get("V5_ENV", "").strip() != "development":
        sys.exit("[FAIL] V5_ENV 必须显式为 development")
    if os.environ.get("V5_GATEWAY_MODE", "").strip() != "live":
        sys.exit("[FAIL] V5_GATEWAY_MODE 必须为 live")
    if os.environ.get("V5_ALLOW_LIVE_MODEL_TESTS", "").strip() != "1":
        sys.exit("[FAIL] 必须显式设 V5_ALLOW_LIVE_MODEL_TESTS=1")
    if not os.environ.get("V5_GATEWAY_API_KEY", "").strip():
        sys.exit("[FAIL] 必须设 V5_GATEWAY_API_KEY")


def _snippet(tmp_dir: str) -> str:
    """嵌入到子进程执行的 Python 代码段。

    启动后必须立即断言：
      1) config.DB_PATH 在 tmp_dir 内
      2) config.DB_PATH ≠ config.PROD_DB_PATH
    """
    return f"""
import sys, os, json, asyncio, pathlib
sys.path.insert(0, {str(BACKEND_DIR)!r})
from app import config
from app import database as db

td = pathlib.Path({tmp_dir!r}).resolve()
assert config.DB_PATH.resolve().is_relative_to(td), \\
    f"DB_PATH {{config.DB_PATH.resolve()}} 不在临时根 {{td}}"
assert config.DB_PATH.resolve() != config.PROD_DB_PATH, \\
    "实际 DB 仍指向正式库"
db.configure_db(config.DB_PATH)
db.init_db()

from app.gateway import gateway
from app.gateway_mode import snapshot, reserve_budget, settle


async def go():
    s0 = snapshot()
    print("snapshot_before=", json.dumps(s0, ensure_ascii=False))
    # 1) 最小业务调用：fast 模型 + max_tokens=4；立即触发预算预检
    r = await gateway.chat(
        "qwen3_flash",
        [{{"role": "user", "content": "echo"}}],
        contract="homework/solver",
        temperature=0.0,
        max_tokens=4,
    )
    print("chat=", json.dumps(
        {{"tokens_in": r.get("tokens_in", 0),
          "tokens_out": r.get("tokens_out", 0),
          "model": r.get("model", "")}},
        ensure_ascii=False))
    s1 = snapshot()
    print("snapshot_after=", json.dumps(s1, ensure_ascii=False))

asyncio.run(go())
"""


def main() -> int:
    _require_auth()

    tmp = Path(tempfile.mkdtemp(prefix="v5_live_smoke_"))
    try:
        env = dict(os.environ)
        # 用 V5_DATA_ROOT 派生整个数据根（DB/backup/vault/uploads）
        env["V5_DATA_ROOT"] = str(tmp)
        # 同时强制去掉可能干扰 test 模式的变量
        env.pop("V5_TEST_DATA_ROOT", None)
        env.pop("V5_ENV", None)  # 让子进程重新解析为 development（用户已鉴权）

        code = _snippet(str(tmp))
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
