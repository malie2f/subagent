"""test history in sync context (like Flask does)."""
import sys
from pathlib import Path

sys.path.insert(0, ".")

from mcp_hub.queue import TaskStore
from mcp_hub.dashboard.api import DashboardState

# 准备数据
store_file = Path("./data/_test_history2.json")
store = TaskStore(store_file)

import asyncio
async def setup():
    for i in range(5):
        await store.publish(
            topic="code-review",
            payload=f"review file {i}",
            from_model="claude",
            for_model="kimi-k3",
        )
    t1 = await store.claim("code-review", worker="kimi-1", for_model="kimi-k3")
    if t1:
        await store.complete(t1.task_id, worker="kimi-1", result="ok1")
    t2 = await store.claim("code-review", worker="kimi-1", for_model="kimi-k3")
    if t2:
        await store.complete(t2.task_id, worker="kimi-1", result="ok2")
    t3 = await store.claim("code-review", worker="kimi-2", for_model="kimi-k3")
    if t3:
        await store.complete(t3.task_id, worker="kimi-2", result="ok3")
asyncio.run(setup())

# 测试 history（sync 上下文，模拟 Flask handler）
state = DashboardState()
state._store = store
all_tasks = asyncio.run(state._store.peek("code-review", limit=10))
tid = all_tasks[0].task_id
print(f"test task_id: {tid}")
h = state.subagent_history(tid, limit=5)
print(f"history ok: {h.get('ok')}")
for k, v in h.get("history", {}).items():
    print(f"  {k}: {len(v)} items")
    for item in v[:3]:
        print(f"    {item['task_id']} {item['status']} by {item.get('claimed_by')}")
