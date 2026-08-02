"""Smoke test: 跑 kimi 子 agent，验证 stream-json 路径下的 transcript。"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main():
    workdir = Path("data/_transcript_smoke_kimi").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    print(f"workdir: {workdir}")

    from mcp_hub.runtimes import detect_all
    kimi = detect_all().get("kimi")
    if not kimi or not kimi.is_available():
        print("kimi 不可用，跳过")
        return
    print(f"kimi stream-json support: {kimi.supports_stream_json()}")

    task_id = f"kimi-transcript-{int(time.time())}"
    task = "用一句话回答：1+1=?"

    async def run_one():
        handle = await kimi.spawn(task_id, "kimi-code/kimi-for-coding", task, str(workdir), timeout_sec=180)
        print(f"spawned pid={handle.pid}")
        result = await kimi.wait(handle, 180)
        return result

    try:
        result = asyncio.run(run_one())
    except Exception as e:
        print(f"kimi run failed: {e}")
        return

    print(f"exit_code={result.exit_code}, duration={result.duration_sec:.1f}s")
    print(f"transcript events: {len(result.transcript)}")
    for ev in result.transcript[:10]:
        print(f"  - {ev.get('type')}: {json.dumps(ev, ensure_ascii=False)[:150]}")

    # 看 transcript 文件
    trans_file = workdir / ".mcp-hub" / "subagents" / f"{task_id}.transcript.jsonl"
    if trans_file.exists():
        print(f"\ntranscript file: {trans_file.stat().st_size} bytes")
        with open(trans_file, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    print(f"  {i+1}. {ev.get('type')}: {json.dumps(ev, ensure_ascii=False)[:140]}")
                except json.JSONDecodeError:
                    pass


if __name__ == "__main__":
    main()
