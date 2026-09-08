"""
v5 后端主配置
"""
from pathlib import Path
import io
import os
import logging

logger = logging.getLogger(__name__)

# V5.2:项目根与配置目录,取代硬编码 D:\ 路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "v5.db"
KEYS_DIR = Path(os.environ.get(
    "V5_KEYS_DIR",
    PROJECT_ROOT / "data" / "keys",
))
KEYS_FILE = KEYS_DIR / "keys.dat"
DATA_DIR.mkdir(parents=True, exist_ok=True)

GATEWAY_BASE_URL = os.environ.get("V5_GATEWAY_URL", "http://127.0.0.1:8080")
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
GATEWAY_TIMEOUT = float(os.environ.get("V5_GATEWAY_TIMEOUT", "60"))

# Obsidian 同步目标（默认自动创建在 v5/data/obsidian_vault）
OBSIDIAN_VAULT_ROOT = Path(os.environ.get("V5_OBSIDIAN_VAULT", DATA_DIR / "obsidian_vault"))

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
    "critic": "qwen3_8_27b",
    "evidence_auditor": "qwen3_8_27b",
    "scope_auditor": "qwen3_8_27b",
    "solver": "deepseek_v4_free",
    "parallel_solver": "minimax_m3",
    "solution_explainer": "qwen3_8_27b",
    "error_analyst": "deepseek_v4_free",
    "review_writer": "deepseek_v4_free",
    "self_test_writer": "qwen3_flash",
    "vision_reader": "qwen3_vl",
    "transcriber_splitter": "qwen3_flash",
    "lesson_structurer": "qwen3_flash",
}
