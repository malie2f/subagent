"""Antigravity CLI adapter —— 包装 `antigravity --print <task> ...`。

Antigravity CLI（官方仓库 google-antigravity/antigravity-cli，命令名 agy / antigravity）
是 Google 在 2026 I/O 发布的独立命令行工具。它通过 `--print` 支持非交互单次任务。

注意调用格式：
  antigravity --print "<task>" --add-dir <dir> --model <model> --dangerously-skip-permissions

解析基于文本输出：Antigravity CLI 没有结构化事件流，但会在回复里用 markdown 链接
（如 `[existing.txt](file:///C:/.../existing.txt)`）引用它改动过的文件。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .base import (
    RuntimeAdapter,
    SubagentHandle,
    SubagentResult,
    open_subagent_logs,
    wait_and_collect,
    write_transcript,
)

# 最低版本要求：v1.1.0 起 `--print` 和 `--dangerously-skip-permissions` 已稳定
MIN_VERSION = (1, 1, 0)

# 认证状态探测缓存 TTL（秒）
_AUTH_TTL = 60.0

# 未登录 CLI 的典型输出片段（不区分大小写）
_UNAUTH_PATTERNS = (
    "please sign in",
    "authentication required",
    "you are not logged in",
    "not logged into antigravity",
    "failed to get oauth token",
    "error getting token source",
)


class AntigravityAdapter(RuntimeAdapter):
    name = "antigravity"
    binary = "antigravity"

    def __init__(self) -> None:
        super().__init__()
        self._auth_ok: bool | None = None
        self._auth_checked_at: float = 0.0
        self._models_cache: list[str] | None = None
        self._models_cached_at: float = 0.0

    def is_available(self) -> bool:
        binary = self._resolve_cmd()
        if not binary:
            return False
        version = self._probe_version(binary)
        if not version or version < MIN_VERSION:
            return False
        # 探测认证状态：未登录的 CLI 调 --print 会挂起等 OAuth，必须提前拦截
        return self._ensure_auth(binary)

    def list_models(self) -> list[str]:
        """调 `antigravity models` 拿真实模型列表（需要已登录）。"""
        binary = self._resolve_cmd()
        if not binary:
            return []
        if not self._ensure_auth(binary):
            return []
        now = time.time()
        if self._models_cache is not None and now - self._models_cached_at < _AUTH_TTL:
            return list(self._models_cache)
        try:
            proc = subprocess.run(
                [binary, "models"],
                timeout=12,
                capture_output=True,
                text=True,
                env=_antigravity_env(),
            )
            if proc.returncode == 0:
                models = [m.strip() for m in proc.stdout.splitlines() if m.strip()]
                self._models_cache = models
                self._models_cached_at = now
                return list(models)
            # 如果返回的是未登录错误，刷新认证缓存
            if _looks_unauthenticated(proc.stdout + proc.stderr):
                self._auth_ok = False
                self._auth_checked_at = now
        except Exception:  # noqa: BLE001
            pass
        return []

    async def spawn(
        self,
        task_id: str,
        model: str,
        task: str,
        workdir: str,
        timeout_sec: int = 600,
        reasoning_effort: str = "",
    ) -> SubagentHandle:
        """fork 一个 `antigravity --print` 进程。"""
        out_dir = Path(workdir) / ".mcp-hub" / "subagents"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{task_id}.log"

        binary = self._resolve_cmd()
        if not binary:
            raise RuntimeError(
                "antigravity CLI (agy / antigravity) not found in PATH"
            )
        if not self._ensure_auth(binary):
            raise RuntimeError(
                "Antigravity CLI 未登录。请在终端运行 `antigravity`（不带参数）"
                "或访问 IDE 完成 Google OAuth，之后 `antigravity models` 能正常列出模型再试。"
                "当前 IDE/language_server 已认证，但 CLI (agy) 的认证是独立的。"
            )

        # 命令顺序是实测结果：--print 后面必须紧跟 task，否则模型会把 flag 当 prompt
        cmd = [binary, "--print", task]
        cmd += ["--add-dir", str(Path(workdir).resolve())]
        if model:
            cmd += ["--model", model]
        cmd.append("--dangerously-skip-permissions")

        # stdout/stderr 直接重定向到日志文件（不走 PIPE）。
        # stderr 单独进 .err.log：summary/artifacts 解析只吃纯 stdout。
        # 代理注入的 env（_antigravity_env）保持不变。
        log_fp, err_file, err_fp = open_subagent_logs(out_file)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workdir,
                stdin=asyncio.subprocess.DEVNULL,   # 防止 CLI 意外读 stdin 等权限确认而挂起
                stdout=log_fp,
                stderr=err_fp,
                env=_antigravity_env(),
            )
        except BaseException:
            log_fp.close()
            err_fp.close()
            raise

        return SubagentHandle(
            pid=proc.pid,
            runtime=self.name,
            model=model,
            task_id=task_id,
            workdir=workdir,
            started_at=time.time(),
            process=proc,
            output_file=out_file,
            prompt=task,
            log_fp=log_fp,
            err_file=err_file,
            err_fp=err_fp,
        )

    async def wait(self, handle: SubagentHandle, timeout_sec: int) -> SubagentResult:
        proc = handle.process
        if proc is None:
            return SubagentResult(
                runtime=handle.runtime,
                model=handle.model,
                task_id=handle.task_id,
                exit_code=-1,
                stdout="",
                stderr="",
                duration_sec=0,
                error="no process",
                prompt=handle.prompt,
            )

        started = time.time()
        stdout, stderr = await wait_and_collect(handle, proc, timeout_sec)
        clean_stdout = _clean_ansi(stdout)
        clean_stderr = _clean_ansi(stderr)

        artifacts = _extract_artifacts(clean_stdout)
        summary = _extract_summary(clean_stdout)
        transcript = _build_transcript(
            prompt=handle.prompt,
            stdout=clean_stdout,
            stderr=clean_stderr,
            artifacts=artifacts,
            summary=summary,
            exit_code=proc.returncode or 0,
        )
        if handle.output_file:
            write_transcript(handle, handle.prompt, transcript)

        return SubagentResult(
            runtime=handle.runtime,
            model=handle.model,
            task_id=handle.task_id,
            exit_code=proc.returncode or 0,
            stdout=clean_stdout,
            stderr=clean_stderr,
            duration_sec=time.time() - started,
            summary=summary,
            artifacts=artifacts,
            error=clean_stderr if proc.returncode != 0 else None,
            prompt=handle.prompt,
            transcript=transcript,
        )

    async def cancel(self, handle: SubagentHandle) -> bool:
        proc = handle.process
        if proc is None or proc.returncode is not None:
            return False
        try:
            proc.kill()
            await proc.wait()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _resolve_cmd(self) -> str | None:
        """Windows 上优先 .exe，也兼容未来可能创建的 agy.cmd / agy 别名。"""
        for cand in [self.binary, "agy", self.binary + ".exe", self.binary + ".cmd"]:
            p = shutil.which(cand)
            if p:
                return p
        return None

    def _probe_version(self, binary: str) -> tuple[int, int, int] | None:
        try:
            proc = subprocess.run(
                [binary, "--version"],
                timeout=10,
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                parts = proc.stdout.strip().split(".")
                if len(parts) >= 3:
                    return (int(parts[0]), int(parts[1]), int(parts[2]))
                if len(parts) == 2:
                    return (int(parts[0]), int(parts[1]), 0)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _ensure_auth(self, binary: str) -> bool:
        """探测 antigravity CLI 是否已登录；带 TTL 缓存避免每次 spawn 都 fork。"""
        now = time.time()
        if self._auth_ok is not None and now - self._auth_checked_at < _AUTH_TTL:
            return self._auth_ok
        self._auth_ok = _check_auth(binary)
        self._auth_checked_at = now
        return self._auth_ok


def _looks_unauthenticated(text: str) -> bool:
    """根据 CLI 输出判断是否因未登录失败。"""
    lowered = text.lower()
    return any(p in lowered for p in _UNAUTH_PATTERNS)


def _check_auth(binary: str) -> bool:
    """运行 `antigravity models` 做轻量认证探测。

    已登录：returncode == 0 并返回模型列表。
    未登录：CLI 会立即打印 OAuth URL 并等 60s，我们用 12s 超时截断，避免挂死。
    """
    try:
        proc = subprocess.run(
            [binary, "models"],
            timeout=12,
            capture_output=True,
            text=True,
            env=_antigravity_env(),
        )
        if proc.returncode == 0:
            return True
        return not _looks_unauthenticated(proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired:
        # 超时大概率是未登录导致的 OAuth 等待
        return False
    except Exception:  # noqa: BLE001
        return False


# ---------- 环境代理 ----------

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
        print(f"[mcp-hub/antigravity] 跳过非 Antigravity 进程占用的代理端口: {proxy} ({proc_name})", flush=True)
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
            print(f"[mcp-hub/antigravity] 自动探测到可用代理: {proxy}", flush=True)
            return proxy
    print(
        "[mcp-hub/antigravity] 未探测到可用代理; Antigravity CLI 可能无法连接 Google。"
        "可设置 ANTIGRAVITY_PROXY=http://host:port 或启动本地代理客户端。",
        flush=True,
    )
    return None


def _antigravity_env() -> dict[str, str]:
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


# ---------- 输出解析 ----------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _extract_summary(stdout: str) -> str:
    """Antigravity --print 的输出就是模型最终回复，直接整体取。"""
    s = stdout.strip()
    return s[:2000] if s else ""


_FILE_LINK_RE = re.compile(
    r"(?P<verb>created|appended|edited|updated|wrote|deleted|modified)?\s*(?:file\s+)?\[(?P<name>[^\]]*)\]\((?P<uri>file://[^)]+)\)",
    re.IGNORECASE,
)


def _file_uri_to_path(uri: str) -> str:
    """file:///C:/foo/bar -> C:/foo/bar；file://host/share 暂不考虑。"""
    parsed = urllib.parse.urlparse(uri)
    path = urllib.parse.unquote(parsed.path)
    if path.startswith("/") and len(path) > 3 and path[2] == ":":
        # /C:/foo -> C:/foo
        path = path[1:]
    return path


