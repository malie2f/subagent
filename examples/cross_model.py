"""最简单的跨模型调用示例。

场景：一个模型（任意 MCP 客户端）通过 mcp-hub 调用另一个模型。
本例直接用 Python SDK 模拟"用 Claude 调用 Kimi"。

前置：pip install mcp httpx
      复制 .env.example 为 .env，填入 ANTHROPIC_API_KEY 和 MOONSHOT_API_KEY

运行：python examples/cross_model.py
"""

from __future__ import annotations

import asyncio

from mcp_hub.config import load_settings
from mcp_hub.models import build_adapters
from mcp_hub.models.base import ChatRequest, Message


async def main() -> None:
    settings = load_settings()
    adapters = build_adapters(settings.model_configs())

    print(f"已加载模型：{list(adapters.keys())}")
    if "claude" not in adapters or "kimi" not in adapters:
        print("需要配置 ANTHROPIC_API_KEY 和 MOONSHOT_API_KEY")
        return

    claude = adapters["claude"]
    kimi = adapters["kimi"]

    # ===== 场景 1：Claude 直接回答 =====
    print("\n===== Claude 直接回答 =====")
    r1 = await claude.chat(
        ChatRequest(
            messages=[Message(role="user", content="用 50 字解释 MCP 协议")],
            max_tokens=256,
        )
    )
    print(f"[claude] {r1.text}\n")

    # ===== 场景 2：让 Claude 把任务派给 Kimi（长上下文 / 中文更优） =====
    print("\n===== Claude → Kimi：长文档总结 =====")
    long_text = (
        "MiniMax 稀宇科技发布 M2.5 模型，SWE-Bench Verified 达 80.2%，"
        "Multi-SWE-Bench 51.3%（多语言第一），BrowseComp 76.3%。"
        "Agent Team 用 Leader/Worker/Verifier 三层对抗架构根治长程任务退化和上下文焦虑。"
        "Token Plan + Agent Plan 合并，套餐额度跨端共享。"
    )
    prompt = f"请用中文分两点总结：\n{long_text}"
    r2 = await kimi.chat(
        ChatRequest(messages=[Message(role="user", content=prompt)], max_tokens=512)
    )
    print(f"[kimi] {r2.text}\n")

    # ===== 场景 3：两个模型协作 —— Claude 写代码，Kimi 写注释 =====
    print("\n===== Claude + Kimi 协作 =====")
    code_task = "写一个 Python 装饰器：函数执行超过 1 秒就打印告警（只给代码，不要解释）"
    r3a = await claude.chat(
        ChatRequest(
            messages=[Message(role="user", content=code_task)],
            system="你只输出代码，不输出任何 markdown 包装。",
            max_tokens=512,
        )
    )
    print(f"[claude 写代码]\n{r3a.text}\n")

    r3b = await kimi.chat(
        ChatRequest(
            messages=[
                Message(
                    role="user",
                    content=f"给下面这段 Python 代码加上中文注释和 docstring：\n```python\n{r3a.text}\n```",
                )
            ],
            max_tokens=512,
        )
    )
    print(f"[kimi 加注释]\n{r3b.text}\n")


if __name__ == "__main__":
    asyncio.run(main())
