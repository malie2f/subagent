"""对照测试：mcp-hub 调 kimi 的两条路径 vs 你以为的 kimi-k3。

路径 A：mcp-hub kimi runtime → kimi CLI → kimi-code/kimi-for-coding（kimi 0.23.5 的 OAuth 模型）
路径 B：mcp-hub opencode runtime → opencode CLI → opencode-go/kimi-k3（opencode-go 套餐里的 k3）

跑同一个问题，看两个 model 的回答质量。
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp_hub.runtimes import detect_all


SAME_TASK = "你是哪个模型？请用一句话准确说出你的 model id 和提供方。"


async def run_path(name: str, runtime, model: str, task: str):
    print(f"\n{'=' * 60}")
    print(f"路径 {name}")
    print(f"  runtime: {runtime.name}, model: {model}")
    print(f"{'=' * 60}")

    t0 = time.time()
    handle = await runtime.spawn(
        task_id=f"compare-{name}",
        model=model,
        task=task,
        workdir=".",
        timeout_sec=120,
    )
    print(f"  pid: {handle.pid}")

    result = await runtime.wait(handle, timeout_sec=120)
    dt = time.time() - t0
    print(f"  exit_code: {result.exit_code}, wall: {dt:.1f}s")
    print(f"  --- answer ---")
    print(f"  {result.summary.strip() if result.summary else '(empty)'}")

    if result.error:
        print(f"  --- error ---")
        print(f"  {result.error[:200]}")
    return result


async def main():
    runtimes = detect_all()

    results = []

    # 路径 A：kimi runtime + kimi-code/kimi-for-coding
    if "kimi" in runtimes:
        r = await run_path(
            "A_kimi_cli",
            runtimes["kimi"],
            "kimi-code/kimi-for-coding",
            SAME_TASK,
        )
        results.append(("A: kimi CLI + kimi-code/kimi-for-coding", r.exit_code == 0, r.summary))
    else:
        print("kimi runtime 不可用")

    # 路径 B：opencode runtime + opencode-go/kimi-k3
    if "opencode" in runtimes:
        r = await run_path(
            "B_opencode_k3",
            runtimes["opencode"],
            "opencode-go/kimi-k3",
            SAME_TASK,
        )
        results.append(("B: opencode + opencode-go/kimi-k3", r.exit_code == 0, r.summary))
    else:
        print("opencode runtime 不可用")

    print(f"\n{'=' * 60}")
    print("总结")
    print(f"{'=' * 60}")
    for label, ok, summary in results:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {label}")
        if summary:
            # 截短到一行
            first_line = summary.strip().split("\n")[0]
            print(f"      → {first_line[:120]}")


if __name__ == "__main__":
    asyncio.run(main())
