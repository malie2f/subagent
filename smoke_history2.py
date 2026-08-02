"""test subagent history with real tasks."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, ".")

from mcp_hub.queue import TaskStore
from mcp_hub.dashboard.api import DashboardState


async def setup():
    store_file = Path("./data/_test_history.json")
    store = TaskStore(store_file)
    # publish 几个 task，让 history 有点数据
    for i in range(5):
        await store.publish(
            topic="code-review",
            payload=f"review file {i}",
            from_model="claude",
            for_model="kimi-k3",
        )
    # claim + complete 其中 2 个，模拟 history
    t1 = await store.claim("code-review", worker="kimi-1", for_model="kimi-k3")
    if t1:
        await store.complete(t1.task_id, worker="kimi-1", result="ok1")
    t2 = await store.claim("code-review", worker="kimi-1", for_model="kimi-k3")
    if t2:
        await store.complete(t2.task_id, worker="kimi-1", result="ok2")
    return store_file


async def main():
    store_file = await setup()
    print(f"setup done, store at {store_file}")
    # 用同一个 store 测试 history
    state = DashboardState()
    state._store = TaskStore(store_file)  # 覆盖默认 store
    h = state.subagent_history("notexist", limit=3)
    print(f"notexist: {h}")
    # 查一个真 task
    all_tasks = await state._store.peek("code-review", limit=10)
    if all_tasks:
        tid = all_tasks[0].task_id
        h = state.subagent_history(tid, limit=3)
        print(f"\nhistory for {tid}:")
        for k, v in h.get("history", {}).items():
            print(f"  {k}: {len(v)} items")
            for item in v[:3]:
                tid2 = item["task_id"]
                status = item["status"]
                by = item.get("claimed_by") or "-"
                print(f"    {tid2} {status} by {by}")


asyncio.run(main())
