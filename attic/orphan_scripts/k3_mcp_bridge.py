#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K3 MCP Bridge — 文本协议桥接脚本（Workaround）

Kimi Code CLI 本身不支持 MCP，本脚本通过让 K3 在回复末尾输出约定 JSON 代码块，
变相调用 mcp-hub 的工具，形成一个简单的 ReAct 循环。

限制：
  - 完全依赖 K3 听话地输出 JSON 代码块，稳定性不如原生 MCP。
  - 当前 prompt 通过命令行参数传给 kimi -p，超长对话可能触及 Windows 命令行长度限制。
  - 仅解析文本工具结果，多模态内容会被忽略或占位。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.types import TextContent

MCP_HUB_SSE_URL = "http://127.0.0.1:8765/sse"
KIMI_BIN = "kimi"
KIMI_MODEL = os.environ.get("K3_MCP_MODEL", "kimi-code/k3")
MAX_TURNS = 5
KIMI_TIMEOUT_SEC = 300  # 每轮 Kimi CLI 调用的超时


def _json_schema_to_text(schema: dict[str, Any], indent: int = 0) -> str:
    """把 JSON Schema 压缩成一行人类可读的参数说明。"""
    lines: list[str] = []
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    for name, sub in props.items():
        desc = sub.get("description", "")
        if not desc:
            desc = sub.get("title", "")
        ty = sub.get("type", "any")
        req_flag = "必填" if name in required else "可选"
        default = sub.get("default", "")
        default_flag = f"，默认={default!r}" if default != "" else ""
        lines.append(f"    - {name} ({ty}, {req_flag}{default_flag}): {desc}")
    return "\n".join(lines) if lines else "    （无参数）"


def build_tools_section(tools: list[Any]) -> str:
    """把 mcp-hub 的 tool 列表转成 prompt 里的一段说明。"""
    parts = [
        "当前可用外部工具（来自 mcp-hub）：",
        "",
    ]
    for tool in tools:
        parts.append(f"工具名：{tool.name}")
        parts.append(f"描述：{tool.description or '(无描述)'}")
        parts.append("参数：")
        parts.append(_json_schema_to_text(tool.inputSchema))
        parts.append("")
    return "\n".join(parts)


def build_system_prompt(tools: list[Any]) -> str:
    """构造给 K3 的系统指令。"""
    tools_text = build_tools_section(tools)
    return f"""你正在通过一个文本桥接协议与 mcp-hub 交互。

{tools_text}

路由规则（非常重要，严格遵循）：
- `call_model` 只能用于当前已配置的直连 API 模型。当前只有 `minimax` 能直接 call_model。
- 对于 Gemini、Opus、Claude、DeepSeek、Kimi、GPT、Qwen 等模型，必须使用 `spawn_subagent` 开 CLI 子 agent。
- Antigravity 的模型（gemini / opus / claude-sonnet-4-6 等）必须用 `spawn_subagent(runtime="antigravity", model="...")`。
- OpenCode 的模型（deepseek / kimi-k3 / qwen3.8 / minimax-m3 等）必须用 `spawn_subagent(runtime="opencode", model="opencode-go/...")`。
- 如果用户只说了模型名（如“用 gemini 总结”），优先走它对应的 runtime，不要先调 call_model。

输出规则：
1. 如果需要调用工具，请在回复末尾输出且仅输出一个 JSON 代码块（markdown triple backticks + json）：
```json
{{"tool": "spawn_subagent", "arguments": {{"runtime": "antigravity", "model": "gemini", "task": "...", "workdir": ".", "timeout_sec": 120, "wait": true}}}}
```
2. 代码块必须是回复的最后内容，前面可以写推理过程。
3. 如果不需要工具，直接给出最终答案，不要输出 JSON 代码块。
4. 拿到工具结果后，我会把结果以用户消息形式再次发给你，你继续推理。
5. 最终答案用中文回复。
"""


def parse_stream_json(raw: str) -> str:
    """解析 kimi CLI 的 stream-json 输出，拼接 assistant 文本。"""
    contents: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("role") == "assistant" and "content" in obj:
            contents.append(str(obj["content"]))
    return "".join(contents)


def extract_tool_call(text: str) -> tuple[str | None, dict[str, Any] | None]:
    """从 assistant 文本中提取最后一个 ```json ... ``` 代码块里的工具调用。"""
    # 找最后一个 json 代码块
    pattern = r"```json\s*\n(.*?)\n```"
    matches = re.findall(pattern, text, re.DOTALL)
    if not matches:
        return None, None

    last = matches[-1].strip()
    try:
        data = json.loads(last)
    except json.JSONDecodeError:
        return None, None

    if not isinstance(data, dict):
        return None, None

    tool_name = data.get("tool")
    arguments = data.get("arguments")
    if not isinstance(tool_name, str) or not isinstance(arguments, dict):
        return None, None

    return tool_name, arguments


def strip_tool_block(text: str) -> str:
    """把文本末尾的 JSON 工具代码块去掉，得到展示给用户的推理文本。"""
    pattern = r"\s*```json\s*\n.*?\n```\s*$"
    return re.sub(pattern, "", text, flags=re.DOTALL).strip()


