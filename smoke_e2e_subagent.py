"""端到端 sub-agent 测试：用 Python 直接发请求，避免 PowerShell 转义。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def main():
    import urllib.request

    print("=" * 60)
    print("Sub-agent 端到端测试（真 opencode + deepseek-v4-flash）")
    print("=" * 60)

    # 派主任务
    payload_text = """请你帮我做 3 件事：
1. 计算 17 * 23 等于多少
2. 告诉我 Python 是什么时候发布的（年份）
3. 写一行 Python 代码打印 hello world

请用 sub-agent 派 3 个 sub-agent 分别查这些事，每个 sub-agent 一个独立任务。
最后给用户一个汇总答案。"""

    body = json.dumps({
        "payload": payload_text,
        "topic": "cluster.work",
        "from_model": "用户",
        "runtime": "opencode",
        "model": "opencode-go/qwen3.7-plus",  # 用更大的模型
    }).encode("utf-8")

    req = urllib.request.Request(
        "http://127.0.0.1:8766/api/cluster/submit",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=5)
    result = json.loads(resp.read().decode("utf-8"))
    print(f"\n[1] 派主任务: {result['task_id']}")
    main_task_id = result["task_id"]

    # 轮询
    print(f"\n[2] 轮询主任务状态...")
    for i in range(60):
        req2 = urllib.request.Request(
            f"http://127.0.0.1:8766/api/cluster/task/{main_task_id}",
        )
        resp2 = urllib.request.urlopen(req2, timeout=5)
        task = json.loads(resp2.read().decode("utf-8"))["task"]
        if task["status"] in ("done", "failed"):
            print(f"  [{i*3}s] 状态: {task['status']}")
            break
        if i % 3 == 0:
            print(f"  [{i*3}s] 状态: {task['status']}, claimed_by={task.get('claimed_by')}")
        time.sleep(3)
    else:
        print("  超时（60次*3s=180s）")
        return

    print(f"\n[3] 主任务最终状态: {task['status']}")
    print(f"  payload: {task.get('payload', '')[:80]}")
    print(f"  result: {(task.get('result') or '')[:200]}")
    print(f"  sub_task_ids (metadata): {task.get('metadata', {}).get('sub_task_ids', [])}")
    sub_task_ids = task.get("metadata", {}).get("sub_task_ids", [])

    if sub_task_ids:
        print(f"\n[4] sub-tasks 状态:")
        for sid in sub_task_ids:
            req3 = urllib.request.Request(f"http://127.0.0.1:8766/api/cluster/task/{sid}")
            resp3 = urllib.request.urlopen(req3, timeout=5)
            sub = json.loads(resp3.read().decode("utf-8"))["task"]
            print(f"  - {sid[:8]}: status={sub['status']}, from_model={sub.get('from_model')}")
            print(f"    result: {(sub.get('result') or '')[:120]}")

    # 读主任务详情
    print(f"\n[5] 主任务详情（用 details 端点）...")
    req4 = urllib.request.Request(f"http://127.0.0.1:8766/api/tasks/{main_task_id}/details?history_limit=3")
    resp4 = urllib.request.urlopen(req4, timeout=5)
    details = json.loads(resp4.read().decode("utf-8"))
    if details.get("transcript"):
        events = details["transcript"].get("events", [])
        print(f"  transcript events: {len(events)}")
        for ev in events[:5]:
            print(f"    - {ev.get('type')}: {(ev.get('content') or ev.get('message') or '')[:80]}")

    print()
    if task["status"] == "done" and sub_task_ids:
        print("=" * 60)
        print("✅ 端到端 sub-agent 流程通过")
        print("=" * 60)
    else:
        print("=" * 60)
        print("⚠️  主任务没产生 sub-tasks（可能 LLM 没派）")
        print("=" * 60)


if __name__ == "__main__":
    main()
