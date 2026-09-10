"""Codex CLI runtime adapter —— 包装 `codex exec --skip-git-repo-check "..."`。

实测命令（codex-cli 0.144.5 + botcf 中转站）：
    codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
        --add-dir <workdir> "task..."

关键参数：
    exec                                非交互子命令
    --skip-git-repo-check               跳过 git repo 强制（mcp-hub 经常在非 git 目录跑）
    --dangerously-bypass-approvals-and-sandbox
                                        自动批准 + 跳过 sandbox（subagent 必须）
    --add-dir <dir>                     加工作目录
    -m, --model <model>                 选 model（gpt-5.6-sol / gpt-5.6-terra / gpt-5.6-luna）
    -c, --config <key=value>            覆盖 config 项（用于 reasoning_effort 等）
    --json                              事件以 JSONL 打到 stdout（v3+ 新增支持）

注意：
    - 必须给 --dangerously-bypass-approvals-and-sandbox，否则会卡在权限确认
    - codex CLI 的 model 由 ~/.codex/config.toml 决定（走 botcf 中转站）
    - workdir 必须是绝对路径
"""

from __future__ import annotations

import asyncio
import json
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
    wait_and_collect,
    write_transcript,
)


class CodexAdapter(RuntimeAdapter):
    name = "codex"
    binary = "codex"
    supports_resume = True

    _KNOWN_MODELS = [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]

    def login_hint(self) -> str:
        return "在本机终端运行 `codex login` 后，回到仪表盘点「连接」。"

    def login_command(self) -> list[str] | None:
        return [self.binary, "login"]

    def is_available(self) -> bool:
        return (
            shutil.which(self.binary) is not None
            or shutil.which(self.binary + ".cmd") is not None
            or shutil.which(self.binary + ".exe") is not None
        )

    def list_models(self) -> list[str]:
        return list(self._KNOWN_MODELS)

    def supports_json_events(self) -> bool:
        """检测 codex CLI 是否支持 --json（事件 JSONL 输出）。"""
        try:
            binary = self._resolve_cmd()
            r = subprocess.run(
                [binary, "exec", "--help"],
                timeout=10,
                capture_output=True,
                text=True,
            )
            text = (r.stdout or "") + (r.stderr or "")
            return "--json" in text and "JSONL" in text
        except Exception:  # noqa: BLE001
            return False

    async def spawn(
        self,
        task_id: str,
        model: str,
        task: str,
        workdir: str,
        timeout_sec: int = 600,
        reasoning_effort: str = "",
    ) -> SubagentHandle:
        binary = self._resolve_cmd()
        abs_workdir = str(Path(workdir).resolve())

        out_dir = Path(abs_workdir) / ".mcp-hub" / "subagents"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{task_id}.log"

        cmd = [
            binary,
            "exec",
            "--skip-git-repo-check",                    # mcp-hub 经常跑在非 git 目录
            "--dangerously-bypass-approvals-and-sandbox",  # subagent 必须自动批准
            "--add-dir", abs_workdir,                   # 加工作目录
            "-m", model,                                # 选 model
        ]
        if reasoning_effort:
            # codex CLI 原生支持思考等级配置
            cmd += ["-c", f"model_reasoning_effort={reasoning_effort}"]
        if self.supports_json_events():
            cmd.append("--json")  # JSONL 事件流（结构化）
        cmd.append("-")  # prompt 走 stdin：Windows 下长多行中文 prompt 作 argv 传给 codex.cmd 会被破坏（模型收不到任务，反问"请告诉我目标"）

        # stdout/stderr 直接重定向到日志文件（不走 PIPE）。
        # stderr 单独进 .err.log：混进 stdout 会破坏 --json JSONL 的
        # _looks_like_jsonl 探测。
        log_fp, err_file, err_fp = open_subagent_logs(out_file)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=abs_workdir,
                stdin=asyncio.subprocess.PIPE,   # prompt 经 stdin 写入（见上）
                stdout=log_fp,
                stderr=err_fp,
                env={**os.environ},
            )
            proc.stdin.write(task.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
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

        # 抽 transcript
        if self.supports_json_events() and _looks_like_jsonl(clean_stdout):
            events = _parse_codex_jsonl(clean_stdout)
        else:
            summary = _extract_summary(clean_stdout)
            artifacts = _extract_artifacts(clean_stdout)
            events = _build_transcript(
                prompt=handle.prompt,
                stdout=clean_stdout,
                stderr=clean_stderr,
                artifacts=artifacts,
                summary=summary,
                exit_code=proc.returncode or 0,
            )
            summary_arg = summary
        # 统一：summary 从 events 取
        if events and events[-1].get("type") == "final":
            summary_arg = events[-1].get("content", "")
        else:
            summary_arg = _extract_summary(clean_stdout)
        artifacts = _extract_artifacts(clean_stdout)

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
            summary=summary_arg,
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

    # ---- 超时断线续跑（codex exec resume <uuid>） ----

    def extract_session_id(self, handle: SubagentHandle) -> str | None:
        """从日志（"session_id":"<uuid>"）里提取 session id。"""
        if handle.output_file is None:
            return None
        try:
            text = handle.output_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        m = _SESSION_ID_RE.search(text)
        return m.group(1) if m else None

    async def resume_spawn(
        self,
        session_id: str,
        task_id: str,
        model: str,
        task: str,
        workdir: str,
        timeout_sec: int = 600,
    ) -> SubagentHandle:
        """用 `codex exec resume <uuid>` 续跑同一 session（task 由 server 拼好）。"""
        binary = self._resolve_cmd()
        abs_workdir = str(Path(workdir).resolve())

        out_dir = Path(abs_workdir) / ".mcp-hub" / "subagents"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{task_id}.log"

        cmd = [
            binary,
            "exec",
            "resume",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--add-dir", abs_workdir,
            "-m", model,
        ]
        if self.supports_json_events():
            cmd.append("--json")
        cmd += [session_id, "-"]  # task 走 stdin（同 spawn 的 Windows argv 破坏修复）

        log_fp, err_file, err_fp = open_subagent_logs(out_file)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=abs_workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=log_fp,
                stderr=err_fp,
                env={**os.environ},
            )
            proc.stdin.write(task.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
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


# ---------- 辅助函数 ----------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SESSION_ID_RE = re.compile(r'"session_id":"([^"]+)"')


def _clean_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _extract_summary(stdout: str) -> str:
    """codex exec 输出格式：
        ----
        workdir / model / provider ...
        ----
        user
        <prompt>
        codex
        <实际回答>
        tokens used
        <数字>
    扒 'codex' marker 之后、'tokens used' 之前的内容。
    """
    # 找 "codex\n" marker
    m = re.search(r"\ncodex\s*\n", stdout)
    if not m:
        # 退而求其次：最后一段非空
        lines = [l.strip() for l in stdout.splitlines() if l.strip()]
        for line in reversed(lines):
            if len(line) > 5 and not line.startswith(("---", "model:", "provider:", "approval:", "sandbox:", "reasoning", "session", "workdir", "tokens", "user", "codex")):
                return line[:500]
        return ""
    start = m.end()
    # 找 "tokens used" marker
    end_m = re.search(r"\ntokens used", stdout[start:])
    end = start + end_m.start() if end_m else len(stdout)
    body = stdout[start:end].strip()
    # 取最后一段像样的（避开空行、--- 等）
    lines = [l for l in body.splitlines() if l.strip()]
    if not lines:
        return ""
    # 如果有 "----" 之类的结束符，过滤掉
    lines = [l for l in lines if not l.startswith("---")]
    # 取最后 5 行（可能有 reasoning 之后的 final answer）
    tail = "\n".join(lines[-5:]).strip()
    return tail[:500] if tail else lines[-1][:500]


def _extract_artifacts(stdout: str) -> list[str]:
    """codex 输出里可能提到 'Wrote /path/to/file' / 'Edited /path'。
    实际上 codex 默认不在 stdout 写这种 marker（diff 在 thread 里），但兼容一下。
    """
    files: list[str] = []
    for line in stdout.splitlines():
        m = re.search(
            r"(?:Wrote|Edited|Created|Updated)\s+([^\s)].+\.\w+)",
            line,
        )
        if m:
            p = m.group(1).strip().rstrip(".")
            if p and p not in files:
                files.append(p)
    return files


def _looks_like_jsonl(stdout: str) -> bool:
    """stdout 看起来是 JSONL 格式？"""
    if not stdout.strip():
        return False
    for line in stdout.splitlines()[:5]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return True
        except json.JSONDecodeError:
            return False
    return False


def _parse_codex_jsonl(stdout: str) -> list[dict[str, Any]]:
    """把 codex --json 的 JSONL 事件流解析成标准化 transcript。

    已知事件类型（基于 codex 0.144.5 源码）：
      - {"type":"thread.started","thread_id":"..."}
      - {"type":"turn.started"}
      - {"type":"item.started"|"item.completed","item":{...}}
        其中 item.type 可能是：
          - "command_execution" (item.command, item.status)
          - "file_change" (item.changes, item.status)
          - "agent_message" (item.text)
          - "reasoning" (item.text)
          - "mcp_tool_call" (item.server, item.tool, item.arguments, item.result)
      - {"type":"turn.completed","usage":{...}}
      - {"type":"thread.completed","usage":{...}}
      - {"type":"error","message":"..."}

    我们映射成：
      - turn: agent_message（user 看到的回答）
      - tool_call: command_execution / mcp_tool_call
      - file_change: file_change
      - final: turn.completed
      - error: error
    """
    events: list[dict[str, Any]] = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue

        et = ev.get("type", "")
        item = ev.get("item") or {}

        if et == "item.started" or et == "item.completed":
            item_type = item.get("type", "")
            if item_type == "agent_message":
                text = item.get("text", "")
                if text and et == "item.completed":
                    events.append({
                        "type": "turn",
                        "role": "assistant",
                        "content": text,
                    })
            elif item_type == "command_execution":
                events.append({
                    "type": "tool_call",
                    "name": "Bash",
                    "args": {"command": item.get("command", "")},
                    "status": item.get("status", ""),
                    "exit_code": item.get("exit_code"),
                })
            elif item_type == "file_change":
                for change in item.get("changes", []) or []:
                    events.append({
                        "type": "file_change",
                        "path": change.get("path", change.get("file_path", "")),
                        "action": change.get("kind", "modify"),
                    })
            elif item_type == "mcp_tool_call":
                events.append({
                    "type": "tool_call",
                    "name": f"{item.get('server', 'mcp')}/{item.get('tool', '')}",
                    "args": item.get("arguments", {}),
                    "result_preview": str(item.get("result", ""))[:300] if et == "item.completed" else None,
                })
            elif item_type == "reasoning":
                if et == "item.completed":
                    text = item.get("text", "")
                    if text:
                        events.append({
                            "type": "turn",
                            "role": "reasoning",
                            "content": text,
                        })
        elif et == "turn.completed":
            # usage 映射成标准 usage 事件（input/output/reasoning/total + cache.read）
            usage = ev.get("usage")
            if isinstance(usage, dict):
                tokens: dict[str, Any] = {}
                for src, dst in (
                    ("input_tokens", "input"),
                    ("output_tokens", "output"),
                    ("reasoning_output_tokens", "reasoning"),
                    ("total_tokens", "total"),
                ):
                    v = usage.get(src)
                    if isinstance(v, (int, float)):
                        tokens[dst] = int(v)
                cached = usage.get("cached_input_tokens")
                if isinstance(cached, (int, float)) and cached:
                    tokens["cache"] = {"read": int(cached), "write": 0}
                if "total" not in tokens and ("input" in tokens or "output" in tokens):
                    tokens["total"] = tokens.get("input", 0) + tokens.get("output", 0)
                if tokens:
                    events.append({"type": "usage", "tokens": tokens, "cost": 0.0})
            # 找最后一个 agent_message 当 final
            for prev in reversed(events):
                if prev.get("type") == "turn" and prev.get("role") == "assistant":
                    events.append({
                        "type": "final",
                        "content": prev.get("content", ""),
                        "stop_reason": "end_turn",
                    })
                    break
        elif et == "error":
            events.append({
                "type": "error",
                "message": ev.get("message", str(ev)),
            })

    return events


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

    # file changes（去重）
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

    # final
    if summary:
        events.append({
            "type": "final",
            "content": summary,
            "stop_reason": "ok" if exit_code == 0 else "error",
        })

    # error
    if exit_code != 0 and stderr:
        events.append({
            "type": "error",
            "message": stderr[-2000:],
        })

    return events
