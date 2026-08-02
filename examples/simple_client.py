"""最简 MCP 客户端：直接连 stdio 上的 mcp-hub server。

不需要 LLM 也能跑：纯 Python 调 mcp-hub 的 6 个工具。

运行：
    终端 1：python -m mcp_hub.server
    终端 2：python examples/simple_client.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_hub.server"],
        cwd=str(Path(__file__).parent.parent),
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 列出所有工具
            tools = await session.list_tools()
            print(f"Hub 提供 {len(tools.tools)} 个工具：")
            for t in tools.tools:
                print(f"  - {t.name}: {t.description[:60]}…")

            # 调 list_models
            print("\n===== list_models =====")
            r = await session.call_tool("list_models", {})
            print(r.content[0].text)

            # 调 queue_status
            print("\n===== queue_status =====")
            r = await session.call_tool("queue_status", {})
            print(r.content[0].text)

            # 发布一个任务
            print("\n===== publish_task =====")
            r = await session.call_tool(
                "publish_task",
                {
                    "topic": "demo",
                    "payload": "用一句话解释 MCP 协议",
                    "from_model": "demo-client",
                },
            )
            print(r.content[0].text)

            # 尝试用 list_models 中可用的第一个模型去 claim + 处理
            models_data = json.loads(
                (await session.call_tool("list_models", {})).content[0].text
            )
            if models_data["models"]:
                model_name = models_data["models"][0]["name"]
                print(f"\n===== claim_task as {model_name} =====")
                r = await session.call_tool(
                    "claim_task",
                    {"topic": "demo", "worker": model_name},
                )
                print(r.content[0].text)

                data = json.loads(r.content[0].text)
                if data.get("ok"):
                    # 直接通过 mcp-hub 调模型处理
                    task_id = data["task"]["task_id"]
                    payload = data["task"]["payload"]
                    print(f"\n===== call_model {model_name} 处理 task =====")
                    r2 = await session.call_tool(
                        "call_model",
                        {"model": model_name, "prompt": payload, "max_tokens": 256},
                    )
                    call_data = json.loads(r2.content[0].text)
                    result_text = call_data.get("text", call_data.get("error", ""))
                    print(result_text[:300])

                    print(f"\n===== complete_task =====")
                    r3 = await session.call_tool(
                        "complete_task",
                        {
                            "task_id": task_id,
                            "worker": model_name,
                            "result": result_text,
                        },
                    )
                    print(r3.content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
