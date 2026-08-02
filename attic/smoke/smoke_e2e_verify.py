"""端到端 test: 派活（带验收） → 模拟子 agent 跑完（写 transcript） → 手动 verify。

这个不走真 cluster（dashboard 独立进程不跑 cluster），但覆盖关键数据流：
  1. POST /api/cluster/submit 发个带 acceptance 的 task
  2. 模拟 worker claim + complete（直接操作 TaskStore）
  3. 写 transcript 文件到对应 workdir
  4. POST /api/tasks/<id>/verify 写验收
  5. 检查 task 最终状态
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main():
    print("=" * 60)
    print("端到端 verify 流程（带 transcript）")
    print("=" * 60)

    from mcp_hub.config import load_settings
    from mcp_hub.queue import TaskStore
    from mcp_hub.dashboard.server import create_app

    s = load_settings()
    print(f"\n[1] queue path: {s.hub_queue_path}")
    print(f"    cluster workdir: {s.hub_cluster_workdir}")

    # 设临时 workdir
    workdir = Path("data/_e2e_verify_smoke").resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    # 起 Flask test client
    app = create_app()
    client = app.test_client()

    # 1) 通过 API 派活（带 acceptance）
    print()
    print("[2] POST /api/cluster/submit 派活...")
    payload = "把 data/_e2e_verify_smoke 目录下的 hello.txt 改成 '世界你好'"
    r = client.post("/api/cluster/submit", json={
        "payload": payload,
        "topic": "cluster.work",
        "from_model": "用户",
        "runtime": "kimi",
        "model": "kimi-code/kimi-for-coding",
        "acceptance_criteria": [
            "创建了 hello.txt",
            "内容包含 '世界'",
        ],
        "acceptance_verifier": "用户",
        "acceptance_max_iterations": 1,
        "acceptance_auto_retry": False,
    })
    data = r.get_json()
    print(f"    status: {r.status_code}, ok: {data.get('ok')}")
    print(f"    task_id: {data.get('task_id')}, has_acceptance: {data.get('has_acceptance')}")
    task_id = data["task_id"]

    # 2) 模拟 worker claim + complete + 写 transcript
    print()
    print(f"[3] 模拟 worker 接活 + 写 transcript...")
    store = TaskStore(s.hub_queue_path)
    loop = asyncio.new_event_loop()

    # claim
    task = loop.run_until_complete(store.claim("cluster.work", worker="test-worker-1"))
    if not task or task.task_id != task_id:
        # 找一下是不是没 claim 到指定的（因为 cluster.work 队列里可能有别的）
        for t in loop.run_until_complete(store.peek("cluster.work", limit=10)):
            if t.task_id == task_id:
                # 直接把它 release 让别的 worker 拿（这里是测试用，简单点改状态）
                pass
        # 重新查
        task = loop.run_until_complete(store.status(task_id))
        print(f"    任务当前状态: {task.status if task else 'not found'}")

    if task and task.task_id == task_id:
        pass
    else:
        # claim 没拿到（因为别的 task 在前）—— 直接 hack queue 文件让 task_id 进 claimed
        # 这是测试 helper，真实场景不会这么干
        queue_data = json.loads(Path(s.hub_queue_path).read_text(encoding="utf-8"))
        for t in queue_data["tasks"]:
            if t["task_id"] == task_id:
                t["status"] = "claimed"
                t["claimed_by"] = "test-worker-1"
                t["claimed_at"] = time.time()
                Path(s.hub_queue_path).write_text(json.dumps(queue_data, ensure_ascii=False, indent=2), encoding="utf-8")
                break

    # 模拟 worker 跑：写文件
    hello_file = workdir / "hello.txt"
    hello_file.write_text("世界你好", encoding="utf-8")

    # 写 transcript
    log_dir = workdir / ".mcp-hub" / "subagents"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{task_id}.log"
    trans_file = log_dir / f"{task_id}.transcript.jsonl"
    with open(log_file, "w", encoding="utf-8") as f:
        f.write("=== STDOUT ===\n[模拟子 agent 跑]\n创建了 hello.txt，内容：世界你好\n\n=== STDERR ===\n")
    with open(trans_file, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "prompt", "ts": time.time(), "content": payload}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"type": "turn", "role": "assistant", "content": "好的，我来创建文件。", "ts": time.time()}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"type": "tool_call", "id": "t1", "name": "Write", "args": {"file_path": str(hello_file), "content": "世界你好"}}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"type": "tool_result", "tool_use_id": "t1", "content": "File created successfully"}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"type": "file_change", "path": str(hello_file), "action": "create"}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"type": "final", "content": "已创建 hello.txt，内容：世界你好", "stop_reason": "end_turn"}, ensure_ascii=False) + "\n")

    # complete
    result, action = loop.run_until_complete(store.complete(
        task_id=task_id,
        worker="test-worker-1",
        result="已创建 hello.txt，内容：世界你好",
    ))
    if result:
        print(f"    complete: status={result.status}, action={action}")
    else:
        print(f"    complete: failed (action={action})")

    # 3) 看任务状态 —— 应该是 verifying（有 acceptance）
    print()
    print("[4] 查 task 状态...")
    r = client.get(f"/api/tasks/{task_id}")
    t = r.get_json()["task"]
    print(f"    status: {t['status']}")
    print(f"    has acceptance: {bool(t.get('acceptance', {}).get('criteria'))}")
    print(f"    verify_history: {len(t.get('verify_history', []))} 条")

    # 4) 看 transcript
    print()
    print("[5] GET /api/subagents/<id>/transcript...")
    r = client.get(f"/api/subagents/{task_id}/transcript")
    tr = r.get_json()
    print(f"    ok: {tr.get('ok')}, event_count: {tr.get('event_count')}")
    if tr.get("ok"):
        for ev in tr["events"][:8]:
            print(f"      - {ev.get('type')}: {json.dumps(ev, ensure_ascii=False)[:100]}")

    # 5) 看 task details（包含 transcript）
    print()
    print("[6] GET /api/tasks/<id>/details（含 transcript）...")
    r = client.get(f"/api/tasks/{task_id}/details?history_limit=2")
    d = r.get_json()
    print(f"    ok: {d.get('ok')}")
    print(f"    task.status: {d['task']['status']}")
    print(f"    transcript.event_count: {d.get('transcript', {}).get('event_count') if d.get('transcript') else 'None'}")

    # 6) verify 通过
    print()
    print("[7] POST /api/tasks/<id>/verify 验收...")
    r = client.post(f"/api/tasks/{task_id}/verify", json={
        "verifier": "用户",
        "passed": True,
        "score": 1.0,
        "issues": "看着 OK",
    })
    vd = r.get_json()
    print(f"    ok: {vd.get('ok')}, next_action: {vd.get('next_action')}")
    print(f"    task status: {vd.get('task', {}).get('status')}")
    print(f"    verify_history 条数: {len(vd.get('task', {}).get('verify_history', []))}")

    # 7) 验收不通过的分支
    print()
    print("[8] 验不通过流程：派活 + complete + verify reject (auto_retry=False)")
    r = client.post("/api/cluster/submit", json={
        "payload": "把 hello.txt 改成 '错的'",
        "topic": "cluster.work",
        "from_model": "用户",
        "runtime": "kimi",
        "model": "kimi-code/kimi-for-coding",
        "acceptance_criteria": [
            "内容包含 '世界'",
        ],
        "acceptance_verifier": "用户",
        "acceptance_max_iterations": 0,
        "acceptance_auto_retry": False,
    })
    task_id2 = r.get_json()["task_id"]
    print(f"    派 task_id2: {task_id2}")
    # 强制改 status 到 claimed 然后 complete
    task2 = loop.run_until_complete(store.status(task_id2))
    # 因为 claim 可能因为 worker_id 不匹配失败，我们直接 hack
    # 用 data 文件直接改
    queue_data = json.loads(Path(s.hub_queue_path).read_text(encoding="utf-8"))
    for t in queue_data["tasks"]:
        if t["task_id"] == task_id2:
            t["status"] = "claimed"
            t["claimed_by"] = "test-worker-1"
            t["claimed_at"] = time.time()
            Path(s.hub_queue_path).write_text(json.dumps(queue_data, ensure_ascii=False, indent=2), encoding="utf-8")
            break
    # 现在 complete
    result2, action2 = loop.run_until_complete(store.complete(
        task_id=task_id2,
        worker="test-worker-1",
        result="改成了 错的",
    ))
    print(f"    complete: status={result2.status}, action={action2}")
    # verify 不通过
    r = client.post(f"/api/tasks/{task_id2}/verify", json={
        "verifier": "用户",
        "passed": False,
        "score": 0.0,
        "issues": "内容不对，应该是 '世界你好'",
    })
    vd2 = r.get_json()
    print(f"    verify reject: ok={vd2.get('ok')}, next_action={vd2.get('next_action')}")
    print(f"    task status: {vd2.get('task', {}).get('status')}")

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)


if __name__ == "__main__":
    main()
