"""
封装 Tailscale CLI 调用,供 mobile_server_info 接口使用。
通过 subprocess 调用 `tailscale status / ip / serve / funnel` 获取网络信息。
任何调用失败均返回安全的默认值,不阻塞后端启动。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import shutil
import subprocess
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CLI_TIMEOUT = 4  # 秒


def _run(args: list[str]) -> Optional[str]:
    """执行 CLI,返回 stdout;失败/超时/不存在返回 None。"""
    if not shutil.which(args[0]):
        return None
    try:
        out = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
        if out.returncode == 0:
            return out.stdout
    except (subprocess.TimeoutExpired, OSError, Exception) as e:
        logger.debug("tailscale %s 失败: %s", args[1] if len(args) > 1 else "?", e)
    return None


def _first_url(obj: Any, prefix: str = "https://") -> Optional[str]:
    """从 serve/funnel 状态 JSON 中抽取第一个 Web URL。

    Tailscale CLI 在不同版本中的输出格式不同:
    - 旧版: {"Web": {"https://host.tailnet.ts.net": {...}}}
    - 新版: {"Web": {"host.tailnet.ts.net:443": {...}}}
    这里同时兼容带 https:// 前缀和裸域名:443 两种形式。
    """
    web = obj.get("Web") if isinstance(obj, dict) else None
    if isinstance(web, dict) and web:
        for key in web.keys():
            if not isinstance(key, str) or not key:
                continue
            if key.startswith(prefix):
                return key
            # 新格式如 "host.tailnet.ts.net:443" 或 "host.tailc03ef9.ts.net:443"
            lowered = key.lower()
            if (
                ":" in lowered
                and lowered.startswith("http") is False
                and (".ts.net" in lowered or ".ts." in lowered or "tail" in lowered)
            ):
                return key
    return None


def _normalize_https_url(url: str) -> str:
    """把裸域名:port 或 https:// 开头的 URL 标准化为 https:// 形式。"""
    cleaned = url.strip().rstrip("/")
    # 去掉 https:// 前缀
    if cleaned.startswith("https://"):
        cleaned = cleaned[len("https://"):]
    # 去掉默认端口 :443(HTTPS 默认端口,保留会让链接显得冗余)
    if cleaned.endswith(":443"):
        cleaned = cleaned[:-4]
    return "https://" + cleaned
def get_tailscale_info() -> dict[str, Any]:
    """
    综合查询 Tailscale 状态,返回:
    - available: 是否是否在线
    - state: tailscale 状态字符串
    - ip: 100.x 私网 IP
    - dns_name: 形如 host.tailnet-xxx.ts.net
    - online: 设备在线
    - serve_url: 已配置的 serve HTTPS URL(含 /#/m-upload)
    - funnel_url: 已配置的 funnel HTTPS URL
    - recommended_url: 优先级: serve_url > funnel_url > http://ip:5173/#/m-upload
    """
    info: dict[str, Any] = {
        "available": False,
        "state": "unknown",
        "online": False,
        "ip": None,
        "dns_name": None,
        "serve_url": None,
        "funnel_url": None,
        "recommended_url": None,
    }

    # 1) tailscale status --json
    status_json = _run(["tailscale", "status", "--json"])
    if status_json:
        try:
            data = json.loads(status_json)
            info["state"] = data.get("BackendState") or data.get("State") or "unknown"
            self_info = data.get("Self") or {}
            dns = (self_info.get("DNSName") or "").rstrip(".")
            if dns:
                info["dns_name"] = dns
            info["online"] = bool(self_info.get("Online"))
        except (json.JSONDecodeError, TypeError):
            pass

    # 2) tailscale ip -4
    ip_text = _run(["tailscale", "ip", "-4"])
    if ip_text:
        for line in ip_text.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            try:
                addr = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            # Tailscale 默认网段为 100.64.0.0/10;Python ipaddress 不把它标记为
            # is_private(它是共享地址空间),这里显式判断前缀。
            is_tailscale_ip = (
                isinstance(addr, ipaddress.IPv4Address)
                and int(addr) >= int(ipaddress.ip_address("100.64.0.0"))
                and int(addr) <= int(ipaddress.ip_address("100.127.255.255"))
            )
            if addr.is_private or is_tailscale_ip:
                info["ip"] = candidate
                info["available"] = True
                break

    # 3) tailscale serve status --json
    serve_json = _run(["tailscale", "serve", "status", "--json"])
    if serve_json:
        try:
            data = json.loads(serve_json)
            url = _first_url(data, "https://")
            if url:
                info["serve_url"] = _normalize_https_url(url) + "/#/m-upload"
        except (json.JSONDecodeError, TypeError):
            pass

    # 4) tailscale funnel status --json
    funnel_json = _run(["tailscale", "funnel", "status", "--json"])
    if funnel_json:
        # 当服务器输出 "tailnet only" 时代表 Funnel 只对内网开放,并未正式公网发布,
        # 仍当作 tailscale URL 而非"公网 Funnel 地址"。
        funnel_text = _run(["tailscale", "funnel", "status"])
        funnel_public = bool(funnel_text) and "tailnet only" not in (funnel_text or "").lower()
        try:
            data = json.loads(funnel_json)
            url = _first_url(data, "https://")
            if url and funnel_public:
                info["funnel_url"] = _normalize_https_url(url) + "/#/m-upload"
        except (json.JSONDecodeError, TypeError):
            pass

    # 5) 构造 recommended_url
    if info["serve_url"]:
        info["recommended_url"] = info["serve_url"]
    elif info["funnel_url"]:
        info["recommended_url"] = info["funnel_url"]
    elif info["dns_name"]:
        info["recommended_url"] = f"https://{info['dns_name']}/#/m-upload"
    elif info["ip"]:
        # Fallback: 假设用户用 vite dev 模式,5173 端口
        info["recommended_url"] = f"http://{info['ip']}:5173/#/m-upload"

    return info





