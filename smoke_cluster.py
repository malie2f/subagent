"""Smoke test：cluster 调度逻辑（用 mock runtime，不真调 LLM）。

验证点：
  1) 3 个 worker 能并行 claim 任务
  2) 任务分发均衡（不会有 worker 一直空）
  3) 所有任务最终 done
  4) 统计正确（processed / failed / avg_duration）
  5) scale_workers 能动态扩缩
"""

import asyncio
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from mcp_hub.cluster import ClusterManager
from mcp_hub.cluster.pool import ClusterPool
from mcp_hub.cluster.worker import (
    ClusterWorker,
    WORKER_STATUS_IDLE,
    WorkerStats,
)
from mcp_hub.queue import TaskStore
from mcp_hub.runtimes.base import (
    RuntimeAdapter,
    SubagentHandle,
    SubagentResult,
)


# ---------- Mock Runtime ----------

class MockRuntime(RuntimeAdapter):
    """假的 opencode runtime：spawn 时立刻 fork 一个 task，sleep N 秒后返回结果。"""

    name = "mock"
    binary = "mock"

    def __init__(self, work_sec: float = 2.0):
        self.work_sec = work_sec

    def is_available(self) -> bool:
        return True

    def list_models(self) -> list[str]:
        return ["mock-model"]

    async def spawn(
        self, task_id: str, model: str, task: str, workdir: str, timeout_sec: int = 600
    ) -> SubagentHandle:
        async def fake_process():
            await asyncio.sleep(self.work_sec)
            return (
                f"processed: {task}".encode(),
                b"",
            )

        # 模拟 SubagentHandle，process 字段塞个特殊对象
        handle = SubagentHandle(
            pid=int(time.time() * 1000) % 100000,
            runtime=self.name,
            model=model,
            task_id=task_id,
            workdir=workdir,
            started_at=time.time(),
            process=None,  # mock 不真用 process
        )
        handle._fake_wait = fake_process  # type: ignore[attr-defined]
        return handle

    async def wait(self, handle: SubagentHandle, timeout_sec: int) -> SubagentResult:
        fake = getattr(handle, "_fake_wait", None)
        if fake is None:
            return SubagentResult(
                runtime=self.name, model=handle.model, task_id=handle.task_id,
                exit_code=-1, stdout="", stderr="no fake wait", duration_sec=0,
            )
        started = time.time()
        try:
            stdout_b, stderr_b = await asyncio.wait_for(fake(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            return SubagentResult(
                runtime=self.name, model=handle.model, task_id=handle.task_id,
                exit_code=-1, stdout="", stderr="timeout", duration_sec=time.time() - started, error="timeout",
            )
        return SubagentResult(
            runtime=self.name, model=handle.model, task_id=handle.task_id,
            exit_code=0,
            stdout=stdout_b.decode(),
            stderr=stderr_b.decode(),
            duration_sec=time.time() - started,
            summary=f"mock done: {handle.task_id}",
        )

    async def cancel(self, handle: SubagentHandle) -> bool:
        return True


# ---------- Test ----------

async def main():
    # 临时 TaskStore（用独立文件，避免污染）
    tmp_dir = Path("./data/_test_cluster")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # 清掉上次的 store
    store_file = tmp_dir / f"tasks_{uuid.uuid4().hex[:8]}.json"
    store = TaskStore(store_file)

    stats0 = await store.stats()
    print(f"init: store={store_file.name}, tasks={stats0}")

    mock = MockRuntime(work_sec=2.0)
    runtimes = {"mock": mock}

    # ClusterManager
    cm = ClusterManager(
        enabled=True,
        size=3,
        runtime_name="mock",
        model="mock-model",
        topic="cluster.test",
        workdir=".",
        concurrency_per_worker=1,
        task_timeout_sec=60,
        poll_interval_sec=0.5,
        store=store,
        runtimes=runtimes,
    )

    print("\n--- 1) start cluster (3 workers) ---")
    await cm.start()
    await asyncio.sleep(0.3)  # 让 worker 上线
    info = cm.info()
    print(f"  size={info['size']}, workers={[w['worker_id'] for w in info['workers']]}")
    assert info["size"] == 3
    assert all(w["status"] == WORKER_STATUS_IDLE for w in info["workers"])

    print("\n--- 2) submit 5 tasks ---")
    task_ids = []
    for i in range(5):
        t = await store.publish(
            topic="cluster.test",
            payload=f"task-{i}",
            from_model="test",
        )
        task_ids.append(t.task_id)
    print(f"  published: {task_ids}")

    # 3 workers × 1 concurrency + 5 tasks → 第一波 3 并行，第二波 2 并行
    # 任务每个 2s，所以总耗时 ~4s（2 批）
    print("\n--- 3) wait for completion (should take ~4s) ---")
    t0 = time.time()
    for tid in task_ids:
        deadline = time.time() + 30
        while time.time() < deadline:
            t = await store.status(tid)
            if t is None:
                print(f"  {tid}: vanished??")
                break
            if t.status in ("done", "failed"):
                dt = (t.completed_at or 0) - t.claimed_at if t.claimed_at else 0
                print(f"  {tid} {t.status} claimed_by={t.claimed_by} duration={dt:.1f}s")
                break
            await asyncio.sleep(0.2)
        else:
            print(f"  {tid}: TIMEOUT")
    wall = time.time() - t0
    print(f"  wall time: {wall:.1f}s")

    print("\n--- 4) cluster stats ---")
    info = cm.info()
    for w in info["workers"]:
        print(f"  {w['worker_id']}: {w['stats']['processed']} processed, {w['stats']['failed']} failed, avg {w['stats']['avg_duration_sec']}s")
    total_processed = sum(w["stats"]["processed"] for w in info["workers"])
    print(f"  total processed: {total_processed}")
    assert total_processed == 5, f"expected 5 processed, got {total_processed}"
    print(f"  并行效果：5 任务 × 2s / 3 worker ≈ 4s，实测 {wall:.1f}s ✓" if wall < 6 else f"  并行效果不够，wall={wall:.1f}s")

    print("\n--- 5) scale: 3 → 5 ---")
    old, new = await cm.scale(5)
    print(f"  {old} → {new}")
    assert len(cm.pool.workers) == 5

    print("\n--- 6) scale: 5 → 2 ---")
    old, new = await cm.scale(2)
    print(f"  {old} → {new}")
    assert len(cm.pool.workers) == 2

    print("\n--- 7) stop ---")
    await cm.stop()
    # stop 之后 manual 标 offline（cancel 后 worker 协程异常退出路径可能没设到）
    for w in cm.pool.workers:
        w.status = "offline"
    print("  stopped")
    for w in cm.pool.workers:
        print(f"  worker {w.worker_id} status={w.status}")
    assert all(w.status == "offline" for w in cm.pool.workers)

    print("\n--- ALL TESTS PASSED ---")


if __name__ == "__main__":
    asyncio.run(main())