def _extract_artifacts(stdout: str) -> list[str]:
    """从 markdown 文件链接里提取被改动过的文件路径。"""
    files: list[str] = []
    for m in _FILE_LINK_RE.finditer(stdout):
        path = _file_uri_to_path(m.group("uri"))
        if path and path not in files:
            files.append(path)
    return files


_ACTION_MAP = {
    "created": "create",
    "wrote": "create",
    "appended": "modify",
    "edited": "modify",
    "updated": "modify",
    "modified": "modify",
    "deleted": "delete",
}


def _build_transcript(
    prompt: str,
    stdout: str,
    stderr: str,
    artifacts: list[str],
    summary: str,
    exit_code: int,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    file_actions: dict[str, str] = {}
    for m in _FILE_LINK_RE.finditer(stdout):
        verb = (m.group("verb") or "").lower()
        path = _file_uri_to_path(m.group("uri"))
        action = _ACTION_MAP.get(verb, "modify")
        # 同路径保留最强动作：modify > create > delete
        if path == "":
            continue
        current = file_actions.get(path)
        if current == "modify":
            continue
        if action == "modify":
            file_actions[path] = "modify"
        elif action == "create" and current != "modify":
            file_actions[path] = "create"
        elif action == "delete" and current not in ("modify", "create"):
            file_actions[path] = "delete"

    for path, action in file_actions.items():
        events.append({
            "type": "file_change",
            "path": path,
            "action": action,
        })

    if summary:
        events.append({
            "type": "final",
            "content": summary,
            "stop_reason": "ok" if exit_code == 0 else "error",
        })

    if exit_code != 0 and stderr:
        events.append({
            "type": "error",
            "message": stderr[-2000:],
        })

    return events


def parse_live_log(stdout: str, stderr: str = "") -> list[dict[str, Any]]:
    """运行中的 antigravity 子 agent：从当前 stdout 实时解析事件预览。

    antigravity CLI 输出的是 markdown 文本，不是 JSONL，所以和最终 transcript
    结构不同：最终 transcript 只有 file_change + final + error；实时预览额外把
    stdout 末尾 30 行当 assistant turn 展示，让人能看到最新进展。
    """
    clean = _clean_ansi(stdout)
    events: list[dict[str, Any]] = []

    file_actions: dict[str, str] = {}
    for m in _FILE_LINK_RE.finditer(clean):
        verb = (m.group("verb") or "").lower()
        path = _file_uri_to_path(m.group("uri"))
        action = _ACTION_MAP.get(verb, "modify")
        if path == "":
            continue
        current = file_actions.get(path)
        if current == "modify":
            continue
        if action == "modify":
            file_actions[path] = "modify"
        elif action == "create" and current != "modify":
            file_actions[path] = "create"
        elif action == "delete" and current not in ("modify", "create"):
            file_actions[path] = "delete"

    for path, action in file_actions.items():
        events.append({"type": "file_change", "path": path, "action": action})

    lines = [ln for ln in clean.splitlines() if ln.strip()]
    tail = "\n".join(lines[-30:])
    if tail:
        events.append({"type": "turn", "role": "assistant", "content": tail})

    if stderr.strip():
        events.append({"type": "error", "message": _clean_ansi(stderr)[-2000:]})

    return events
