"""工具基类 —— 同步 fork CLI 进程、收集 JSON 输出。

跟 runtimes/ 的区别：
    - runtimes/  是 spawn-and-forget 的 subagent（长任务，异步）
    - tools/    是 request-response 的工具（短任务，同步等结果）

mmx 之类多模态 CLI 都属于"调一下等结果"的模式。
"""

from __future__ import annotations

import abc
import asyncio
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ToolResult:
    """工具调用结果。"""

    tool: str
    operation: str
    ok: bool
    data: Any = None
    error: str | None = None
    duration_sec: float = 0.0
    raw_output: str = ""
    files: list[str] = None  # type: ignore[assignment]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "operation": self.operation,
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "duration_sec": self.duration_sec,
            "files": self.files or [],
        }


class ToolAdapter(abc.ABC):
    """所有工具 adapter 的基类。"""

    name: str = "base"
    binary: str = ""

    def __init__(self):
        pass

    @abc.abstractmethod
    def is_available(self) -> bool:
        """检测 CLI 是否安装。"""

    @abc.abstractmethod
    def list_operations(self) -> list[str]:
        """列出此工具支持的操作（chat / image / speech / ...）。"""

    @abc.abstractmethod
    async def call(self, operation: str, **kwargs: Any) -> ToolResult:
        """调一个操作，等结果返回。"""

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "binary": self.binary,
            "available": self.is_available(),
            "operations": self.list_operations() if self.is_available() else [],
        }

    def _resolve_cmd(self) -> str | None:
        for cand in [self.binary, self.binary + ".cmd", self.binary + ".exe"]:
            p = shutil.which(cand)
            if p:
                return p
        return None

    async def _run(
        self,
        cmd: list[str],
        *,
        operation: str,
        timeout_sec: int = 300,
        expect_json: bool = True,
        cwd: str | None = None,
    ) -> ToolResult:
        """通用执行器：异步跑一个子进程，收 stdout/stderr。"""
        started = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except FileNotFoundError as e:
            return ToolResult(
                tool=self.name,
                operation=operation,
                ok=False,
                error=f"binary not found: {e}",
                duration_sec=time.time() - started,
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_sec
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            return ToolResult(
                tool=self.name,
                operation=operation,
                ok=False,
                error=f"timeout after {timeout_sec}s",
                duration_sec=time.time() - started,
            )

        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        dt = time.time() - started

        if proc.returncode != 0:
            return ToolResult(
                tool=self.name,
                operation=operation,
                ok=False,
                error=stderr or stdout or f"exit code {proc.returncode}",
                duration_sec=dt,
                raw_output=stdout,
            )

        data: Any = stdout
        if expect_json:
            # mmx 输出形如 "...JSON..."（前面会有一些进度信息或者一行 info）
            # 策略：找第一个 { 或 [ 开头、最后一个 } 或 ] 结尾的子串
            data = _extract_json(stdout)
            if data is None:
                # 不是 JSON，原样返回
                data = stdout

        files = _extract_files_from_cmd(cmd) if operation.endswith(("_generate", "synthesize", "music_generate")) else []

        return ToolResult(
            tool=self.name,
            operation=operation,
            ok=True,
            data=data,
            duration_sec=dt,
            raw_output=stdout if not expect_json else "",
            files=files,
        )


def _extract_json(text: str) -> Any | None:
    """从文本里抽 JSON 块。mmx 默认 stdout 会先吐一些 hint 行，再吐 JSON。"""
    if not text:
        return None
    # 找第一个 { 或 [
    starts = [i for i, c in enumerate(text) if c in "[{"]
    if not starts:
        return None
    for start in starts:
        # 尝试从 start 开始 parse
        snippet = text[start:]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            continue
        # 也试试再短一点（可能后面还有 noise）
    return None


def _extract_files_from_cmd(cmd: list[str]) -> list[str]:
    """从命令参数里抽 --out / --out-dir 之类的输出路径。"""
    files: list[str] = []
    for i, arg in enumerate(cmd):
        if arg in ("--out", "--out-dir") and i + 1 < len(cmd):
            files.append(cmd[i + 1])
    return files
