"""证明 kimi CLI 在 -p 模式下有真 agent 能力（能读/写文件、跑命令）。"""
import asyncio
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mcp_hub.runtimes import detect_all


async def main():
    if not shutil.which("kimi"):
        print("kimi 没装")
        return

    runtimes = detect_all()
    kimi = runtimes["kimi"]

    # 测试 1：让 kimi 真创建一个文件
    workdir = Path("./data/_kimi_agent_test").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    test_file = workdir / "kimi_was_here.txt"

    # 删干净
    if test_file.exists():
        test_file.unlink()

    task = (
        f"在当前目录下创建一个文件 {test_file.name}，"
        f"内容是三行：\n"
        f"line1: 我是 Kimi\n"
        f"line2: 我能写文件\n"
        f"line3: {time.time()}\n"
        f"写完后请回复'完成'。"
    )

    print(f"workdir: {workdir}")
    print(f"test_file: {test_file}")
    print(f"task: {task}")
    print()

    t0 = time.time()
    handle = await kimi.spawn(
        task_id="kimi-agent-test",
        model="kimi-code/kimi-for-coding",
        task=task,
        workdir=str(workdir),
        timeout_sec=180,
    )
    print(f"spawned pid: {handle.pid}")
    result = await kimi.wait(handle, timeout_sec=180)
    dt = time.time() - t0
    print(f"exit_code: {result.exit_code}, wall: {dt:.1f}s")
    print(f"summary: {result.summary[:200] if result.summary else '(empty)'}")
    print()

    # 看文件是否被创建
    print("=" * 50)
    print("验证文件是否真被 kimi 写出来了：")
    print("=" * 50)
    if test_file.exists():
        content = test_file.read_text(encoding="utf-8")
        print(f"  ✓ 文件存在 ({len(content)} chars)")
        print(f"  内容：")
        for line in content.splitlines():
            print(f"    {line}")
        print()
        print("结论：kimi CLI 在 -p 模式下有真·agent 能力（能用 Write 工具）")
    else:
        print(f"  ✗ 文件不存在，kimi 没真写文件")
        print(f"  stdout 末尾：")
        print("  " + (result.stdout or "")[-1000:].replace("\n", "\n  "))


if __name__ == "__main__":
    asyncio.run(main())
