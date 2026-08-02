"""Grok Build runtime adapter —— 包装 `grok -p "<task>" --output-format json`。

实测命令（Grok Build 0.2.118）：
    grok.exe -p "只回复 ok" --always-approve --output-format json --cwd <dir>

关键参数：
    -p <task>            headless 单轮任务（内部仍支持 agentic 工具循环，实测能写文件）
    --always-approve     自动批准权限（非交互模式必需）
    --output-format json stdout 输出单个 JSON 对象（不是 JSONL）：
        {"text", "stopReason", "sessionId", "requestId", "thought",
         "usage": {"input_tokens", "output_tokens", "reasoning_tokens",
                   "total_tokens", "cache_read_input_tokens",
                   "cache_creation_input_tokens"},
         "num_turns", "total_cost_usd",
         "modelUsage": {"grok-4.5-build": {...}}}
    --cwd <dir>          工作目录
    -m <model>           模型（默认 grok-4.5，计费名 grok-4.5-build）
    --reasoning-effort   low / medium / high（默认 high）
    --resume <sessionId> 续跑同一 session
    --prompt-file <PATH> 从文件读任务（备选，避免命令行过长）

注意：
    - 二进制不在 PATH：默认在 ~/.grok/bin/grok.exe（真实 exe，无 npm shim 问题）
    - 网络必须走代理（美国服务）：复用 antigravity 的 _antigravity_env() 注入
      HTTP_PROXY/HTTPS_PROXY（127.0.0.1:17891 等本地代理）
    - 按量计费（total_cost_usd 非零），别拿它跑大回归

解析策略（wait）：
    - stdout 容错取第一个 '{' 到最后 '}' 解析成单个 JSON
    - thought -> turn(reasoning)；text -> turn(assistant) + final 事件（summary）
    - usage -> usage 事件（cost = total_cost_usd）
    - 解析失败降级：summary = stdout 尾部非空文本
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .antigravity import _antigravity_env
from .base import (
    RuntimeAdapter,
    SubagentHandle,
    SubagentResult,
    open_subagent_logs,
    wait_and_collect,
    write_transcript,
)

# list_models 的缓存 TTL（秒）
_PROBE_TTL = 300.0

# 默认模型（models_cache.json 读取失败时回退）
_FALLBACK_MODELS = ["grok-4.5"]


class GrokAdapter(RuntimeAdapter):
    name = "grok"
    binary = "grok"
    supports_resume = True

    def __init__(self):
        super().__init__()
        self._models_cache: list[str] | None = None
        self._models_cached_at: float = 0.0

    # ---- 可用性 / 模型列表 ----

    def is_available(self) -> bool:
        """grok 不在 PATH，看默认安装路径 ~/.grok/bin/grok.exe 是否存在。"""
        p = self._resolve_cmd()
        if p == self.binary:
            return False
        return Path(p).is_file()

    def list_models(self) -> list[str]:
        """读 ~/.grok/models_cache.json 的 models 键（带 TTL 缓存）。"""
        now = time.time()
        if self._models_cache is not None and now - self._models_cached_at < _PROBE_TTL:
            return list(self._models_cache)
        models: list[str] = []
        try:
            cache_file = Path.home() / ".grok" / "models_cache.json"
            data = json.loads(cache_file.read_text(encoding="utf-8", errors="replace"))
            raw = data.get("models") if isinstance(data, dict) else None
            if isinstance(raw, dict):
                models = [str(k) for k in raw.keys() if k]
            elif isinstance(raw, list):
                models = [
                    str(m.get("id") or m.get("name"))
                    for m in raw
                    if isinstance(m, dict) and (m.get("id") or m.get("name"))
                ]
        except Exception:  # noqa: BLE001
            models = []
        if not models:
            models = list(_FALLBACK_MODELS)
        self._models_cache = models
        self._models_cached_at = now
        return list(models)

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
        """fork 一个 `grok -p ... --output-format json` 进程。"""
        out_dir = Path(workdir) / ".mcp-hub" / "subagents"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{task_id}.log"

        cmd = self._build_cmd(model, task, workdir, reasoning_effort)
        env = _antigravity_env()  # grok 是美国服务，必须走代理

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

    async def wait(self, handle: SubagentHandle, timeout_sec: int) -> SubagentResult:
        """等 grok 跑完，解析单个 JSON 输出。"""
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
        transcript, summary, artifacts = _parse_grok_json(clean_stdout)
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

    # ---- 超时断线续跑（grok 原生支持 --resume） ----

    def extract_session_id(self, handle: SubagentHandle) -> str | None:
        """从日志（JSON 里带 "sessionId":"..."）里提取 session id。"""
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
        """用 --resume 续跑同一 session（task 由 server 拼好）。"""
        out_dir = Path(workdir) / ".mcp-hub" / "subagents"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{task_id}.log"

        cmd = self._build_cmd(model, task, workdir, "", resume=session_id)
        env = _antigravity_env()

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
        """grok 不在 PATH：先 which 探测，再回退默认安装路径 ~/.grok/bin/grok.exe。"""
        for cand in [self.binary, self.binary + ".exe"]:
            p = shutil.which(cand)
            if p:
                return p
        default = Path.home() / ".grok" / "bin" / "grok.exe"
        if default.is_file():
            return str(default)
        return self.binary  # fallback（is_available 会据此判 False）

    def _build_cmd(
        self,
        model: str,
        task: str,
        workdir: str,
        reasoning_effort: str,
        resume: str = "",
    ) -> list[str]:
        exe = self._resolve_cmd()
        cmd = [exe]
        if resume:
            cmd += ["--resume", resume]
        cmd += ["-p", task, "--always-approve", "--output-format", "json"]
        # 去掉 "grok/" 前缀；空 model 用 CLI 默认
        m = model.split("/", 1)[1] if model.startswith("grok/") else model
        if m:
            cmd += ["-m", m]
        if reasoning_effort in ("low", "medium", "high"):
            cmd += ["--reasoning-effort", reasoning_effort]
        cmd += ["--cwd", str(Path(workdir).resolve())]
        return cmd


# ---------- 辅助函数 ----------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SESSION_ID_RE = re.compile(r'"sessionId"\s*:\s*"([^"]+)"')


def _clean_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _extract_json_obj(stdout: str) -> dict[str, Any] | None:
    """stdout 应该是一整个 JSON 对象；容错取第一个 '{' 到最后 '}'。"""
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(stdout[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _parse_grok_json(stdout: str) -> tuple[list[dict[str, Any]], str, list[str]]:
    """把 `grok --output-format json` 的单个 JSON 输出解析成标准化事件流。

    返回 (events, summary, artifacts)：
      - events: turn(reasoning) / turn(assistant) / usage / final
      - summary: text 字段；没有时降级用 stdout 尾部非空文本
      - artifacts: 空（单 JSON 输出里没有结构化工具调用记录）
    """
    obj = _extract_json_obj(stdout)
    if obj is None:
        summary = ""
        for line in reversed(stdout.splitlines()):
            if line.strip():
                summary = line.strip()
                break
        events: list[dict[str, Any]] = []
        if summary:
            events.append({"type": "final", "content": summary, "stop_reason": "unknown"})
        return events, summary, []

    events = []

    thought = obj.get("thought")
    if isinstance(thought, str) and thought.strip():
        events.append({"type": "turn", "role": "reasoning", "content": thought})

    text = obj.get("text")
    summary = text.strip() if isinstance(text, str) else ""
    if summary:
        events.append({"type": "turn", "role": "assistant", "content": summary})

    usage = obj.get("usage")
    if isinstance(usage, dict):
        tokens: dict[str, Any] = {}
        for src, dst in (
            ("input_tokens", "input"),
            ("output_tokens", "output"),
            ("reasoning_tokens", "reasoning"),
            ("total_tokens", "total"),
        ):
            v = usage.get(src)
            if isinstance(v, (int, float)):
                tokens[dst] = int(v)
        cache: dict[str, int] = {}
        for src, dst in (
            ("cache_read_input_tokens", "read"),
            ("cache_creation_input_tokens", "write"),
        ):
            v = usage.get(src)
            if isinstance(v, (int, float)):
                cache[dst] = int(v)
        if cache:
            tokens["cache"] = cache
        cost = obj.get("total_cost_usd")
        events.append({
            "type": "usage",
            "tokens": tokens,
            "cost": float(cost) if isinstance(cost, (int, float)) else 0.0,
        })

    stop_reason = obj.get("stopReason")
    if summary:
        events.append({
            "type": "final",
            "content": summary,
            "stop_reason": stop_reason if isinstance(stop_reason, str) and stop_reason else "stop",
        })

    return events, summary, []
