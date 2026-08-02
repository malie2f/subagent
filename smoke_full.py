"""3 条路径都跑一遍。"""

import asyncio
import json

from mcp_hub.server import _init, call_model, spawn_subagent, list_runtimes


async def main():
    _init()

    print("=" * 60)
    print("[1] list_runtimes")
    r = await list_runtimes()
    d = json.loads(r)
    print(f"  runtimes: {d['count']} | 并发={d['max_concurrent']}")

    print()
    print("[2] 路径 A：call_model('minimax') —— 用户 key 直接调")
    r = await call_model(model="minimax", prompt="一句话：什么是闭包？", max_tokens=2000)
    d = json.loads(r)
    if d.get("ok"):
        import re
        text = re.sub(r"<think>.*?</think>", "", d["text"], flags=re.DOTALL).strip()
        print(f"  ✅ usage={d.get('usage', {})}")
        print(f"  {text[:200]}")
    else:
        print(f"  ❌ {d.get('error')}")

    print()
    print("[3] 路径 B：spawn_subagent('opencode', 'minimax') —— opencode-go 转发")
    r = await spawn_subagent(
        runtime="opencode",
        model="minimax",  # alias
        task="一句话：什么是闭包？",
        workdir=r"C:\Users\Lenovo\.mavis\agents\mavis\workspace",
        timeout_sec=60,
    )
    d = json.loads(r)
    if d.get("ok"):
        rd = d["result"]
        print(f"  ✅ exit={rd['exit_code']} dur={rd['duration_sec']:.1f}s")
        print(f"  {rd['summary'][:200]}")
    else:
        print(f"  ❌ {d.get('error', d)}")

    print()
    print("[4] 路径 C：spawn_subagent('opencode', 'deepseek') —— opencode-go 转发 DeepSeek")
    r = await spawn_subagent(
        runtime="opencode",
        model="deepseek",  # alias
        task="用 Python 写一个快速排序（10 行内）",
        workdir=r"C:\Users\Lenovo\.mavis\agents\mavis\workspace",
        timeout_sec=60,
    )
    d = json.loads(r)
    if d.get("ok"):
        rd = d["result"]
        print(f"  ✅ exit={rd['exit_code']} dur={rd['duration_sec']:.1f}s")
        print(f"  {rd['summary'][:300]}")
    else:
        print(f"  ❌ {d.get('error', d)}")


if __name__ == "__main__":
    asyncio.run(main())
