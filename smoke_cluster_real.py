"""真·cluster smoke：用真 opencode + deepseek-v4-flash 跑 2 个最简单任务。

目的：验证 cluster 端到端（不是 mock）
  - opencode 进程真能 spawn 起来
  - deepseek-v4-flash 真的能跑出结果
  - 任务并行分发
  - 集群 stats 准确
"""

import asyncio
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

# 用真 cluster 配置覆盖 .env
os.environ["HUB_CLUSTER_ENABLED"] = "true"
os.environ["HUB_CLUSTER_SIZE"] = "2"
os.environ["HUB_CLUSTER_RUNTIME"] = "opencode"
os.environ["HUB_CLUSTER_MODEL"] = "opencode-go/deepseek-v4-flash"
os.environ["HUB_CLUSTER_TOPIC"] = "cluster.smoke"
os.environ["HUB_CLUSTER_TASK_TIMEOUT_SEC"] = "180"
os.environ["HUB_CLUSTER_POLL_INTERVAL_SEC"] = "1.0"

sys.path.insert(0, str(Path(__file__).parent))

from mcp_hub.cluster import ClusterManager
from mcp_hub.queue import TaskStore
from mcp_hub.runtimes import detect_all


async def main():
    if not shutil.which("opencode"):
        print("opencode 没装，跳过真·cluster smoke")
        return

    # 临时 TaskStore
    tmp_dir = Path("./data/_test_cluster_real")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    store_file = tmp_dir / f"tasks_{uuid.uuid4().hex[:8]}.json"
    store = TaskStore(store_file)

    runtimes = detect_all()
    print(f"runtimes available: {list(runtimes.keys())}")
    if "opencode" not in runtimes:
        print("opencode runtime 不可用，跳过")
        return

    cm = ClusterManager(
        enabled=True,
        size=2,
        runtime_name="opencode",
        model="opencode-go/deepseek-v4-flash",
        topic="cluster.smoke",
        workdir=".",
        concurrency_per_worker=1,
        task_timeout_sec=180,
        poll_interval_sec=1.0,
        store=store,
        runtimes=runtimes,
    )

    print("\n--- 1) start cluster (2 workers, opencode-go/deepseek-v4-flash) ---")
    await cm.start()
    await asyncio.sleep(0.5)
    info = cm.info()
    print(f"  size={info['size']}, model={info['model']}, topic={info['topic']}")
    assert info["size"] == 2

    # 最简单的 prompt —— 让 LLM 答一句就好
    SIMPLE_PROMPT = "用一句话说'OK'，不要任何其他内容。"

    print("\n--- 2) submit 2 tasks ---")
    t0 = time.time()
    task_ids = []
    for i in range(2):
        t = await store.publish(
            topic="cluster.smoke",
            payload=SIMPLE_PROMPT,
            from_model="smoke",
        )
        task_ids.append(t.task_id)
    print(f"  published: {task_ids}")

    print("\n--- 3) wait for completion (timeout=180s) ---")
    completed = []
    deadline = time.time() + 180
    while len(completed) < 2 and time.time() < deadline:
        for tid in task_ids:
            if tid in completed:
                continue
            t = await store.status(tid)
            if t and t.status in ("done", "failed"):
                completed.append(tid)
                dt = (t.completed_at or 0) - t.claimed_at if t.claimed_at else 0
                print(f"  {tid} {t.status} by={t.claimed_by} duration={dt:.1f}s")
                result_preview = (t.result or "")[:120].replace("\n", " ")
                print(f"    result: {result_preview}")
        await asyncio.sleep(1.0)

    wall = time.time() - t0
    print(f"\n  wall: {wall:.1f}s for {len(completed)}/2 tasks")

    print("\n--- 4) cluster stats ---")
    info = cm.info()
    for w in info["workers"]:
        s = w["stats"]
        print(f"  {w['worker_id']}: processed={s['processed']} failed={s['failed']} avg={s['avg_duration_sec']}s last_err={s['last_error'][:80] if s['last_error'] else 'none'}")

    total_processed = sum(w["stats"]["processed"] for w in info["workers"])
    total_failed = sum(w["stats"]["failed"] for w in info["workers"])
    print(f"  total: {total_processed} ok, {total_failed} failed")

    if total_processed == 2 and total_failed == 0:
        print("\n✓ 真·cluster smoke PASS")
    else:
        print(f"\n✗ 真·cluster smoke FAIL ({total_processed} ok, {total_failed} failed)")

    print("\n--- 5) stop cluster ---")
    await cm.stop()
    print("  done")


if __name__ == "__main__":
    asyncio.run(main())
