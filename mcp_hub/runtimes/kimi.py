"""Kimi Code runtime adapter —— 包装 `kimi -p "..." -m model --add-dir ... --yolo`。

实测命令（kimi 0.23.5）：
    kimi -p "task" -m <model> --add-dir <workdir> --yolo

关键参数：
    -p / --prompt <prompt>    非交互模式，跑完即退
    -m / --model <model>      LLM 模型 alias（必须是在 ~/.kimi/config.toml 里配过的）
    --add-dir <dir>           加工作目录（可重复给）
    -y / --yolo               自动批准所有 actions（subagent 必须）
    --output-format text|stream-json   输出格式（v3+ 我们用 stream-json 拿结构化事件）
    --auto                    另一种 auto permission 模式
    --skills-dir <dir>        加载 skills 目录

注意：
    - 不给 -p kimi 会进交互 REPL（hang）
    - kimi CLI 0.23.5 限制：-p 和 --yolo 不能同时用（-p 模式默认就是 auto-approve）
    - --add-dir 不传默认只看 cwd
    - **model 名差异**：
        kimi CLI 走 kimi-code OAuth 协议，model alias 必须在 ~/.kimi/config.toml 里配过。
        kimi 0.23.5 默认能用的是 `kimi-code/kimi-for-coding`（kimi-for-coding-highspeed 也行）。
        而 Moonshot OpenAI 兼容 API 的 model 名（如 `kimi-k3-0905-preview`）kimi CLI 不认，
        需要走 mcp_hub/models/moonshot.py（直接调 API）。
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


class KimiAdapter(RuntimeAdapter):
    name = "kimi"
    binary = "kimi"

    # kimi CLI 0.23.5 默认 config.toml 里的 model alias（用户可改）
    # 注意：这些是 kimi-code OAuth 协议的 alias，跟 Moonshot OpenAI API 的 model 名不一样
    _KNOWN_MODELS = [
        "kimi-code/kimi-for-coding",          # 默认
        "kimi-code/kimi-for-coding-highspeed", # 高速版
    ]

    def is_available(self) -> bool:
        return (
            shutil.which(self.binary) is not None
            or shutil.which(self.binary + ".cmd") is not None
            or shutil.which(self.binary + ".exe") is not None
        )

    def list_models(self) -> list[str]:
        """kimi CLI 没 list-models；调 `kimi doctor` 也不会吐 models。

        先返回已知 alias 清单 + 试着从 config.toml 读 default_model。
        """
        models = list(self._KNOWN_MODELS)
        try:
            cfg = Path.home() / ".kimi" / "config.toml"
            if cfg.exists():
                import re as _re

                text = cfg.read_text(encoding="utf-8")
                m = _re.search(r'default_model\s*=\s*"([^"]+)"', text)
                if m and m.group(1) not in models:
                    models.insert(0, m.group(1))
        except Exception:  # noqa: BLE001
            pass
        return models

    def supports_stream_json(self) -> bool:
        """检测 kimi CLI 是否支持 --output-format stream-json。"""
        try:
            binary = self._resolve_cmd()
            r = subprocess.run([binary, "--help"], timeout=10, capture_output=True, text=True)
            text = (r.stdout or "") + (r.stderr or "")
            return "stream-json" in text
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

        use_stream = self.supports_stream_json()

        cmd = [
            binary,
            "-p", task,                  # 非交互模式 + 任务正文
            "-m", model,                 # 选模型 alias
            "--add-dir", abs_workdir,    # 加工作目录
            # 注意：kimi 不允许 -p + --yolo 同时用。-p 是 non-interactive mode，
            # 这个模式默认就是 auto-approve 的（没交互就不能确认）
        ]
        if use_stream:
            cmd += ["--output-format", "stream-json"]  # 结构化事件流
        else:
            cmd += ["--output-format", "text"]         # fallback 纯文本

        # stdout/stderr 直接重定向到日志文件（不走 PIPE）。
        # stderr 单独进 .err.log：混进 stdout 会破坏 stream-json 的
        # _looks_like_jsonl 探测。
        log_fp, err_file, err_fp = open_subagent_logs(out_file)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=abs_workdir,
                stdin=asyncio.subprocess.DEVNULL,   # 防止 CLI 意外读 stdin 等权限确认而挂起
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

        # 抽 transcript
        if self.supports_stream_json() and _looks_like_jsonl(clean_stdout):
            events = _parse_kimi_stream_json(clean_stdout)
            # stream-json 没有 usage，但 session 的 wire.jsonl 里每个 API 调用
            # 都有 StatusUpdate.token_usage —— 按 session_id 捞回来补一条 usage 事件
            sid = _extract_session_id(clean_stdout)
            if sid:
                usage_ev = _read_wire_usage(sid)
                if usage_ev is not None:
                    for i in range(len(events) - 1, -1, -1):
                        if events[i].get("type") == "final":
                            events.insert(i, usage_ev)
                            break
                    else:
                        events.append(usage_ev)
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


# ---------- 辅助函数 ----------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _extract_summary(stdout: str) -> str:
    """kimi 输出最后一段非空行当 summary。"""
    lines = [l.strip() for l in stdout.splitlines() if l.strip()]
    if not lines:
        return ""
    for line in reversed(lines):
        if len(line) > 20 and not line.startswith(("```", "#", "/", "•", "-")):
            return line[:500]
    return lines[-1][:500]


def _extract_artifacts(stdout: str) -> list[str]:
    """kimi 输出里 "Edited /path" / "Created /path" 这种标记。"""
    files: list[str] = []
    for line in stdout.splitlines():
        m = re.search(
            r"(?:Edited|Created|Wrote|Read|Updated|Writing)\s+([^\s)].+\.\w+)",
            line,
        )
        if m:
            p = m.group(1).strip().rstrip(".")
            if p and p not in files:
                files.append(p)
    return files


def _looks_like_jsonl(stdout: str) -> bool:
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


def _parse_kimi_stream_json(stdout: str) -> list[dict[str, Any]]:
    """把 kimi --output-format stream-json 的 JSONL 事件流解析成标准化 transcript。

    kimi 0.23.5 stream-json 实际格式（实测）：
      - {"role":"assistant","content":"..."}            → turn
      - {"role":"assistant","tool_calls":[{...}]}       → tool_call(s)
      - {"role":"tool","tool_call_id":"...","content":"..."}  → tool_result
      - {"role":"meta","type":"session.resume_hint",...} → 跳过
      - {"role":"user", ...}                            → 一般是 prompt 回显，跳过

    统一输出标准化事件流。
    """
    events: list[dict[str, Any]] = []

    pending_assistant_text: str = ""
    final_text: str | None = None

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

        role = ev.get("role", "")
        content = ev.get("content", "")

        if role == "assistant":
            # text content
            if isinstance(content, str) and content:
                events.append({
                    "type": "turn",
                    "role": "assistant",
                    "content": content,
                })
                final_text = content  # 记下来兜底当 final
            elif isinstance(content, list):
                # 多段 content（text + tool_calls）
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "text" and blk.get("text"):
                        events.append({
                            "type": "turn",
                            "role": "assistant",
                            "content": blk["text"],
                        })
                        final_text = blk["text"]
            # tool_calls
            for tc in ev.get("tool_calls", []) or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function", {}) or {}
                args_str = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except json.JSONDecodeError:
                    args = {"_raw": args_str}
                events.append({
                    "type": "tool_call",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "args": args,
                })
        elif role == "tool":
            # tool result
            events.append({
                "type": "tool_result",
                "tool_use_id": ev.get("tool_call_id", ""),
                "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
            })
        # role == "user" / "meta" / "system" 都跳过

    # final —— 用最后一个 assistant 文本
    if final_text:
        events.append({
            "type": "final",
            "content": final_text,
            "stop_reason": "end_turn",
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


# ---------- wire.jsonl usage 解析 ----------

def _extract_session_id(stdout: str) -> str | None:
    """从 stream-json 的 meta session.resume_hint 行里提取 session_id。"""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{") or "session" not in line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("role") == "meta" and "session" in str(ev.get("type", "")):
            sid = ev.get("session_id")
            if isinstance(sid, str) and sid:
                return sid
    return None


def _read_wire_usage(session_id: str, sessions_root: Path | None = None) -> dict[str, Any] | None:
    """读 kimi session 的 wire.jsonl，聚合每次 API 调用的 token 用量。

    两种 session 布局/事件格式都支持（0.23.5 实测）：

    新版（kimi-code 布局）：
      路径 ~/.kimi-code/sessions/<wd_*>/<session_id>/agents/main/wire.jsonl
      事件 {"type":"usage.record","usage":{"inputOther":N,"output":N,
            "inputCacheRead":N,"inputCacheCreation":N},"usageScope":"turn",...}
      一次 turn 一条，直接求和。

    旧版（~/.kimi 布局）：
      路径 ~/.kimi/sessions/<workdir-hash>/<session_id>/wire.jsonl
      事件 {"message":{"type":"StatusUpdate","payload":{"token_usage":{
            "input_other":N,"output":N,"input_cache_read":N,
            "input_cache_creation":N},"message_id":"..."}}}
      同一 message_id 可能推多条，按 message_id 去重取最后一条再求和。

    没有 cost 源，cost 记 0。
    """
    roots = [sessions_root] if sessions_root is not None else [
        Path.home() / ".kimi-code" / "sessions",
        Path.home() / ".kimi" / "sessions",
    ]
    wire_files: list[Path] = []
    for root in roots:
        wire_files.extend(root.glob(f"*/{session_id}/agents/main/wire.jsonl"))
        wire_files.extend(root.glob(f"*/{session_id}/wire.jsonl"))
    if not wire_files:
        return None

    total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    found = False

    def add(input_: int, output: int, cache_r: int, cache_w: int) -> None:
        nonlocal found
        total["input"] += input_
        total["output"] += output
        total["cache_read"] += cache_r
        total["cache_write"] += cache_w
        found = True

    legacy_by_msg: dict[str, dict[str, Any]] = {}
    for wire in wire_files:
        try:
            with wire.open("r", encoding="utf-8", errors="replace") as fp:
                for idx, line in enumerate(fp):
                    if "usage" not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    # 新版 usage.record
                    if ev.get("type") == "usage.record":
                        u = ev.get("usage")
                        if isinstance(u, dict):
                            add(
                                int(u.get("inputOther") or 0),
                                int(u.get("output") or 0),
                                int(u.get("inputCacheRead") or 0),
                                int(u.get("inputCacheCreation") or 0),
                            )
                        continue
                    # 旧版 StatusUpdate
                    msg = ev.get("message") or {}
                    if msg.get("type") != "StatusUpdate":
                        continue
                    payload = msg.get("payload") or {}
                    tu = payload.get("token_usage")
                    if not isinstance(tu, dict):
                        continue
                    mid = str(payload.get("message_id") or f"_line_{idx}")
                    legacy_by_msg[mid] = tu
        except OSError:
            continue

    for tu in legacy_by_msg.values():
        add(
            int(tu.get("input_other") or 0),
            int(tu.get("output") or 0),
            int(tu.get("input_cache_read") or 0),
            int(tu.get("input_cache_creation") or 0),
        )

    if not found:
        return None
    return {
        "type": "usage",
        "tokens": {
            "input": total["input"],
            "output": total["output"],
            "reasoning": 0,
            "total": total["input"] + total["output"] + total["cache_read"] + total["cache_write"],
            "cache": {"read": total["cache_read"], "write": total["cache_write"]},
        },
        "cost": 0.0,
    }
