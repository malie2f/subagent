"""DeepSeek 集群 smoke：3 worker × 4 任务，opencode-go/deepseek-v4-flash。

验证：
  - 3 个 opencode 进程能并行起
  - 任务真·调 deepseek-v4-flash
  - 任务能正确 done
  - cluster 统计准确
"""

import asyncio
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

# 强制 cluster 配置
os.environ["HUB_CLUSTER_ENABLED"] = "true"
os.environ["HUB_CLUSTER_SIZE"] = "3"
os.environ["HUB_CLUSTER_RUNTIME"] = "opencode"
os.environ["HUB_CLUSTER_MODEL"] = "opencode-go/deepseek-v4-flash"
os.environ["HUB_CLUSTER_TOPIC"] = "cluster.deepseek"
os.environ["HUB_CLUSTER_TASK_TIMEOUT_SEC"] = "180"
os.environ["HUB_CLUSTER_POLL_INTERVAL_SEC"] = "1.0"

sys.path.insert(0, str(Path(__file__).parent))

from mcp_hub.cluster import ClusterManager
from mcp_hub.queue import TaskStore
from mcp_hub.runtimes import detect_all


# 4 个不同任务，每个给 worker 一点实际工作（但都不重）
TASKS = [
    "用一句话总结 Python 的 GIL",
    "用一句话解释什么是 monorepo",
    "用一句话描述 REST API 和 GraphQL 的区别",
    "用一句话解释 Docker 容器和虚拟机的区别",
]


async def main():
    if not shutil.which("opencode"):
        print("opencode 没装")
        return

    tmp_dir = Path("./data/_test_cluster_ds")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    store = TaskStore(tmp_dir / f"tasks_{uuid.uuid4().hex[:8]}.json")

    runtimes = detect_all()
    if "opencode" not in runtimes:
        print("opencode runtime 不可用")
        return

    cm = ClusterManager(
        enabled=True, size=3, runtime_name="opencode",
        model="opencode-go/deepseek-v4-flash",
        topic="cluster.deepseek", workdir=".",
        concurrency_per_worker=1, task_timeout_sec=180,
        poll_interval_sec=1.0, store=store, runtimes=runtimes,
    )

    print("--- start cluster (3 workers) ---")
    await cm.start()
    await asyncio.sleep(0.5)
    info = cm.info()
    print(f"  size={info['size']} model={info['model']}")

    print("\n--- submit 4 tasks ---")
    t0 = time.time()
    task_ids = []
    for i, payload in enumerate(TASKS):
        t = await store.publish(topic="cluster.deepseek", payload=payload, from_model="smoke")
        task_ids.append((t.task_id, payload))

    print(f"  published {len(task_ids)} tasks")

    print("\n--- wait for completion (timeout=180s) ---")
    completed = {}
    deadline = time.time() + 180
    while len(completed) < len(task_ids) and time.time() < deadline:
        for tid, payload in task_ids:
            if tid in completed:
                continue
            t = await store.status(tid)
            if t and t.status in ("done", "failed"):
                completed[tid] = t
                dt = (t.completed_at or 0) - t.claimed_at if t.claimed_at else 0
                print(f"  [{tid}] {t.status} by={t.claimed_by} duration={dt:.1f}s")
                print(f"    prompt: {payload[:50]}")
                print(f"    result: {(t.result or '')[:120]}")
                if t.error:
                    print(f"    error: {t.error[:120]}")
        await asyncio.sleep(1.0)

    wall = time.time() - t0
    print(f"\n  wall: {wall:.1f}s")

    print("\n--- cluster stats ---")
    info = cm.info()
    for w in info["workers"]:
        s = w["stats"]
        print(f"  {w['worker_id']}: processed={s['processed']} failed={s['failed']} avg={s['avg_duration_sec']}s")

    total_ok = sum(w["stats"]["processed"] for w in info["workers"])
    total_fail = sum(w["stats"]["failed"] for w in info["workers"])
    print(f"  total: {total_ok} ok, {total_fail} failed")

    print(f"\n--- stop ---")
    await cm.stop()

    if total_ok == 4 and total_fail == 0:
        print("\n✓ PASS")
    else:
        print(f"\n✗ FAIL ({total_ok}/4 ok)")


if __name__ == "__main__":
    asyncio.run(main())
