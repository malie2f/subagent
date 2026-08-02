"""验证 M3 + 列出 model 列表。"""

import asyncio
import json
import os

from mcp_hub.server import _init, list_models, call_model


async def main():
    # 强制重新读 .env
    os.environ["MINIMAX_MODEL"] = "MiniMax-M3"
    _init()

    print("=" * 60)
    r = await list_models()
    d = json.loads(r)
    print("[1] 已加载模型：")
    for m in d["models"]:
        print(f"  - {m['name']}: {m['model']} @ {m['base_url']}")

    print()
    print("[2] call_model minimax with M3")
    r = await call_model(
        model="minimax",
        prompt="一句话：M3 比 M2.7 主要强在哪？",
        max_tokens=2000,
    )
    d = json.loads(r)
    if d.get("ok"):
        import re
        text = re.sub(r"<think>.*?</think>", "", d["text"], flags=re.DOTALL).strip()
        print(f"  ✅ usage={d.get('usage', {})}")
        print(f"  {text[:400]}")
    else:
        print(f"  ❌ {d.get('error')}")


if __name__ == "__main__":
    asyncio.run(main())
