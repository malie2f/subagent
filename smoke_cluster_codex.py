"""smoke_cluster_codex.py —— 端到端验证多 pool 架构（v3.2）。

目标：
  1. 起 ClusterManager（opencode + codex 两个 pool）
  2. 发一个 task 到 cluster.work.codex（for_model=codex/gpt-5.6-terra）
  3. 等 codex worker claim → fork codex CLI → 跑 → 完成
  4. 同样发一个 task 到 cluster.work（for_model=opencode/...）验证 opencode pool 还能跑
  5. dashboard /api/cluster 聚合两个 pool 信息
  6. 拿 list_workers 看 6 个 worker 都在 idle

用法：
  cd mcp-hub
  python smoke_cluster_codex.py
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# 加 cwd 到 path（保证 import mcp_hub.*）
ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

from mcp_hub.config import load_settings, ensure_queue_dir
from mcp_hub.queue import TaskStore
from mcp_hub.runtimes import detect_all
from mcp_hub.cluster import ClusterManager, PoolSpec


SIMPLE_PROMPT = """你是一个测试机器人。请直接回答（不要寒暄，不要解释）：
1+1=?

只回一个数字。"""


def log(msg, *, prefix="[smoke]"):
    print(f"{prefix} {msg}", flush=True)


async def publish_and_wait(store: TaskStore, *, topic: str, for_model: str,
                            payload: str, timeout_sec: int = 120) -> dict:
    """发布一个 task，等 done/failed/verifying。返回 task dict。"""
    log(f"publish topic={topic} for_model={for_model}")
    task = await store.publish(
        topic=topic,
        payload=payload,
        from_model="smoke_cluster_codex",
        for_model=for_model,
    )
    log(f"  -> task_id={task.task_id}")

    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        await asyncio.sleep(2.0)
        t = await store.status(task.task_id)
        if t is None:
            return {"ok": False, "error": "task vanished"}
        log(f"  status={t.status} (t={time.time()-t0:.1f}s)")
        if t.status in ("done", "failed", "verifying"):
            return {
                "ok": t.status in ("done", "verifying"),
                "task_id": task.task_id,
                "status": t.status,
                "result_preview": (t.result or "")[:200],
                "error": t.error,
                "claimed_by": t.claimed_by,
                "duration": time.time() - t0,
            }
    return {"ok": False, "error": f"timeout after {timeout_sec}s", "task_id": task.task_id}


async def main():
    s = load_settings()
    log(f"config loaded, queue={s.hub_queue_path}")
    log(f"cluster enabled={s.hub_cluster_enabled}, pools={len(s.cluster_pool_specs())}")

    store = TaskStore(ensure_queue_dir(s.hub_queue_path))
    runtimes = detect_all()
    log(f"runtimes available: {list(runtimes.keys())}")

    specs = [PoolSpec.from_dict(d) for d in s.cluster_pool_specs()]
    for sp in specs:
        log(f"  pool '{sp.name}': size={sp.size} runtime={sp.runtime} model={sp.model} topic={sp.topic}")

    mgr = ClusterManager(specs=specs, store=store, runtimes=runtimes)

    # 启动 cluster
    log("starting cluster ...")
    await mgr.start()
    log(f"started, {len(mgr.pools)} pools running")

    if len(mgr.pools) < 2:
        log(f"!! only {len(mgr.pools)} pool(s) running, expected 2", prefix="[WARN]")

    # 等待 worker 上线
    await asyncio.sleep(3.0)

    # 打印 worker 状态
    info = mgr.info()
    log(f"cluster info: pool_count={info['pool_count']}")
    for p in info["pools"]:
        log(f"  pool='{p['name']}' size={p['size']} workers_status={[w['status'] for w in p['workers']]}")

    results = {}

    # 1) codex pool 跑一个任务
    log("\n=== TEST 1: codex pool (gpt-5.6-terra) ===")
    r1 = await publish_and_wait(
        store,
        topic="cluster.work.codex",
        for_model="codex/gpt-5.6-terra",
        payload=SIMPLE_PROMPT,
        timeout_sec=180,
    )
    results["codex"] = r1
    log(f"codex result: {json.dumps(r1, ensure_ascii=False, indent=2)}")

    # 2) opencode pool 跑一个任务（验证 opencode pool 还能跑，没被破坏）
    log("\n=== TEST 2: opencode pool (deepseek-v4-flash) ===")
    r2 = await publish_and_wait(
        store,
        topic="cluster.work",
        for_model="opencode/opencode-go/deepseek-v4-flash",
        payload=SIMPLE_PROMPT,
        timeout_sec=120,
    )
    results["opencode"] = r2
    log(f"opencode result: {json.dumps(r2, ensure_ascii=False, indent=2)}")

    # 收尾
    log("\n=== final cluster state ===")
    info = mgr.info()
    for p in info["pools"]:
        ws = p["workers"]
        n_busy = sum(1 for w in ws if w["status"] == "busy")
        n_idle = sum(1 for w in ws if w["status"] == "idle")
        n_off = sum(1 for w in ws if w["status"] == "offline")
        log(f"  pool='{p['name']}' size={p['size']} busy={n_busy} idle={n_idle} offline={n_off}")
        for w in ws:
            log(f"    {w['worker_id']}: status={w['status']} processed={w['stats']['processed']} failed={w['stats']['failed']} last_task={w['stats']['last_task_id'][:8] if w['stats']['last_task_id'] else 'none'}")

    # 停 cluster
    log("\nstopping cluster ...")
    await mgr.stop()
    log("stopped")

    # 总结
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for name, r in results.items():
        if r.get("ok"):
            print(f"  [{name}] OK status={r.get('status')} claimed_by={r.get('claimed_by')} duration={r.get('duration', 0):.1f}s")
            print(f"        result: {r.get('result_preview', '')[:100]}")
        else:
            print(f"  [{name}] FAIL error={r.get('error', 'unknown')}")
    print("=" * 60)

    # 退出码
    all_ok = all(r.get("ok") for r in results.values())
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
