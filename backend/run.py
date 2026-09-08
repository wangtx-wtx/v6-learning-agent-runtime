"""v5 后端入口

默认监听 0.0.0.0:8800,允许手机/局域网访问。
可通过环境变量 V5_HOST / V5_PORT 覆盖。
启动时打印连接信息横幅,包含 Tailscale Serve 一键启用提示。

V5.2:Windows cp1252 终端直跑会因中文横幅 UnicodeEncodeError 闪退,
横幅改用 ASCII + sys.stdout.reconfigure(encoding='utf-8', errors='replace') 兜底。
"""
import os
import shutil
import subprocess
import sys
import uvicorn

BANNER = """
============================================================
  v5 backend started: {host}:{port}
  - Local    : http://localhost:{port}/#/m-upload
  - LAN      : http://<your-LAN-IP>:{port}/#/m-upload
============================================================
  Remote (Tailscale Serve, recommended):
    1) PC   : tailscale up
    2) PC   : tailscale serve --bg {port}
    3) Phone: open https://<hostname>.<tailnet>.ts.net/#/m-upload
       (Phone needs Tailscale App + same account)
============================================================
  Public (Tailscale Funnel, requires token):
    - Set backend/.env: V5_MOBILE_TOKEN=<your-password>
    - PC: tailscale funnel --bg {port}
    - Anyone: open https://<hostname>.<tailnet>.ts.net/#/m-upload?token=<password>
============================================================
  Token page (localhost only): http://localhost:{port}/#/m-token
  - Rotate / set custom / reset - hot reload, no restart
============================================================
"""


def _safe_print(s: str) -> None:
    """兼容非 UTF-8 终端:错误字符替换为 ?,不抛 UnicodeEncodeError"""
    try:
        sys.stdout.buffer.write((s + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception:
        try:
            sys.stdout.write(s + "\n")
        except UnicodeEncodeError:
            sys.stdout.write(s.encode("ascii", errors="replace").decode("ascii") + "\n")


def _print_tailscale_hint(port: int) -> None:
    """如果 tailscale CLI 可用且已登录,打印实际的 ts.net URL。"""
    if not shutil.which("tailscale"):
        return
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=4,
        )
        if out.returncode != 0:
            return
        import json
        data = json.loads(out.stdout)
        dns = (data.get("Self", {}).get("DNSName") or "").rstrip(".")
        ip_list = data.get("Self", {}).get("TailscaleIPs") or []
        ip = next((i for i in ip_list if i.startswith("100.")), "")
        if dns or ip:
            _safe_print(f"  · Tailscale DNS : {dns or '(no MagicDNS)'}")
            _safe_print(f"  · Tailscale IP  : {ip}")
            if dns:
                _safe_print(f"  · Recommended URL: https://{dns}/#/m-upload")
    except Exception:
        pass


def _print_token_hint() -> None:
    """如已配置 V5_MOBILE_TOKEN,提示如何传递。"""
    token = os.environ.get("V5_MOBILE_TOKEN", "").strip()
    if not token:
        try:
            env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
            if os.path.exists(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("V5_MOBILE_TOKEN="):
                            token = line.split("=", 1)[1].strip().strip('"').strip("'")
                            break
        except Exception:
            pass
    if token:
        _safe_print("  · [AUTH] V5_MOBILE_TOKEN enabled, use ?token=xxx or x-app-token header")


if __name__ == "__main__":
    host = os.environ.get("V5_HOST", "0.0.0.0")
    port = int(os.environ.get("V5_PORT", "8800"))

    # V5.2:让 print() 在 cp1252 等非 UTF-8 终端也能安全打印中文
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass  # Python < 3.7 或被替换的 stdout

    _safe_print(BANNER.format(host=host, port=port))
    _print_token_hint()
    _print_tailscale_hint(port)
    _safe_print("")
    uvicorn.run("app.main:app", host=host, port=port, reload=True)