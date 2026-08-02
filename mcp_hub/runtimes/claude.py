"""Claude Code runtime adapter —— 包装 `claude --print --add-dir ... "..."`。

实测命令（claude 2.1.207）：
    claude --print --add-dir <workdir> --model <model> \
           --dangerously-skip-permissions "task..."

关键参数：
    -p / --print              非交互模式，结果打到 stdout
    --add-dir <dir>           允许子 agent 访问的工作目录（可多次给）
    --model <model>           选模型（sonnet / opus / ... 或对方自定义）
    --dangerously-skip-permissions   自动批准所有工具调用（subagent 必须）
    --bare                    极简模式（关 hook/LSP/插件同步等），适合批跑
    --append-system-prompt    追加系统提示
    --output-format text|json|stream-json
                             输出格式（stream-json 给流式事件，json 给最终 JSON，
                             text 给纯文本）。stream-json 能拿到完整结构化事件流。
    --verbose                 配合 stream-json 时必须（否则只发最终结果）。

注意：
    - claude 默认会进交互 REPL，**必须**给 -p
    - 不给 --dangerously-skip-permissions 它会卡在权限确认（hang）
    - workdir 必须是绝对路径，--add-dir 接受多次
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
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


def _get_provider_credential(cfg: dict[str, Any], key: str) -> str:
    """读 provider 的 key / base_url：优先环境变量，其次 mcp_hub.config .env 配置。"""
    env_val = os.environ.get(cfg.get(f"{key}_env", ""), "")
    if env_val:
        return env_val
    setting_attr = cfg.get(f"{key}_setting")
    if not setting_attr:
        return ""
    try:
        from mcp_hub.config import load_settings

        s = load_settings()
        return getattr(s, setting_attr, "") or ""
    except Exception:  # noqa: BLE001
        return ""


class ClaudeAdapter(RuntimeAdapter):
    name = "claude"
    binary = "claude"

    # provider -> {api_key_env, api_key_setting, base_url_env, base_url_setting, models}
    # 模型名传给 `claude --model` 时会自动去掉 provider 前缀。
    #
    # 注意：Claude Code CLI 基于 Anthropic SDK，baseURL 不要带 /v1（SDK 自己会拼 /v1/messages）。
    # botcf 的 Anthropic 模式 baseURL = https://botcf.com；OpenAI 兼容模式才是 https://botcf.com/v1。
    # api_key_setting / base_url_setting 对应 mcp_hub.config.HubSettings 的字段名。
    _PROVIDERS: dict[str, dict[str, Any]] = {
        "anthropic": {
            "api_key_env": "ANTHROPIC_API_KEY",
            "api_key_setting": "anthropic_api_key",
            "base_url_env": "ANTHROPIC_BASE_URL",
            "base_url_setting": "anthropic_base_url",
            "models": ["sonnet", "opus", "haiku", "claude-opus-4-6"],
        },
        # botcf-claude 系列：通过 Claude Code CLI 接入，需使用 Anthropic 原生模式。
        # BotCF 文档要求：Claude Code CLI 对应的令牌必须可用 `😡Claude-Max` 分组；
        # 当前 .env 中的 key 实测属于 `🍊Claude-Kiro` 分组，用 Claude Code CLI 会报 402，
        # 但同一 key 在 OpenAI 兼容模式（opencode runtime）可正常调用。
        # 如要启用 Claude Code CLI，请去 BotCF 控制台创建 `😡Claude-Max` 分组的新 key。
        "botcf-claude": {
            "api_key_env": "BOTCF_CLAUDE_API_KEY",
            "api_key_setting": "botcf_claude_api_key",
            "base_url_env": "BOTCF_CLAUDE_BASE_URL",
            "base_url_setting": "botcf_claude_base_url",
            "models": ["claude-opus-5", "claude-opus-4-6"],
        },
        "botcf-claude-stable": {
            "api_key_env": "BOTCF_CLAUDE_STABLE_API_KEY",
            "api_key_setting": "botcf_claude_stable_api_key",
            "base_url_env": "BOTCF_CLAUDE_STABLE_BASE_URL",
            "base_url_setting": "botcf_claude_stable_base_url",
            "models": ["claude-opus-5", "claude-opus-4-6"],
        },
    }

    def __init__(self) -> None:
        super().__init__()
        self._stream_json_checked: bool | None = None

    def is_available(self) -> bool:
        # 只要 Claude Code CLI 安装了就算可用；具体某个 provider 有没有 key 在
        # list_models() 里过滤，spawn() 里再报清晰错误。
        return shutil.which(self.binary) is not None or shutil.which(self.binary + ".cmd") is not None

    def list_models(self) -> list[str]:
        """返回所有已配置 provider 的模型清单（带 provider 前缀，方便 alias 路由）。"""
        models: list[str] = []
        for provider, cfg in self._PROVIDERS.items():
            if not _get_provider_credential(cfg, "api_key"):
                continue
            for m in cfg["models"]:
                models.append(f"{provider}/{m}")
        return models

    def _parse_model(self, model: str) -> tuple[str, str]:
        """把 'provider/model' 拆成 (provider, model_name)。

        无 provider 前缀时默认走 anthropic（保持向后兼容）。
        """
        if "/" in model:
            provider, model_name = model.split("/", 1)
            if provider in self._PROVIDERS:
                return provider, model_name
        # 旧写法：直接传 sonnet/opus/haiku，走 anthropic
        return "anthropic", model

    def supports_stream_json(self) -> bool:
        """检测 claude CLI 是否支持 --output-format stream-json。

        跑 `claude --help` 抓 stdout/stderr 看有没有 'stream-json' 字样。
        """
        if self._stream_json_checked is not None:
            return self._stream_json_checked
        import subprocess
        try:
            binary = self._resolve_cmd()
            r = subprocess.run([binary, "--help"], timeout=10, capture_output=True, text=True)
            text = (r.stdout or "") + (r.stderr or "")
            self._stream_json_checked = "stream-json" in text
        except Exception:  # noqa: BLE001
            self._stream_json_checked = False
        return self._stream_json_checked

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

        provider, model_name = self._parse_model(model)
        cfg = self._PROVIDERS.get(provider)
        if cfg is None:
            raise RuntimeError(f"Claude runtime 不认识的 provider: {provider} (model={model})")

        api_key = _get_provider_credential(cfg, "api_key")
        base_url = _get_provider_credential(cfg, "base_url")
        if not api_key:
            raise RuntimeError(
                f"Claude provider '{provider}' 未配置 API key，"
                f"请在 .env 设置 {cfg['api_key_env']} 或环境变量 {cfg['api_key_env']}"
            )

        # 准备输出文件
        out_dir = Path(abs_workdir) / ".mcp-hub" / "subagents"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{task_id}.log"

        # Claude Code CLI 会读 ~/.claude/settings.json，其中 env 块的优先级高于
        # 进程环境变量。为了避免用户全局 settings.json（例如配了 DeepSeek 的 key）
        # 覆盖当前 provider 的 key，给每个子 agent 建一个隔离的 HOME/USERPROFILE，
        # 里面只放当前 provider 的 settings.json。
        home_dir = out_dir / f"claude-home-{task_id}"
        claude_config_dir = home_dir / ".claude"
        claude_config_dir.mkdir(parents=True, exist_ok=True)
        settings_file = claude_config_dir / "settings.json"
        settings = {
            "env": {
                "ANTHROPIC_AUTH_TOKEN": api_key,
            }
        }
        if base_url:
            settings["env"]["ANTHROPIC_BASE_URL"] = base_url
        settings_file.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")

        # 决定输出格式：能 stream-json 就用，否则 text
        use_stream = self.supports_stream_json()

        cmd = [
            binary,
            "--print",                        # 非交互
            "--bare",                         # 极简模式：不读 keychain，强制用 env key
            "--add-dir", abs_workdir,         # 允许子 agent 读写这个目录
            "--model", model_name,            # 选模型（去掉 provider 前缀）
            "--dangerously-skip-permissions", # 自动批准所有工具调用
        ]
        if use_stream:
            cmd += ["--output-format", "stream-json", "--verbose"]
        else:
            cmd += ["--output-format", "text"]
        cmd.append(task)                      # 任务正文（位置参数）

        # 注入当前 provider 的 key / base_url；base_url 为空时沿用 Anthropic 官方
        # Claude Code CLI 优先读取 settings.json 里的 env，所以隔离 home 是关键；
        # 同时再设一份环境变量作为双保险（ANTHROPIC_AUTH_TOKEN 是 CLI 官方认的 token 变量）。
        env = {**os.environ, cfg["api_key_env"]: api_key}
        if base_url:
            env[cfg["base_url_env"]] = base_url
        env["ANTHROPIC_AUTH_TOKEN"] = api_key
        env["ANTHROPIC_API_KEY"] = api_key
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url
        # 隔离 Claude Code CLI 的 home 目录，避免用户全局 settings.json 覆盖当前 key
        if sys.platform == "win32":
            env["USERPROFILE"] = str(home_dir)
            env["LOCALAPPDATA"] = str(home_dir / "AppData" / "Local")
            env["APPDATA"] = str(home_dir / "AppData" / "Roaming")
        else:
            env["HOME"] = str(home_dir)

        # stdout/stderr 直接重定向到日志文件（不走 PIPE）。
        # stderr 单独进 .err.log：混进 stdout 会破坏 stream-json 的
        # _looks_like_stream_json 探测（看前几行是否是 JSON）。
        log_fp, err_file, err_fp = open_subagent_logs(out_file)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=abs_workdir,
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
            workdir=abs_workdir,
            started_at=time.time(),
            process=proc,
            output_file=out_file,
            prompt=task,
            log_fp=log_fp,
            err_file=err_file,
            err_fp=err_fp,
            home_dir=home_dir,
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

        # 抽 transcript —— 看输出是 stream-json 还是 text
        if _looks_like_stream_json(clean_stdout):
            events = _parse_claude_stream_json(clean_stdout)
        else:
            summary = _extract_summary(clean_stdout)
            artifacts = _extract_artifacts(clean_stdout)
            events = _build_transcript_from_text(
                prompt=handle.prompt,
                stdout=clean_stdout,
                stderr=clean_stderr,
                artifacts=artifacts,
                summary=summary,
                exit_code=proc.returncode or 0,
            )
            summary_arg = summary
        # 统一：summary 从 events 拿或从 text 拿
        if events and events[-1].get("type") == "final":
            summary_arg = events[-1].get("content", "")
        else:
            summary_arg = _extract_summary(clean_stdout)
        artifacts = _extract_artifacts(clean_stdout)

        if handle.output_file:
            write_transcript(handle, handle.prompt, events)

        # 清理 Claude Code CLI 配置隔离用的临时 home 目录
        if handle.home_dir and handle.home_dir.exists():
            try:
                import shutil
                shutil.rmtree(handle.home_dir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass

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
                return unwrap_cmd_shim(p)  # npm shim 解开成真实 exe
        return self.binary


# ---------- 辅助函数 ----------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _extract_summary(stdout: str) -> str:
    """从 claude 输出里扒最后一段非空段当 summary。"""
    lines = [l.strip() for l in stdout.splitlines() if l.strip()]
    if not lines:
        return ""
    # 优先取最后一段明显的结论（长度 > 20、不是路径/代码块标记）
    for line in reversed(lines):
        if len(line) > 20 and not line.startswith(("```", "#", "/", "•", "-")):
            return line[:500]
    return lines[-1][:500]


def _extract_artifacts(stdout: str) -> list[str]:
    """从 claude 输出里扒它读/写的文件路径。

    claude 输出里像 "● Edited /path/to/file.py" 或 "● Created /path/..."
    """
    files: list[str] = []
    for line in stdout.splitlines():
        m = re.search(
            r"(?:Edited|Created|Wrote|Read|Updated)\s+([^\s)].+\.\w+)",
            line,
        )
        if m:
            p = m.group(1).strip().rstrip(".")
            if p and p not in files:
                files.append(p)
    return files


def _looks_like_stream_json(stdout: str) -> bool:
    """stdout 看起来是 stream-json 格式（每行是 JSON）？"""
    if not stdout.strip():
        return False
    lines = [l for l in stdout.splitlines() if l.strip()][:3]
    for line in lines:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "type" in obj:
                return True
        except json.JSONDecodeError:
            return False
    return False


def _parse_claude_stream_json(stdout: str) -> list[dict[str, Any]]:
    """把 claude --output-format stream-json 的输出解析成标准化事件流。

    stream-json 事件类型（实测）：
      - {"type":"message_start","message":{...}}
      - {"type":"content_block_start","content_block":{"type":"text","text":""}}
      - {"type":"content_block_start","content_block":{"type":"tool_use","id":"...","name":"...","input":{...}}}
      - {"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}
      - {"type":"content_block_delta","delta":{"type":"input_json_delta","partial_json":"..."}}
      - {"type":"content_block_stop","index":N}
      - {"type":"message_delta","delta":{"stop_reason":"end_turn"}}
      - {"type":"message_stop"}
      - {"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"...","content":...}]}}

    Claude Code CLI（2.x）实际还发这些外层包装事件（实测历史日志）：
      - {"type":"assistant","message":{...,"usage":{"input_tokens":N,
          "cache_creation_input_tokens":N,"cache_read_input_tokens":N,
          "output_tokens":N,...}}}   —— 同一 message.id 流式重复推，usage 是累积快照
      - {"type":"result","subtype":"success","usage":{同上, 整轮合计},
          "total_cost_usd":0.006,...} —— 最终一轮一次，是 usage 的首选来源

    我们合并 delta → 完整 text / 完整 tool_use input，输出标准化事件：
      {type:"turn", role:"assistant"|"user", content:...}
      {type:"tool_call", name, id, args}
      {type:"tool_result", tool_use_id, content}
      {type:"usage", tokens:{input/output/reasoning/total, cache:{read,write}}, cost}
      {type:"final", content, stop_reason}
    """
    events: list[dict[str, Any]] = []

    # 状态机：当前 message 累积器
    current_msg: dict[str, Any] | None = None  # {role, blocks: [...]}
    current_block: dict[str, Any] | None = None
    pending_tool_results: list[dict[str, Any]] = []
    # usage：result 事件是整轮合计（首选）；assistant message.usage 按 message.id
    # 去重留最后快照，仅在进程被杀、没有 result 时兜底求和
    result_usage: dict[str, Any] | None = None
    result_cost: float = 0.0
    msg_usage: dict[str, dict[str, Any]] = {}  # message.id -> 最后一次 usage 快照

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

        if et == "message_start":
            msg = ev.get("message", {})
            current_msg = {
                "role": msg.get("role", "assistant"),
                "blocks": [],
            }

        elif et == "content_block_start":
            cb = ev.get("content_block", {}) or {}
            cb_type = cb.get("type", "")
            if cb_type == "text":
                current_block = {"type": "text", "text": cb.get("text", "")}
            elif cb_type == "tool_use":
                current_block = {
                    "type": "tool_use",
                    "id": cb.get("id", ""),
                    "name": cb.get("name", ""),
                    "input_json": "",
                    "input": cb.get("input", {}),
                }
            else:
                current_block = {"type": cb_type, "raw": cb}

        elif et == "content_block_delta":
            d = ev.get("delta", {}) or {}
            if current_block is None:
                continue
            d_type = d.get("type", "")
            if d_type == "text_delta":
                current_block["text"] = current_block.get("text", "") + d.get("text", "")
            elif d_type == "input_json_delta":
                current_block["input_json"] = current_block.get("input_json", "") + d.get("partial_json", "")

        elif et == "content_block_stop":
            if current_block is not None and current_msg is not None:
                # 解析 tool_use 的 input_json
                if current_block.get("type") == "tool_use":
                    if current_block.get("input_json"):
                        try:
                            current_block["input"] = json.loads(current_block["input_json"])
                        except json.JSONDecodeError:
                            current_block["input"] = {"_raw": current_block["input_json"]}
                current_msg["blocks"].append(current_block)
                current_block = None

        elif et == "message_delta":
            if current_msg is not None:
                d = ev.get("delta", {}) or {}
                current_msg["stop_reason"] = d.get("stop_reason", "")

        elif et == "message_stop":
            if current_msg is not None:
                # 把累积的 blocks 转成 events
                for b in current_msg.get("blocks", []):
                    if b.get("type") == "text":
                        if b.get("text", "").strip():
                            events.append({
                                "type": "turn",
                                "role": current_msg.get("role", "assistant"),
                                "content": b["text"],
                            })
                    elif b.get("type") == "tool_use":
                        events.append({
                            "type": "tool_call",
                            "id": b.get("id", ""),
                            "name": b.get("name", ""),
                            "args": b.get("input", {}),
                        })
                if current_msg.get("role") == "assistant" and current_msg.get("stop_reason"):
                    # 这条 message 是最终回答（end_turn / tool_use）
                    # 但只有 end_turn 才是 final
                    if current_msg["stop_reason"] == "end_turn":
                        # 抽 text 拼起来当 final
                        final_text = "\n".join(
                            b.get("text", "") for b in current_msg["blocks"]
                            if b.get("type") == "text"
                        )
                        if final_text.strip():
                            events.append({
                                "type": "final",
                                "content": final_text,
                                "stop_reason": "end_turn",
                            })
                current_msg = None

        elif et == "user" or et == "user_message":
            # user message 多半是 tool_result
            msg = ev.get("message", {}) or {}
            for blk in msg.get("content", []) or []:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    pending_tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": blk.get("tool_use_id", ""),
                        "content": blk.get("content", ""),
                        "is_error": blk.get("is_error", False),
                    })

        elif et == "assistant":
            # Claude Code 外层包装事件：只取 message.usage 快照（内容走
            # content_block_* 那套状态机）；同一 message.id 会重复推，后者覆盖前者
            msg = ev.get("message")
            if isinstance(msg, dict):
                u = msg.get("usage")
                mid = msg.get("id")
                if isinstance(u, dict):
                    msg_usage[str(mid) if mid else f"_anon_{len(msg_usage)}"] = u

        elif et == "result":
            # 整轮合计，最后一次出现为准（正常一轮就一个）
            u = ev.get("usage")
            result_usage = u if isinstance(u, dict) else {}
            c = ev.get("total_cost_usd")
            result_cost = float(c) if isinstance(c, (int, float)) else 0.0

    # 兜底：把 pending_tool_results 全加进去
    events.extend(pending_tool_results)

    # usage：首选 result 的整轮合计；没有 result（进程被杀等）时按 message.id
    # 去重后的 assistant 快照求和兜底（cost 只有 result 有，兜底为 0.0）
    if result_usage is not None:
        usage_ev = _claude_usage_event(result_usage, result_cost)
    else:
        usage_ev = _claude_usage_event(_sum_message_usage(msg_usage), 0.0)
    if usage_ev is not None:
        events.append(usage_ev)

    return events


def _as_int(v: Any) -> int | None:
    """数值 → int；非数值返回 None（不编造）。"""
    return int(v) if isinstance(v, (int, float)) else None


def _sum_message_usage(msg_usage: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """把按 message.id 去重后的 usage 快照合成一个合计 dict（字段名保持源格式）。"""
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    found = False
    for u in msg_usage.values():
        for k in totals:
            v = _as_int(u.get(k))
            if v is not None:
                totals[k] += v
                found = True
    return totals if found else {}


def _claude_usage_event(usage: dict[str, Any], cost: float) -> dict[str, Any] | None:
    """Claude Code 的 usage 对象 → 标准 usage 事件。源字段全缺时返回 None。

    字段映射（Anthropic 口径：input_tokens 不含缓存读/写）：
      input_tokens                → tokens.input
      output_tokens               → tokens.output
      cache_read_input_tokens     → tokens.cache.read
      cache_creation_input_tokens → tokens.cache.write
      （无源字段）                → tokens.total = input + output + cache.read + cache.write
      （无源字段）                → tokens.reasoning = 0
      result.total_cost_usd       → cost（只有 result 事件带）
    """
    if not usage:
        return None
    inp = _as_int(usage.get("input_tokens"))
    out = _as_int(usage.get("output_tokens"))
    cr = _as_int(usage.get("cache_read_input_tokens"))
    cw = _as_int(usage.get("cache_creation_input_tokens"))
    if inp is None and out is None and cr is None and cw is None:
        return None
    inp, out, cr, cw = inp or 0, out or 0, cr or 0, cw or 0
    return {
        "type": "usage",
        "tokens": {
            "input": inp,
            "output": out,
            "reasoning": 0,
            "total": inp + out + cr + cw,
            "cache": {"read": cr, "write": cw},
        },
        "cost": float(cost),
    }


def _build_transcript_from_text(
    prompt: str,
    stdout: str,
    stderr: str,
    artifacts: list[str],
    summary: str,
    exit_code: int,
) -> list[dict[str, Any]]:
    """text 模式下的 transcript —— 用启发式抓文件改动和最终输出。"""
    events: list[dict[str, Any]] = []

    # file changes（去重，每个 path 只记一次）
    seen: set[str] = set()
    for path in artifacts:
        if path in seen:
            continue
        seen.add(path)
        events.append({
            "type": "file_change",
            "path": path,
            "action": "modify",  # text 模式下分不出 create / modify
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
