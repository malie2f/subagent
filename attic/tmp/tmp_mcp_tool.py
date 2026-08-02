#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时 MCP 工具调用包装脚本（绕过某些客户端对 tool name / params 的封装问题）。

用法：
    python tmp_mcp_tool.py <tool_name> '<json_args>'

例：
    python tmp_mcp_tool.py spawn_subagent '{"runtime":"claude","model":"claude-opus-5","task":"say hi","workdir":".","timeout_sec":120}'
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.sse import sse_client


SSE_URL = "http://127.0.0.1:8765/sse"


async def main(tool_name: str, args_json: str) -> None:
    args = json.loads(args_json)
    async with sse_client(SSE_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, args)
            text = "\n".join(
                c.text for c in result.content if getattr(c, "text", None)
            )
            print(text)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <tool_name> '<json_args>'", file=sys.stderr)
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
