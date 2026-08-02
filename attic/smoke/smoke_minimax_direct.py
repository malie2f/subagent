"""完整测一遍：用户 key + 4 个 alias 都能跑。"""

import asyncio
import json

from mcp_hub.server import _init, call_model, list_models


async def main():
    _init()

    print("=" * 60)
    r = await list_models()
    d = json.loads(r)
    print(f"[1] 已加载模型：{d['count']} 个")
    for m in d["models"]:
        print(f"  - {m['name']}: {m['model']} @ {m['base_url']}")

    print()
    print("[2] call_model('minimax', ...) — 用 Coding Plan key 直接调")
    r = await call_model(
        model="minimax",
        prompt="用一句话介绍你自己，不要加任何思考标记，直接回答。",
        max_tokens=3000,  # 留够空间
    )
    d = json.loads(r)
    if d.get("ok"):
        print(f"  ✅ provider={d['provider']} | usage={d.get('usage', {})}")
        print(f"  text: {d['text'][:500]}")
    else:
        print(f"  ❌ {d.get('error')}")

    print()
    print("[3] 写个 Python 函数测试 — 验证 coding 能力")
    r = await call_model(
        model="minimax",
        prompt="用 Python 写一个函数，输入任意嵌套列表，把所有叶子节点为数字的加 1。",
        system="只输出代码，不要 markdown 包装，不要解释。",
        max_tokens=2000,
    )
    d = json.loads(r)
    if d.get("ok"):
        print(f"  ✅ text:")
        # 提取代码
        text = d["text"]
        # 去掉 thinking 块
        import re
        code = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        print(code[:1000])


if __name__ == "__main__":
    asyncio.run(main())
