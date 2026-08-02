#!/usr/bin/env python3
"""测试两个模型调用：GPT 5.6 Luna (codex) 和 Gemini 3.6 flash (antigravity)。"""
import asyncio
import json
from mcp import ClientSession
from mcp.client.sse import sse_client

SSE_URL = "http://127.0.0.1:8765/sse"


async def call_spawn(runtime: str, model: str, task: str, timeout: int = 180):
    async with sse_client(SSE_URL) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                "spawn_subagent",
                {
                    "runtime": runtime,
                    "model": model,
                    "task": task,
                    "workdir": ".",
                    "timeout_sec": timeout,
                    "wait": True,
                    "from_model": "test_two_models",
                },
            )
            return result


async def main():
    tests = [
        ("codex", "gpt-5.6-luna", "用一句话介绍你自己"),
        ("antigravity", "gemini-3.6-flash-medium", "用一句话介绍你自己"),
    ]
    for runtime, model, task in tests:
        print(f"\n===== 测试 {runtime} / {model} =====")
        try:
            result = await call_spawn(runtime, model, task)
            if getattr(result, "isError", False):
                print(f"[工具返回错误] {result}")
            else:
                for item in getattr(result, "content", []) or []:
                    if hasattr(item, "text"):
                        print(item.text)
                    else:
                        print(f"[非文本内容] {item}")
        except Exception as e:
            print(f"[调用异常] {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
