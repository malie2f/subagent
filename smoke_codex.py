"""Smoke test：mcp-hub → codex → gpt-5.6-sol 端到端。

验证：
  1) mcp-hub 通过 CodexAdapter spawn 真 codex exec 进程
  2) botcf 中转站 + gpt-5.6-sol 真能调
  3) 能从 stdout 扒出 codex 的回答
  4) 三个 model (sol/terra/luna) 都能跑
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp_hub.runtimes import detect_all


async def run_one(runtimes, model: str, task: str):
    codex = runtimes["codex"]
    print(f"\n--- test: model={model} ---")
    print(f"  task: {task}")

    t0 = time.time()
    handle = await codex.spawn(
        task_id=f"codex-smoke-{model}",
        model=model,
        task=task,
        workdir=".",
        timeout_sec=180,
    )
    print(f"  pid: {handle.pid}")

    result = await codex.wait(handle, timeout_sec=180)
    dt = time.time() - t0
    print(f"  exit_code: {result.exit_code}, duration: {result.duration_sec:.1f}s (wall {dt:.1f}s)")
    print(f"  summary: {result.summary[:200] if result.summary else '(empty)'}")
    if result.error:
        print(f"  error: {result.error[:200]}")

    if result.exit_code == 0 and result.summary:
        print(f"  ✓ PASS")
        return True
    else:
        print(f"  ✗ FAIL")
        print(f"  full stdout:")
        print("  " + (result.stdout or "")[-800:].replace("\n", "\n  "))
        return False


async def main():
    runtimes = detect_all()
    if "codex" not in runtimes:
        print("codex runtime 不可用")
        return

    codex = runtimes["codex"]
    print(f"codex info: binary={codex.binary}, available={codex.is_available()}")
    print(f"  models: {codex.list_models()}")

    # 三个 model 各跑一次
    results = []
    for model in ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]:
        ok = await run_one(
            runtimes,
            model=model,
            task="用一句话解释什么是 JSON，不要超过 30 字。",
        )
        results.append((model, ok))
        # 给 API 一点喘息时间
        await asyncio.sleep(1.0)

    print("\n=== summary ===")
    for model, ok in results:
        print(f"  {model}: {'✓' if ok else '✗'}")

    n_ok = sum(1 for _, ok in results if ok)
    if n_ok == len(results):
        print(f"\n✓ ALL PASS ({n_ok}/{len(results)})")
    else:
        print(f"\n✗ {len(results) - n_ok} FAILED")


if __name__ == "__main__":
    asyncio.run(main())
