"""OpenCode runtime adapter —— 包装 `opencode run --format json -m provider/model "..."`。

实测命令（opencode 1.18.4）：
    opencode run -m opencode/big-pickle --format json --auto --print-logs "task..."

关键参数：
    --format json    stdout 输出 JSONL 事件流，每行一个事件：
                       {"type":"step_start","timestamp":...,"sessionID":"ses_...","part":{...}}
                       {"type":"text","timestamp":...,"part":{"id":"prt_...","type":"text","text":"..."}}
                       {"type":"step_finish","timestamp":...,"part":{"type":"step-finish",
                        "reason":"stop","tokens":{...},"cost":0}}
                     工具调用是 part.type == "tool" 的事件（state.status / state.input）。
    --auto           自动批准权限（非交互模式必需，可用 HUB_OPENCODE_AUTO=false 关掉）
    --print-logs     把日志打到 stderr，方便排查

解析策略（wait）：
    - 逐行 JSON 解析；行首非 '{' 或解析失败的行降级为原始文本收集，不崩
    - text part 按 part.id 去重（流式更新会重复推同一个 part，后者覆盖前者），
      全部 text 拼接成 summary 并生成 final 事件
    - tool part 完结时映射成 tool_call（附 result_preview）；write→create、edit→modify 生成 file_change
    - step_finish 的 tokens/cost 聚合成一个 {"type":"usage", ...} 事件
    - 未知 part.type 直接跳过
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
    unwrap_cmd_shim,
    wait_and_collect,
    write_transcript,
)

# `--format json` 的最低版本要求
MIN_VERSION = (1, 18, 0)

# list_models / 版本探测的子进程缓存 TTL（秒）
_PROBE_TTL = 300.0

# tool 名 → file_change action
_WRITE_TOOLS = {"write"}
_EDIT_TOOLS = {"edit", "patch", "apply_patch"}


class OpencodeAdapter(RuntimeAdapter):
    name = "opencode"
    binary = "opencode"
    supports_resume = True

    def __init__(self):
        super().__init__()
        self._models_cache: list[str] | None = None
        self._models_cached_at: float = 0.0
        self._version_cache: tuple[int, ...] | None = None
        self._version_checked_at: float = 0.0

    # ---- 可用性 / 模型列表 ----

    def is_available(self) -> bool:
        """二进制存在且版本 >= 1.18（--format json 的底线）。"""
        if not self._find_binary_with_ext():
            return False
        version = self._probe_version()
        if version is None:
            return False
        return version >= MIN_VERSION

    def list_models(self) -> list[str]:
        """调 `opencode models` 拿真实模型列表（带 TTL 缓存，避免每次 fork 子进程）。"""
        now = time.time()
        if self._models_cache is not None and now - self._models_cached_at < _PROBE_TTL:
            return list(self._models_cache)
        try:
            binary = self._resolve_cmd()
            proc = subprocess.run(
                [binary, "models"],
                timeout=15,
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                models = [m.strip() for m in proc.stdout.splitlines() if m.strip()]
                # 过滤黑名单（死掉的 provider/模型）
                from mcp_hub.config import load_settings
                settings = load_settings()
                blocklist = set(settings.model_blocklist())
                models = [m for m in models if m not in blocklist]
                # provider 白名单（如 opencode-go 只放行 ds-v4-flash/pro）
                allow = settings.provider_allowlist()
                if allow:
                    models = [
                        m for m in models
                        if "/" not in m
                        or m.split("/", 1)[0] not in allow
                        or m.split("/", 1)[1] in allow[m.split("/", 1)[0]]
                    ]
                self._models_cache = models
                self._models_cached_at = now
                return list(models)
        except Exception:  # noqa: BLE001
            pass
        return []

    # ---- 生命周期 ----

    async def spawn(
        self,
        task_id: str,
        model: str,
        task: str,
        workdir: str,
        timeout_sec: int = 600,
        reasoning_effort: str = "",
    ) -> SubagentHandle:
        """真的 fork 一个 `opencode run --format json` 进程。

        model 格式：provider/model（如 opencode-go/minimax-m2.7）
        """
        # reasoning_effort 优先靠模型变体名实现（如 gemini-3.6-flash-medium）；
        # 无变体时回退到 opencode 原生 `--variant <effort>` 标志（deepseek 官方
        # 分组等 provider 的 reasoning effort 走这条路，如 --variant max）。
        effort_warn = ""
        variant_flag = ""
        if reasoning_effort and not model.endswith(("-low", "-medium", "-high")):
            candidate = f"{model}-{reasoning_effort}"
            if candidate in self.list_models():
                model = candidate
            else:
                variant_flag = reasoning_effort
        # 准备输出文件
        out_dir = Path(workdir) / ".mcp-hub" / "subagents"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{task_id}.log"

        # 拼命令 —— Windows 上要解析 .cmd 包装
        binary = self._resolve_cmd()
        cmd = [
            binary,
            "run",
            "-m", model,
            "--format", "json",  # 结构化 JSONL 事件流（1.18+）
            "--print-logs",      # 把日志打到 stderr，方便我们抓
        ]
        if self._auto_approve():
            cmd.append("--auto")  # 自动批准权限，否则非交互模式会卡在审批
        if variant_flag:
            cmd += ["--variant", variant_flag]  # provider 级 reasoning effort（如 max）
        cmd.append(task)

        # opencode 解析相对文件路径时认 PWD 环境变量而不是进程 cwd（实测：
        # 从 Git Bash 起的进程继承 PWD=启动目录，write 工具的相对路径会落到
        # PWD 下而不是 cwd 下）。把 PWD 对齐到 workdir，保证文件落在工作目录。
        env = {**os.environ, "PWD": str(Path(workdir).resolve())}

        # stdout/stderr 直接重定向到日志文件（不走 PIPE，杜绝 pipe buffer 堵死）。
        # stderr 单独进 .err.log：--print-logs 的日志很啰嗦，混进 stdout 会
        # 污染 JSONL 解析。
        log_fp, err_file, err_fp = open_subagent_logs(out_file)
        if effort_warn:
            err_fp.write(effort_warn)  # 变体不存在的警告落到 stderr 日志
            err_fp.flush()
        try:
            # Windows 上 asyncio.create_subprocess_exec 默认不走 shell，直接传 list
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workdir,
                stdin=asyncio.subprocess.DEVNULL,   # 防止 CLI 意外读 stdin 等权限确认而挂起
                stdout=log_fp,
                stderr=err_fp,
                env=env,
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
        """等 opencode 跑完，解析 JSONL 事件流。"""
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

        exit_code = proc.returncode or 0
        transcript, summary, artifacts = _parse_opencode_jsonl(clean_stdout)
        if exit_code != 0 and clean_stderr:
            transcript.append({
                "type": "error",
                "message": clean_stderr[-2000:],
            })
        if handle.output_file:
            write_transcript(handle, handle.prompt, transcript)

        return SubagentResult(
            runtime=handle.runtime,
            model=handle.model,
            task_id=handle.task_id,
            exit_code=exit_code,
            stdout=clean_stdout,
            stderr=clean_stderr,
            duration_sec=time.time() - started,
            summary=summary,
            artifacts=artifacts,
            error=clean_stderr if exit_code != 0 else None,
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

    # ---- 超时断线续跑（opencode session 落盘，原生支持 resume） ----

    def extract_session_id(self, handle: SubagentHandle) -> str | None:
        """从日志（每行 JSONL 带 "sessionID":"ses_..."）里提取 session id。"""
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
        """用 --session 续跑同一 session（task 由 server 拼好）。"""
        out_dir = Path(workdir) / ".mcp-hub" / "subagents"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{task_id}.log"

        binary = self._resolve_cmd()
        cmd = [
            binary,
            "run",
            "-m", model,
            "--format", "json",
            "--print-logs",
        ]
        if self._auto_approve():
            cmd.append("--auto")
        cmd += ["--session", session_id, task]

        env = {**os.environ, "PWD": str(Path(workdir).resolve())}

        log_fp, err_file, err_fp = open_subagent_logs(out_file)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workdir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_fp,
                stderr=err_fp,
                env=env,
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

    # ---- 内部工具 ----

    def _resolve_cmd(self) -> str:
        """Windows 上 .cmd 包装的解析（npm shim 解开成真实 exe）。"""
        for cand in [self.binary, self.binary + ".cmd", self.binary + ".exe"]:
            p = shutil.which(cand)
            if p:
                return unwrap_cmd_shim(p)
        return self.binary  # fallback

    def _find_binary_with_ext(self) -> str | None:
        for cand in [self.binary, self.binary + ".cmd", self.binary + ".exe"]:
            p = shutil.which(cand)
            if p:
                return p
        return None

    def _auto_approve(self) -> bool:
        """读 HUB_OPENCODE_AUTO 配置（默认 True）。读配置失败时按 True 处理。"""
        try:
            from ..config import load_settings

            return bool(load_settings().hub_opencode_auto)
        except Exception:  # noqa: BLE001
            return True

    def _probe_version(self) -> tuple[int, ...] | None:
        """跑 `opencode --version` 解析版本号（带 TTL 缓存）。失败返回 None。"""
        now = time.time()
        if self._version_checked_at and now - self._version_checked_at < _PROBE_TTL:
            return self._version_cache
        version: tuple[int, ...] | None = None
        try:
            binary = self._resolve_cmd()
            proc = subprocess.run(
                [binary, "--version"],
                timeout=10,
                capture_output=True,
                text=True,
            )
            version = _parse_version((proc.stdout or "") + "\n" + (proc.stderr or ""))
        except Exception:  # noqa: BLE001
            version = None
        self._version_cache = version
        self._version_checked_at = now
        return version


# ---------- 辅助函数 ----------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_SESSION_ID_RE = re.compile(r'"sessionID":"([^"]+)"')


def _clean_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _parse_version(text: str) -> tuple[int, ...] | None:
    """从 `opencode --version` 输出里解析 (major, minor, patch)。"""
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def _parse_opencode_jsonl(stdout: str) -> tuple[list[dict[str, Any]], str, list[str]]:
    """把 `opencode run --format json` 的 JSONL 输出解析成标准化事件流。

    返回 (events, summary, artifacts)：
      - events: file_change / turn(reasoning) / tool_call(附 result_preview) /
                turn(assistant) / usage / final
      - summary: 全部 text part 拼接（去重后）；没有 text 时降级用最后一行原始文本
      - artifacts: file_change 涉及的文件路径（按首次出现顺序）

    容错：
      - 行首非 '{' 或 JSON 解析失败 → 收进 raw_lines，不崩
      - 同一个 text part 流式重复推 → 按 part.id 覆盖去重
      - 未知 part.type → 跳过
    """
    text_parts: dict[str, dict[str, Any]] = {}      # pid -> {text, ts, order}
    reasoning_parts: dict[str, dict[str, Any]] = {}
    text_order: list[str] = []
    reasoning_order: list[str] = []
    tool_events: list[dict[str, Any]] = []  # 已完结的 tool_call（按完结顺序）
    tool_done: set[str] = set()          # 已发 tool_call 的 callID/part.id
    file_actions: dict[str, str] = {}    # path -> action（保序）
    usage_tokens: dict[str, int] = {"input": 0, "output": 0, "reasoning": 0, "total": 0}
    usage_cache = {"read": 0, "write": 0}
    usage_cost = 0.0
    has_usage = False
    stop_reason = ""
    raw_lines: list[str] = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("{"):
            raw_lines.append(line)
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            raw_lines.append(line)
            continue
        if not isinstance(ev, dict):
            raw_lines.append(line)
            continue

        part = ev.get("part")
        if not isinstance(part, dict):
            continue
        ptype = part.get("type", "")
        pid = str(part.get("id") or "")
        ts = ev.get("timestamp")
        ts_sec = ts / 1000 if isinstance(ts, (int, float)) else 0.0

        if ptype == "text":
            text = part.get("text", "")
            if isinstance(text, str):
                key = pid or f"_anon_{len(text_parts)}"
                if key not in text_parts:
                    text_parts[key] = {"text": text, "ts": ts_sec, "order": len(text_order)}
                    text_order.append(key)
                else:
                    text_parts[key]["text"] = text

        elif ptype == "reasoning":
            text = part.get("text", "")
            if isinstance(text, str) and text.strip():
                key = pid or f"_anon_{len(reasoning_parts)}"
                if key not in reasoning_parts:
                    reasoning_parts[key] = {"text": text, "ts": ts_sec, "order": len(reasoning_order)}
                    reasoning_order.append(key)
                else:
                    reasoning_parts[key]["text"] = text

        elif ptype in ("tool", "tool_use"):
            state = part.get("state") or {}
            status = state.get("status", "")
            # 只在工具完结时记一次（pending/running 阶段跳过）
            if status not in ("completed", "error"):
                continue
            call_id = str(part.get("callID") or pid or "")
            if call_id and call_id in tool_done:
                continue
            if call_id:
                tool_done.add(call_id)
            tool_name = str(part.get("tool") or state.get("tool") or "?")
            inp = state.get("input") or {}
            if not isinstance(inp, dict):
                inp = {"_raw": inp}
            ev_tool: dict[str, Any] = {
                "type": "tool_call",
                "name": tool_name,
                "id": call_id,
                "args": inp,
                "status": status,
                "ts": ts_sec,
            }
            # file_change 映射：write → create；edit/patch → modify（modify 最强，不被 create 覆盖）
            fpath = inp.get("filePath") or inp.get("path") or ""
            if isinstance(fpath, str) and fpath:
                if tool_name in _WRITE_TOOLS:
                    if file_actions.get(fpath) != "modify":
                        file_actions[fpath] = "create"
                elif tool_name in _EDIT_TOOLS:
                    file_actions[fpath] = "modify"
            # 工具输出（截断）
            out = state.get("output")
            result_content = ""
            if isinstance(out, str) and out:
                result_content = out[:2000]
            elif isinstance(state.get("error"), str):
                result_content = state["error"][:2000]
            if result_content:
                ev_tool["result_preview"] = result_content
            tool_events.append(ev_tool)

        elif ptype == "step-finish":
            tok = part.get("tokens") or {}
            if isinstance(tok, dict):
                for k in ("input", "output", "reasoning", "total"):
                    v = tok.get(k)
                    if isinstance(v, (int, float)):
                        usage_tokens[k] += int(v)
                cache = tok.get("cache") or {}
                if isinstance(cache, dict):
                    for k in ("read", "write"):
                        v = cache.get(k)
                        if isinstance(v, (int, float)):
                            usage_cache[k] += int(v)
                has_usage = True
            cost = part.get("cost")
            if isinstance(cost, (int, float)):
                usage_cost += float(cost)
            reason = part.get("reason")
            if isinstance(reason, str) and reason:
                stop_reason = reason

        # step-start 及其它未知类型 → 跳过

    # ---- 汇总成标准化事件流（按时间戳排序，让对话过程可读）----
    timed_events: list[dict[str, Any]] = []

    for key in reasoning_order:
        p = reasoning_parts[key]
        if p["text"].strip():
            timed_events.append({
                "type": "turn",
                "role": "reasoning",
                "content": p["text"],
                "ts": p["ts"],
            })

    for key in text_order:
        p = text_parts[key]
        if p["text"].strip():
            timed_events.append({
                "type": "turn",
                "role": "assistant",
                "content": p["text"],
                "ts": p["ts"],
            })

    timed_events.extend(tool_events)
    # 按时间戳排序；时间戳相同则保持原有顺序（reasoning -> assistant -> tool）
    timed_events.sort(key=lambda e: e.get("ts", 0))

    events: list[dict[str, Any]] = list(timed_events)

    # file_change 放在对话流后面（作为汇总）
    for path, action in file_actions.items():
        events.append({
            "type": "file_change",
            "path": path,
            "action": action,
        })

    # usage（聚合所有 step_finish）
    if has_usage:
        events.append({
            "type": "usage",
            "tokens": {
                **usage_tokens,
                "cache": dict(usage_cache),
            },
            "cost": usage_cost,
        })

    # final（summary = 全部 text 拼接；降级用最后一行原始文本）
    summary = "\n\n".join(p["text"] for p in text_parts.values() if p["text"].strip()).strip()
    if not summary and raw_lines:
        summary = raw_lines[-1]
    if summary:
        events.append({
            "type": "final",
            "content": summary,
            "stop_reason": stop_reason or "stop",
        })

    artifacts = list(file_actions.keys())
    return events, summary, artifacts
