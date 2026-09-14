"""
v5 后端主配置

V5.6.1 测试环境硬隔离：
- 运行环境 ``V5_ENV``: production | development | test（默认 development）。
- 测试模式（``V5_ENV=test``）必须提供独立临时根目录 ``V5_TEST_DATA_ROOT``，
  所有路径（db/uploads/backups/quarantine/obsidian_vault/.instance.lock）均
  由该根目录派生。**fail-closed**：缺根/根在正式 data 下/指向正式 v5.db → 直接
  RuntimeError，绝不回退到正式路径。
- 数据库、备份、上传、隔离区、锁文件、Obsidian 路径统一来自 ``DATA_DIR`` 单一根。
"""
from pathlib import Path
import io
import os
import logging

logger = logging.getLogger(__name__)

# V5.2:项目根与配置目录,取代硬编码 D:\ 路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BASE_DIR = Path(__file__).resolve().parent.parent
# 正式数据根（恒定，用于 test 模式的 fail-closed 校验）
PROD_DATA_DIR = (BASE_DIR / "data").resolve()
PROD_DB_PATH = PROD_DATA_DIR / "v5.db"
KEYS_DIR = Path(os.environ.get(
    "V5_KEYS_DIR",
    PROJECT_ROOT / "data" / "keys",
))
KEYS_FILE = KEYS_DIR / "keys.dat"

# ---------------------------------------------------------------------------
# 运行环境解析（V5.6.1）
# ---------------------------------------------------------------------------
ENV = os.environ.get("V5_ENV", "development").strip().lower()
if ENV not in ("production", "development", "test"):
    raise RuntimeError(f"无效 V5_ENV='{ENV}'，允许: production|development|test")


def _test_data_root() -> Path:
    """解析并校验测试数据根（V5_ENV=test 时调用）。"""
    raw = os.environ.get("V5_TEST_DATA_ROOT", "").strip()
    if not raw:
        raise RuntimeError(
            "V5_ENV=test 必须设置 V5_TEST_DATA_ROOT=<绝对临时目录>"
        )
    root = Path(raw).resolve()
    # 禁止指向正式数据目录或其子目录
    try:
        root.relative_to(PROD_DATA_DIR)
    except ValueError:
        pass
    else:
        raise RuntimeError(
            f"V5_TEST_DATA_ROOT={root} 位于正式数据目录 {PROD_DATA_DIR} 下，已拒绝"
        )
    # 禁止直接指向正式 v5.db
    if root == PROD_DB_PATH:
        raise RuntimeError(f"V5_TEST_DATA_ROOT 不能指向正式数据库 {PROD_DB_PATH}")
    return root


if ENV == "test":
    DATA_DIR = _test_data_root()
    # 测试模式：忽略单独路径 env，全部派生自 DATA_DIR（统一单一根）
    DB_PATH = DATA_DIR / "v5.db"
    UPLOAD_DIR = DATA_DIR / "uploads"
    BACKUP_DIR = DATA_DIR / "backups"
    QUARANTINE_DIR = DATA_DIR / "quarantine"
    OBSIDIAN_VAULT_ROOT = DATA_DIR / "obsidian_vault"
    ARTIFACTS_DIR = DATA_DIR / "artifacts"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
else:
    # development / production：兼容旧 V5_DATA_ROOT（默认正式 backend/data）
    DATA_DIR = Path(os.environ.get("V5_DATA_ROOT", str(PROD_DATA_DIR))).resolve()
    DB_PATH = DATA_DIR / "v5.db"
    UPLOAD_DIR = Path(os.environ.get("V5_UPLOAD_DIR", str(DATA_DIR / "uploads"))).resolve()
    BACKUP_DIR = Path(os.environ.get("V5_BACKUP_DIR", str(DATA_DIR / "backups"))).resolve()
    QUARANTINE_DIR = Path(os.environ.get("V5_QUARANTINE_DIR", str(DATA_DIR / "quarantine"))).resolve()
    OBSIDIAN_VAULT_ROOT = Path(os.environ.get("V5_OBSIDIAN_VAULT", str(DATA_DIR / "obsidian_vault"))).resolve()
    ARTIFACTS_DIR = Path(os.environ.get("V5_ARTIFACTS_DIR", str(DATA_DIR / "artifacts"))).resolve()
    for _d in (DATA_DIR, UPLOAD_DIR, BACKUP_DIR, QUARANTINE_DIR, OBSIDIAN_VAULT_ROOT, ARTIFACTS_DIR):
        _d.mkdir(parents=True, exist_ok=True)

ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


def assert_not_production(path: str | Path | None = None,
                          test_only: bool = True) -> None:
    """V5.6.1 最终防线：测试进程中指向正式 v5.db 立即抛错。

    ``path`` 显式给定时检查该路径（backup/restore 传入目标库）；
    否则检查当前活动数据库（database 写入口）。开发/生产模式不拦截。
    """
    if test_only and ENV != "test":
        return
    target = path if path is not None else _active_db_guard()
    if Path(target).resolve() == PROD_DB_PATH:
        raise RuntimeError(
            f"拒绝操作正式数据库 {PROD_DB_PATH}（当前 V5_ENV={ENV}）。"
            "测试进程必须使用独立临时目录。"
        )


