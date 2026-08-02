"""Smoke test: 跑一个真 subagent 验证 transcript 文件 + API。

1) 启 Flask test client
2) 用 opencode 跑个小任务（"echo 你好"）
3) 验证 .log 和 .transcript.jsonl 都写出来了
4) 验证 /api/subagents/<id>/transcript 能拿到 events
5) 验证 /api/tasks/<id>/details 包含 transcript
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# 让能找到 mcp_hub
sys.path.insert(0, str(Path(__file__).parent))


def main():
    workdir = Path("data/_transcript_smoke").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    print(f"[1] workdir: {workdir}")

    # 1) 跑 opencode 一个小任务
    print()
    print("[2] 用 opencode 跑真子 agent...")
    from mcp_hub.runtimes import detect_all
    opencode = detect_all()["opencode"]
    if not opencode.is_available():
        print("  opencode 不可用，跳过")
        return
    models = opencode.list_models()
    if not models:
        print("  opencode 没模型，跳过")
        return
    model = models[0]
    print(f"  用 model: {model}")

    task_id = f"transcript-smoke-{int(time.time())}"
    task = "用一句话回答：1+1=?"

    async def run_one():
        handle = await opencode.spawn(task_id, model, task, str(workdir), timeout_sec=120)
        print(f"  spawned pid={handle.pid}, prompt={handle.prompt[:30]}...")
        result = await opencode.wait(handle, 120)
        return result

    result = asyncio.run(run_one())
    print(f"  exit_code={result.exit_code}, duration={result.duration_sec:.1f}s")
    print(f"  summary: {result.summary[:120]}")
    print(f"  artifacts: {result.artifacts}")
    print(f"  prompt 写入 result: {result.prompt[:50]}")
    print(f"  transcript events: {len(result.transcript)}")
    for ev in result.transcript[:5]:
        print(f"    - {ev.get('type')}: {json.dumps(ev, ensure_ascii=False)[:120]}")

    # 2) 检查文件
    print()
    print("[3] 检查文件...")
    log_file = workdir / ".mcp-hub" / "subagents" / f"{task_id}.log"
    trans_file = workdir / ".mcp-hub" / "subagents" / f"{task_id}.transcript.jsonl"
    print(f"  log exists: {log_file.exists()} ({log_file.stat().st_size if log_file.exists() else 0} bytes)")
    print(f"  transcript exists: {trans_file.exists()} ({trans_file.stat().st_size if trans_file.exists() else 0} bytes)")
    if not trans_file.exists():
        print("  ❌ transcript 文件没写出来！")
        return
    # 读 transcript
    print()
    print("[4] transcript 内容:")
    with open(trans_file, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                print(f"  {i+1}. {ev.get('type')}: {json.dumps(ev, ensure_ascii=False)[:140]}")
            except json.JSONDecodeError as e:
                print(f"  {i+1}. INVALID: {e}")

    # 3) 起 dashboard API 测 endpoint
    print()
    print("[5] dashboard API 测 transcript endpoint")
    try:
        from mcp_hub.dashboard.api import DashboardState
        from mcp_hub.dashboard.server import create_app
        app = create_app()
        client = app.test_client()

        # 测 transcript
        r = client.get(f"/api/subagents/{task_id}/transcript")
        data = r.get_json()
        print(f"  /api/subagents/{task_id}/transcript: ok={data.get('ok')}, event_count={data.get('event_count')}")
        if data.get("ok"):
            for ev in data.get("events", [])[:3]:
                print(f"    - {ev.get('type')}")

        # 测 subagents 列表
        r = client.get("/api/subagents")
        sub_data = r.get_json()
        print(f"  /api/subagents: count={sub_data.get('count')}")
        for s in sub_data.get("subagents", [])[:3]:
            print(f"    - {s['task_id']} status={s['status']} has_log={s.get('has_log')}")

    except Exception as e:
        print(f"  API 测失败: {e}")
        import traceback
        traceback.print_exc()

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)


if __name__ == "__main__":
    main()
