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
from .netutil import proxied_env as _antigravity_env

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