def _active_db_guard() -> Path:
    """延迟获取 database 活动库路径（避免 config import 期循环引用）。"""
    from .database import _active_db_path
    return _active_db_path()

GATEWAY_BASE_URL = os.environ.get("V5_GATEWAY_URL", "http://127.0.0.1:8317")
# Unified API Gateway 鉴权 Key（自动读取 .env 或环境变量）
def _load_env_file():
    """读取 backend/.env 文件（如果存在），返回 dict"""
    import os
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    result = {}
    if not os.path.exists(env_path):
        return result
    try:
        with io.open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip().strip(chr(34) + chr(39) + chr(32))
    except Exception:
        pass
    return result


def _auto_detect_gateway_key() -> str:
    """读取 Unified API Gateway 统一鉴权 key"""
    _env = _load_env_file()
    # 1. 环境变量优先
    val = os.environ.get("V5_GATEWAY_API_KEY", "").strip()
    if val:
        return val
    # 2. .env 文件
    val = _env.get("V5_GATEWAY_API_KEY", "").strip()
    if val:
        return val
    # 3. 直接探测网关 keys.dat（PowerShell 解密，失败则返回空）
    import subprocess, tempfile, os.path as op
    # V5.2:支持多个候选位置,优先级:env 配置 > 项目 data/keys > 历史兜底
    candidates = [
        str(KEYS_FILE),  # V5_KEYS_DIR 路径(默认 PROJECT_ROOT/data/keys/)
    ]
    for kp in candidates:
        if not op.exists(kp):
            continue
        try:
            ps_body = (
                "$keysPath = '"
                + kp.replace("'", "''")
                + "'; "
                + "$s = ConvertTo-SecureString (Get-Content -LiteralPath $keysPath -Raw).Trim(); "
                + "$b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); "
                + "try { $keys = [Runtime.InteropServices.Marshal]::PtrToStringAuto($b) | ConvertFrom-Json } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }; "
                + "Write-Output $keys.gateway"
            )
            tmp_ps = tempfile.mktemp(suffix=".ps1")
            with io.open(tmp_ps, "w", encoding="utf-8-sig") as f2:
                f2.write(ps_body)
            try:
                out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tmp_ps], capture_output=True, text=True, timeout=10)
                if out.returncode == 0:
                    lines = out.stdout.strip().splitlines()
                    if lines:
                        key = lines[-1].strip()
                        if key:
                            return key
            finally:
                os.remove(tmp_ps)
        except Exception:
            continue
    return ""

GATEWAY_API_KEY = _auto_detect_gateway_key()
# 长文审查（critic）等节点单次生成常超过 60s；read 超时过短会导致
# “重试 3 次仍失败”且错误消息为空的假故障。默认放宽到 180s。
GATEWAY_TIMEOUT = float(os.environ.get("V5_GATEWAY_TIMEOUT", "180"))

# V5.6.1: 路径统一由上方 ENV 分支派生；DATA_ROOT 作为受控根别名保留（== DATA_DIR）
DATA_ROOT = DATA_DIR

# 移动端 / Funnel 模式鉴权 Token（设置后启用；空则不校验）
_env = _load_env_file()
MOBILE_TOKEN = (
    os.environ.get("V5_MOBILE_TOKEN", "").strip()
    or _env.get("V5_MOBILE_TOKEN", "").strip()
)
# 默认前端端口(Vite dev server),可在 .env 用 VITE_PORT 覆盖
FRONTEND_PORT = int(os.environ.get("VITE_PORT", _env.get("VITE_PORT", "5173")))


def set_mobile_token(value: str) -> None:
    """运行时热更新移动端 Token，无需重启后端。"""
    global MOBILE_TOKEN
    MOBILE_TOKEN = (value or "").strip()

# 每个工作流的默认模型选择
DEFAULT_ROUTE = {
    "student_simulator": "deepseek_v4_free",
    "note_writer": "deepseek_v4_free",
    "critic": "glm_flash",
    "evidence_auditor": "glm_flash",
    "scope_auditor": "glm_flash",
    "solver": "deepseek_v4_free",
    "parallel_solver": "glm_flash",
    "solution_explainer": "glm_flash",
    "error_analyst": "deepseek_v4_free",
    "review_writer": "deepseek_v4_free",
    "self_test_writer": "qwen3_flash",
    "vision_reader": "qwen3_flash",
    "transcriber_splitter": "qwen3_flash",
    "lesson_structurer": "qwen3_flash",
    # V6 Phase 2：分段理解角色
    "segment_understanding": "qwen3_flash",
}
