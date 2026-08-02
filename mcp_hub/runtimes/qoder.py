"""Qoder CN CLI runtime adapter —— 包装 `qoderclicn -p --permission-mode auto ...`。

实测命令（qoderclicn 1.1.6）：
    qoderclicn -p --permission-mode auto --model Qwen3.8-Max-Preview "task..."

关键参数：
    -p / --print            非交互模式，结果打到 stdout
    --permission-mode auto  自动批准所有工具调用（subagent 必须）
    --model <model>         选模型（Qwen3.8-Max-Preview / DeepSeek-V4-Pro 等）
    --add-dir <dir>         允许子 agent 访问的工作目录（可多次给）
    --session-id <uuid>     指定 session id（落盘，可续跑）
    --resume <uuid>         续跑已有 session（实测：记忆完整保留）

续跑机制：
    spawn 时把 task_id（12 位 hex）映射成固定 UUID 传给 --session-id，
    超时/手动续跑时用 --resume 同一 UUID 拉起，模型能识别原会话上下文。
    实测命令：
        qoderclicn -p --permission-mode auto --session-id <uuid> "记住数字 4281"
        qoderclicn -p --permission-mode auto --resume <uuid> "数字是什么？" → 4281

注意：
    - qoderclicn 默认会进交互 REPL，**必须**给 -p
    - 不给 --permission-mode auto 会卡在权限确认（hang）
    - workdir 用绝对路径；resume 必须在同一 workdir（session 按 cwd 归档）
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .base import (
    RuntimeAdapter,
    SubagentHandle,
    SubagentResult,
    open_subagent_logs,
    resolve_cmd_prefix,
    wait_and_collect,
    write_transcript,
)


class QoderAdapter(RuntimeAdapter):
    name = "qoder"
    binary = "qoderclicn"
    supports_resume = True

    @staticmethod
    def _session_uuid(task_id: str) -> str | None:
        """task_id（12 位 hex，可能带续跑后缀 rsN）→ 固定 UUID，当 qoder session id 用。"""
        base = re.sub(r"rs\d+$", "", task_id)
        if not re.fullmatch(r"[0-9a-fA-F]{12}", base):
            return None
        return f"{base[:8]}-{base[8:]}-4000-8000-000000000000"

    def is_available(self) -> bool:
        return (
            shutil.which(self.binary) is not None
            or shutil.which(self.binary + ".cmd") is not None
            or shutil.which(self.binary + ".exe") is not None
        )

    def list_models(self) -> list[str]:
        """调 `qoderclicn --list-models` 拿真实模型列表（带 TTL 缓存，避免每次 fork 子进程）。"""
        binary = self._resolve_cmd()
        try:
            r = subprocess.run(
                [binary, "--list-models"],
                timeout=15,
                capture_output=True,
                text=True,
            )
            if r.returncode == 0:
                lines = [m.strip() for m in r.stdout.splitlines() if m.strip()]
                # 第一行通常是 "MODEL" 表头
                if lines and lines[0].upper() == "MODEL":
                    lines = lines[1:]
                return lines
        except Exception:  # noqa: BLE001
            pass
        return []

    def _parse_model(self, model: str) -> str:
        """支持 'qoder/Qwen3.8-Max-Preview' 和裸 'Qwen3.8-Max-Preview' 两种写法。"""
        prefix = "qoder/"
        if model.startswith(prefix):
            return model[len(prefix):]
        return model

    async def spawn(
        self,
        task_id: str,
        model: str,
        task: str,
        workdir: str,
        timeout_sec: int = 600,
        reasoning_effort: str = "",
    ) -> SubagentHandle:
        # 固定 session id 落盘，超时/手动续跑可以 --resume 同一 session
        sid = self._session_uuid(task_id)
        extra = ["--session-id", sid] if sid else []
        return await self._launch(task_id, model, task, workdir, extra)

    # ---- 超时断线续跑（qoderclicn --resume <uuid>，实测记忆完整保留） ----

    def extract_session_id(self, handle: SubagentHandle) -> str | None:
        """session id 由 task_id 确定性推出，不用扒日志。"""
        return self._session_uuid(handle.task_id)

    async def resume_spawn(
        self,
        session_id: str,
        task_id: str,
        model: str,
        task: str,
        workdir: str,
        timeout_sec: int = 600,
    ) -> SubagentHandle:
        """用 --resume 续跑同一 session（task 由调用方拼好）。"""
        return await self._launch(task_id, model, task, workdir, ["--resume", session_id])

    async def _launch(
        self,
        task_id: str,
        model: str,
        task: str,
        workdir: str,
        session_args: list[str],
    ) -> SubagentHandle:
        cmd_prefix = resolve_cmd_prefix(self.binary)  # 解开 npm .cmd shim
        abs_workdir = str(Path(workdir).resolve())
        model_name = self._parse_model(model)

        out_dir = Path(abs_workdir) / ".mcp-hub" / "subagents"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{task_id}.log"

        cmd = [
            *cmd_prefix,
            "--print",                # 非交互
            "--permission-mode", "auto",  # 自动批准所有工具调用
            "--add-dir", abs_workdir, # 允许子 agent 读写这个目录
            "--model", model_name,    # 选模型（去掉 qoder/ 前缀）
        ]
        cmd += session_args          # --session-id <uuid> 或 --resume <uuid>
        cmd.append(task)             # 任务正文

        log_fp, err_file, err_fp = open_subagent_logs(out_file)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=abs_workdir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_fp,
                stderr=err_fp,
                env={**os.environ},
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
            workdir=abs_workdir,
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
        events = _build_transcript(
            prompt=handle.prompt,
            stdout=clean_stdout,
            stderr=clean_stderr,
            artifacts=artifacts,
            summary=summary,
            exit_code=proc.returncode or 0,
        )

        if handle.output_file:
            write_transcript(handle, handle.prompt, events)

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
            transcript=events,
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

    def _resolve_cmd(self) -> str:
        for cand in [self.binary, self.binary + ".cmd", self.binary + ".exe"]:
            p = shutil.which(cand)
            if p:
                return p
        return self.binary


# ---------- 辅助函数 ----------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _extract_summary(stdout: str) -> str:
    """qoderclicn -p 输出通常就是纯文本答案，直接取整体尾部。"""
    text = stdout.strip()
    if not text:
        return ""
    # 如果有明显的 markdown 代码块，取第一个代码块内容
    m = re.search(r"```(?:\w+)?\n(.*?)\n```", text, re.DOTALL)
    if m:
        return m.group(1).strip()[:1000]
    return text[:1000]


def _extract_artifacts(stdout: str) -> list[str]:
    """从 qoderclicn 输出里扒文件改动路径。"""
    files: list[str] = []
    for line in stdout.splitlines():
        m = re.search(
            r"(?:Edited|Created|Wrote|Updated|Read)\s+([^\s)].+\.\w+)",
            line,
        )
        if m:
            p = m.group(1).strip().rstrip(".")
            if p and p not in files:
                files.append(p)
    return files


def _build_transcript(
    prompt: str,
    stdout: str,
    stderr: str,
    artifacts: list[str],
    summary: str,
    exit_code: int,
) -> list[dict[str, Any]]:
    """text 模式下的 transcript。"""
    events: list[dict[str, Any]] = []

    seen: set[str] = set()
    for path in artifacts:
        if path in seen:
            continue
        seen.add(path)
        events.append({
            "type": "file_change",
            "path": path,
            "action": "modify",
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
    """运行中的 qoder 子 agent：从当前 stdout 实时解析事件预览。

    qoderclicn --print 输出是纯文本（可能带 ANSI），结构类似 markdown。
    实时预览：提取文件变更 + 末尾 50 行作为最新 assistant 输出，让人看到进展。
    """
    clean = _clean_ansi(stdout)
    events: list[dict[str, Any]] = []

    for path in _extract_artifacts(clean):
        events.append({"type": "file_change", "path": path, "action": "modify"})

    lines = [ln for ln in clean.splitlines() if ln.strip()]
    tail = "\n".join(lines[-50:])
    if tail:
        events.append({"type": "turn", "role": "assistant", "content": tail})

    if stderr.strip():
        events.append({"type": "error", "message": _clean_ansi(stderr)[-2000:]})

    return events
