"""Kimi subagent smoke：用 kimi CLI + kimi-k3-0905-preview 跑一个简单任务。

验证：
  - kimi CLI 真能跑（不是 stub）
  - k3 model 能被接受
  - spawn → wait → 结果能拿到
"""

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp_hub.runtimes import detect_all


async def main():
    if not shutil.which("kimi"):
        print("kimi 没装")
        return

    runtimes = detect_all()
    if "kimi" not in runtimes:
        print("kimi runtime 不可用")
        return

    kimi = runtimes["kimi"]
    print(f"kimi info: binary={kimi.binary} available={kimi.is_available()}")
    print(f"  known models: {kimi.list_models()[:5]}")

    # kimi CLI 走 kimi-code OAuth，model alias 必须在 ~/.kimi/config.toml 里配过
    # 当前可用的：kimi-code/kimi-for-coding（默认）、kimi-code/kimi-for-coding-highspeed
    # 注意：kimi-k3-0905-preview 是 Moonshot API 的 model 名，kimi CLI 不识别
    model = "kimi-code/kimi-for-coding"
    task = "用一句话解释 MoE 架构"

    print(f"\n--- spawn: model={model} ---")
    print(f"  task: {task}")

    import time
    t0 = time.time()
    handle = await kimi.spawn(
        task_id="kimi-k3-smoke-1",
        model=model,
        task=task,
        workdir=".",
        timeout_sec=120,
    )
    print(f"  pid: {handle.pid}")

    result = await kimi.wait(handle, timeout_sec=120)
    dt = time.time() - t0
    print(f"\n--- result ({dt:.1f}s) ---")
    print(f"  exit_code: {result.exit_code}")
    print(f"  duration: {result.duration_sec:.1f}s")
    print(f"  summary: {result.summary[:200] if result.summary else '(empty)'}")
    if result.error:
        print(f"  error: {result.error[:200]}")
    print(f"\n  stdout (last 500 chars):")
    print("  " + (result.stdout or "")[-500:].replace("\n", "\n  "))

    if result.exit_code == 0:
        print("\n✓ PASS")
    else:
        print(f"\n✗ FAIL (exit_code={result.exit_code})")


if __name__ == "__main__":
    asyncio.run(main())
