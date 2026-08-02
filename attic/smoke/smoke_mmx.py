"""Smoke test：mmx 真的能跑通吗？"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp_hub.config import load_settings
from mcp_hub.tools.mmx import MmxAdapter


async def main():
    s = load_settings()
    mmx = MmxAdapter(api_key=s.minimax_api_key)
    print(f"mmx api_key 注入: {bool(s.minimax_api_key)} (len={len(s.minimax_api_key) if s.minimax_api_key else 0})")
    print()

    print("=" * 50)
    print("test 1: quota (最便宜，验证 key 注入是否生效)")
    print("=" * 50)
    r = await mmx.call("quota")
    print(f"  ok = {r.ok}, duration = {r.duration_sec:.2f}s")
    if r.ok:
        import json
        d = r.data if isinstance(r.data, dict) else {}
        remains = d.get("model_remains", [])
        if remains:
            m = remains[0]
            print(f"  model={m.get('model_name')}  interval_remain={m.get('current_interval_remaining_percent')}%  weekly_remain={m.get('current_weekly_remaining_percent')}%")
        else:
            print(f"  raw: {str(r.data)[:200]}")
    else:
        print(f"  err: {r.error[:200]}")

    print()
    print("=" * 50)
    print("test 2: chat (用 MiniMax-M3 打个招呼)")
    print("=" * 50)
    r = await mmx.call("chat", message="用一句话介绍你自己", max_tokens=200)
    print(f"  ok = {r.ok}, duration = {r.duration_sec:.2f}s")
    if r.ok:
        d = r.data if isinstance(r.data, dict) else {}
        text = d.get("text") or d.get("content") or d.get("message") or d.get("response") or ""
        print(f"  text: {text[:300]}")
        if not text:
            print(f"  raw keys: {list(d.keys())[:10]}")
            print(f"  raw: {str(r.data)[:300]}")
    else:
        print(f"  err: {r.error[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
