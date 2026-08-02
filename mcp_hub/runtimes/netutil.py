"""Netutil —— 本地 HTTP 代理自动探测与注入。

某些美国服务的 CLI（Antigravity / Grok Build 等）不会自动走 Windows 系统代理，
需要显式注入 HTTP_PROXY/HTTPS_PROXY 环境变量。本模块提供：

    proxied_env() -> dict[str, str]   # 返回注入好代理的完整环境

探测顺序：显式配置(ANTIGRAVITY_PROXY) > 环境变量 > Windows 系统代理 > 常见本地端口。
探测结果有缓存（_ProxyCache），不会每次 spawn 都重复探测。

从 antigravity.py 抽出来的通用设施（2026-08-02），供所有需要代理的 adapter 共用。
"""

from __future__ import annotations

import os
import urllib.parse

# 常见本地代理端口（按优先级）
_COMMON_PROXY_PORTS = [
    17891,  # Antigravity 默认端口
    7890,
    7897,
    10808,
    1080,
    8080,
    8888,
    8889,
    2017,
    20171,
    2022,
    8118,
    10080,
    9910,
    7891,
    9090,
]

# 这些进程名开头的端口不是 Antigravity，要跳过（避免误把云盘/下载器当代理）
_NON_ANTIGRAVITY_PROCESSES: list[str] = []

# 探测目标：Google 账号，能返回 401/403 即认为代理可用
_PROXY_PROBE_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
_PROXY_PROBE_TIMEOUT = 4.0
_PROXY_SOCKET_TIMEOUT = 1.0


def _port_owner_process_name(host: str, port: int) -> str | None:
    """查某个本地端口被哪个进程持有（Windows netstat + tasklist）。非 Windows 返回 None。"""
    try:
        import subprocess
        # netstat -ano 输出格式：TCP  127.0.0.1:port  0.0.0.0:0  LISTENING  pid
        out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
        target = f"{host}:{port}"
        pid = None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "TCP" and target in parts[1] and "LISTENING" in line:
                pid = parts[-1]
                break
        if not pid:
            return None
        task = subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}"], text=True, errors="replace")
        for line in task.splitlines():
            if pid in line and ".exe" in line.lower():
                return line.split()[0]
    except Exception:  # noqa: BLE001
        pass
    return None


class _ProxyCache:
    """缓存可用的代理地址，避免每次 spawn 都重复探测。"""

    def __init__(self) -> None:
        self._value: str | None = None
        self._checked: bool = False

    def get(self) -> str | None:
        if self._checked:
            return self._value
        self._value = _resolve_working_proxy()
        self._checked = True
        return self._value

    def invalidate(self) -> None:
        self._checked = False
        self._value = None


_proxy_cache = _ProxyCache()


def _get_system_proxy() -> str | None:
    """读取 Windows 系统代理设置。返回 http://host:port 或 None。"""
    try:
        import winreg  # Windows only
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
        winreg.CloseKey(key)
        if proxy_enable and proxy_server:
            # ProxyServer 可能是 "host:port" 或 "http=host:port;https=host:port"
            # 取第一个 host:port 段，统一补成 http://
            server = proxy_server.split(";")[0].split("=")[-1].strip()
            if server:
                return f"http://{server}"
    except Exception:  # noqa: BLE001
        pass
    return None


def _proxy_host_port(proxy: str) -> tuple[str, int] | None:
    """从 http://host:port 解析 host/port。"""
    try:
        parsed = urllib.parse.urlparse(proxy)
        if parsed.hostname and parsed.port:
            return (parsed.hostname, parsed.port)
    except Exception:  # noqa: BLE001
        pass
    return None


def _port_is_open(host: str, port: int, timeout: float = _PROXY_SOCKET_TIMEOUT) -> bool:
    """快速 TCP 探测端口是否开放。"""
    try:
        import socket

        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def _probe_proxy(proxy: str) -> bool:
    """测试一个 HTTP 代理是否能连到 Google API。"""
    # 只测 HTTP 代理；socks5 需要额外依赖，暂不支持自动探测
    if not proxy.startswith("http://"):
        return False

    hp = _proxy_host_port(proxy)
    if not hp:
        return False

    # 跳过已知非 Antigravity 进程占用的端口（如 0dcloudCore 占 17891）
    proc_name = (_port_owner_process_name(hp[0], hp[1]) or "").lower()
    if any(proc_name.startswith(bad) for bad in _NON_ANTIGRAVITY_PROCESSES):
        print(f"[mcp-hub/proxy] 跳过非 Antigravity 进程占用的代理端口: {proxy} ({proc_name})", flush=True)
        return False

    if not _port_is_open(hp[0], hp[1]):
        return False

    try:
        import urllib.error
        import urllib.request

        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(
            _PROXY_PROBE_URL,
            method="GET",
            headers={"User-Agent": "mcp-hub-antigravity/1.0"},
        )
        with opener.open(req, timeout=_PROXY_PROBE_TIMEOUT) as resp:
            # 能连上就行，401/403 说明代理有效只是没 token
            return resp.status in (200, 401, 403)
    except urllib.error.HTTPError as e:
        # 401/403 同样说明代理能把请求发出去并收到响应
        return e.code in (200, 401, 403)
    except Exception:  # noqa: BLE001
        return False


def _collect_proxy_candidates() -> list[str]:
    """收集候选代理：显式配置 > 环境变量 > 系统代理 > 常见本地端口。"""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(proxy: str | None) -> None:
        if not proxy:
            return
        proxy = proxy.strip()
        if proxy in seen:
            return
        seen.add(proxy)
        candidates.append(proxy)

    # 1) 显式配置（最高优先级）
    add(os.environ.get("ANTIGRAVITY_PROXY"))

    # 2) 环境变量
    add(os.environ.get("HTTP_PROXY"))
    add(os.environ.get("http_proxy"))
    add(os.environ.get("HTTPS_PROXY"))
    add(os.environ.get("https_proxy"))

    # 3) Windows 系统代理
    add(_get_system_proxy())

    # 4) 常见本地端口（只测 HTTP，不测 socks5）
    for port in _COMMON_PROXY_PORTS:
        add(f"http://127.0.0.1:{port}")

    return candidates


def _resolve_working_proxy() -> str | None:
    """找一个能连 Google 的代理。没有则返回 None。"""
    for proxy in _collect_proxy_candidates():
        if _probe_proxy(proxy):
            print(f"[mcp-hub/proxy] 自动探测到可用代理: {proxy}", flush=True)
            return proxy
    print(
        "[mcp-hub/proxy] 未探测到可用代理; Antigravity CLI 可能无法连接 Google。"
        "可设置 ANTIGRAVITY_PROXY=http://host:port 或启动本地代理客户端。",
        flush=True,
    )
    return None


def proxied_env() -> dict[str, str]:
    """Antigravity CLI 默认不会自动走 Windows 系统代理，需要显式注入环境变量。

    探测顺序：ANTIGRAVITY_PROXY > HTTP_PROXY > 系统代理 > 常见本地端口。
    """
    env = {**os.environ}

    # 如果环境变量里已经有显式代理，优先沿用
    existing = (
        env.get("ANTIGRAVITY_PROXY")
        or env.get("HTTP_PROXY")
        or env.get("http_proxy")
        or env.get("HTTPS_PROXY")
        or env.get("https_proxy")
    )
    if existing:
        proxy = existing
    else:
        proxy = _proxy_cache.get()

    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
    if "NO_PROXY" not in env and "no_proxy" not in env:
        env["NO_PROXY"] = "localhost,127.0.0.1"
    return env