def format_tool_result(tool_name: str, result: Any) -> str:
    """把 mcp call_tool 的结果转成文本。"""
    texts: list[str] = []
    if getattr(result, "isError", False):
        texts.append(f"[调用 {tool_name} 时出错]")

    for item in getattr(result, "content", []) or []:
        if isinstance(item, TextContent):
            texts.append(item.text)
        else:
            texts.append(f"[非文本内容: {type(item).__name__}]")

    if not texts:
        return f"[工具 {tool_name} 返回空结果]"

    return "\n".join(texts)


async def run_kimi(prompt: str, workdir: str) -> str:
    """调用 Kimi CLI 并返回原始 stream-json 文本。"""
    if not shutil.which(KIMI_BIN):
        raise RuntimeError(f"找不到 Kimi CLI: {KIMI_BIN}")

    cmd = [
        KIMI_BIN,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "-m",
        KIMI_MODEL,
        "--add-dir",
        workdir,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=KIMI_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(
            f"Kimi CLI 在 {KIMI_TIMEOUT_SEC} 秒内未返回。"
        )

    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        raise RuntimeError(
            f"Kimi CLI 退出码 {proc.returncode}\nstderr:\n{stderr_text}\nstdout:\n{stdout_text[:2000]}"
        )

    if not stdout_text.strip():
        raise RuntimeError(f"Kimi CLI 没有输出。stderr:\n{stderr_text}")

    return stdout_text


async def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python k3_mcp_bridge.py \"<prompt>\"", file=sys.stderr)
        return 1

    user_prompt = sys.argv[1]
    workdir = os.getcwd()

    # 连接 mcp-hub 并拉取工具列表
    try:
        async with sse_client(MCP_HUB_SSE_URL, timeout=5) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                tools = tools_result.tools

                system_prompt = build_system_prompt(tools)

                # 对话历史：每个元素是 (role, text)
                history: list[tuple[str, str]] = [("user", user_prompt)]
                final_answer: str | None = None

                for turn in range(MAX_TURNS):
                    # 组装 prompt
                    parts = [system_prompt, ""]
                    for role, text in history:
                        if role == "user":
                            parts.append(f"[用户]\n{text}\n")
                        else:
                            parts.append(f"[助手]\n{text}\n")
                    parts.append(
                        "[系统]\n请根据以上对话继续。如果需要工具，在回复末尾输出 JSON 代码块；"
                        "如果已能给出最终答案，直接回答。\n"
                    )
                    full_prompt = "\n".join(parts)

                    print(f"[turn {turn + 1}] 调用 Kimi CLI（prompt 长度 {len(full_prompt)}）...")
                    try:
                        raw_output = await run_kimi(full_prompt, workdir)
                    except RuntimeError as exc:
                        print(f"调用 Kimi CLI 失败：{exc}", file=sys.stderr)
                        return 1

                    assistant_text = parse_stream_json(raw_output)
                    if not assistant_text:
                        print(
                            f"未能从 stream-json 中解析出 assistant 内容。原始输出：\n{raw_output}",
                            file=sys.stderr,
                        )
                        return 1

                    tool_name, arguments = extract_tool_call(assistant_text)

                    if tool_name is None:
                        # 没有工具调用，这就是最终答案
                        final_answer = assistant_text
                        break

                    # 去掉 JSON 块，把前面的推理文本也加入历史
                    reasoning = strip_tool_block(assistant_text)
                    history.append(("assistant", reasoning))

                    # 给 mcp-hub 工具显式署名，让 dashboard 能区分调用方平台
                    # 仅当工具参数里包含 from_model 时才注入，避免 call_model 等工具校验失败
                    tool_info = next((t for t in tools if t.name == tool_name), None)
                    if tool_info and "from_model" in (tool_info.inputSchema.get("properties") or {}):
                        arguments["from_model"] = "kimicode"

                    print(f"[turn {turn + 1}] 调用工具: {tool_name}({json.dumps(arguments, ensure_ascii=False)})")

                    try:
                        result = await session.call_tool(tool_name, arguments)
                    except Exception as exc:
                        print(f"工具调用失败：{type(exc).__name__}: {exc}", file=sys.stderr)
                        return 1

                    result_text = format_tool_result(tool_name, result)
                    # 工具结果通常很长（子 agent 的完整 stdout/transcript），回喂前截断，避免
                    # 下一轮 prompt 过长导致 Kimi CLI 处理变慢或挂死。
                    if len(result_text) > 2000:
                        result_text = result_text[:2000] + "\n...（已截断，只保留前 2000 字符）"
                    history.append(("user", f"工具 {tool_name} 的返回结果：\n{result_text}"))

                if final_answer is None:
                    print("达到最大轮次，未获得最终答案。", file=sys.stderr)
                    return 1

                print(final_answer)
                return 0

    except Exception as exc:
        print(f"连接 mcp-hub 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"请确认 mcp-hub 正以 SSE 模式运行在 {MCP_HUB_SSE_URL}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
