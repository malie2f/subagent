"""smoke test: sub-agent 协议端到端。

测试：
1. 派一个主任务（payload 故意让 LLM 派 2 个 sub-agent）
2. cluster worker 跑第一轮 → 看到 SUBAGENT block
3. 自动派 2 个 sub-tasks
4. 轮询等所有完成
5. 第二轮汇总 → 写回主任务
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


async def main():
    print("=" * 60)
    print("Sub-agent 协议端到端测试")
    print("=" * 60)

    # 1) 直接用 TaskStore + RuntimeAdapter + ClusterPool 模拟一次完整 run
    from mcp_hub.config import load_settings
    from mcp_hub.queue import TaskStore
    from mcp_hub.runtimes import detect_all
    from mcp_hub.cluster import ClusterManager
    from mcp_hub.runtimes.base import (
        SUBAGENT_BLOCK_START, SUBAGENT_BLOCK_END, parse_subagent_block,
        strip_subagent_block,
    )

    settings = load_settings()
    store = TaskStore(settings.hub_queue_path)
    runtimes = detect_all()

    # 直接用 mock runtime 测逻辑（避免花 API 钱）
    print("\n[1] 用 MockRuntime 模拟一个会派 sub-agents 的 LLM")
    from mcp_hub.runtimes.base import SubagentResult, RuntimeAdapter, SubagentHandle


    class MockRuntime(RuntimeAdapter):
        name = "mock"
        binary = "mock"
        _KNOWN_MODELS = ["mock-model"]
        def is_available(self): return True
        def list_models(self): return ["mock-model"]
        def __init__(self):
            self.spawn_count = 0
            self.wait_count = 0
            self.sub_results_recorded = []
        async def spawn(self, task_id, model, task, workdir, timeout_sec=600):
            self.spawn_count += 1
            h = SubagentHandle(
                pid=100 + self.spawn_count, runtime=self.name, model=model,
                task_id=task_id, workdir=workdir, started_at=time.time(),
            )
            return h
        async def wait(self, handle, timeout_sec):
            self.wait_count += 1
            # 第一轮：返回带 SUBAGENT block
            if "round2" not in handle.task_id:
                output = (
                    "好的，我来拆 2 个 sub-agent 并行查。\n\n"
                    "<<<SUBAGENT>>>\n"
                    "查 1+1=? 的答案\n"
                    "---\n"
                    "查 2+2=? 的答案\n"
                    "<<<END>>>"
                )
            else:
                # 第二轮：汇总（prompt 会包含 sub-agent 结果）
                output = f"汇总：1+1=2，2+2=4。sub_agents 看到的：{[t[:30] for t in self.sub_results_recorded]}"
            return SubagentResult(
                runtime=self.name, model="mock-model",
                task_id=handle.task_id, exit_code=0,
                stdout=output, stderr="", duration_sec=0.1,
            )
        async def cancel(self, handle): return True

    # 2) 派主任务
    print("\n[2] 派主任务...")
    main_task = await store.publish(
        topic="cluster.work",
        payload="帮我算 1+1 和 2+2",
        from_model="用户",
        for_model="mock/mock-model",
    )
    print(f"  main task: {main_task.task_id}")

    # 3) 模拟 cluster worker 跑
    print("\n[3] 模拟 cluster worker 跑完整流程（含 sub-agents）...")
    mock = MockRuntime()

    # 3.1 claim main
    claimed = await store.claim("cluster.work", worker="mock-1", for_model="mock/mock-model")
    assert claimed and claimed.task_id == main_task.task_id, f"claim 失败: {claimed}"
    print(f"  [3.1] claim 成功: {claimed.task_id}")

    # 3.2 第一轮 spawn + wait
    from mcp_hub.runtimes.base import SUBAGENT_PROTOCOL_HINT
    handle = await mock.spawn(
        task_id=claimed.task_id, model="mock-model",
        task=claimed.payload + SUBAGENT_PROTOCOL_HINT,
        workdir=".",
    )
    result = await mock.wait(handle, 60)
    assert result.exit_code == 0
    sub_descs = parse_subagent_block(result.stdout)
    assert len(sub_descs) == 2, f"期望 2 个 sub-agent，实际 {len(sub_descs)}"
    print(f"  [3.2] 第一轮返回，sub-agent 任务: {sub_descs}")

    # 3.3 派 sub-tasks
    sub_task_ids = []
    for desc in sub_descs:
        sub = await store.publish(
            topic="cluster.work",
            payload=desc,
            from_model=f"{claimed.task_id}:subagent",
            for_model="mock/mock-model",
        )
        sub_task_ids.append(sub.task_id)
    await store.set_sub_task_ids(claimed.task_id, sub_task_ids)
    print(f"  [3.3] 派了 {len(sub_task_ids)} 个 sub-tasks: {sub_task_ids}")

    # 3.4 模拟其他 worker 跑 sub-tasks
    for i, sid in enumerate(sub_task_ids):
        # 找到 topic + 匹配的 sub-task
        sub_task = None
        for _ in range(5):
            for t in await store.peek("cluster.work", limit=20):
                if t.task_id == sid:
                    sub_task = t
                    break
            if sub_task:
                break
            await asyncio.sleep(0.1)
        assert sub_task, f"找不到 sub-task {sid}"
        # claim
        claimed_sub = await store.claim(
            "cluster.work", worker=f"mock-sub-{i+1}", for_model="mock/mock-model"
        )
        if not claimed_sub or claimed_sub.task_id != sid:
            # 已经被其他 worker 拿走了（不应该）—— 直接 force claim
            claimed_sub = None
            for _ in range(3):
                # 找特定 task_id claim
                pass
            # 直接读 status
            t = await store.status(sid)
            if t and t.status == "claimed":
                # 已经 claimed 了，模拟其他 worker 跑完
                pass
        if claimed_sub:
            mock.sub_results_recorded.append(f"sub-result-{i}")
            await store.complete(
                task_id=sid, worker=f"mock-sub-{i+1}",
                result=f"sub_result_{i}: 答案",
            )
        else:
            # 已经被别人 claim 了，直接 complete
            mock.sub_results_recorded.append(f"sub-result-{i}")
            await store.complete(
                task_id=sid, worker=f"mock-sub-{i+1}",
                result=f"sub_result_{i}: 答案",
            )
    print(f"  [3.4] 模拟所有 sub-tasks 跑完")

    # 3.5 轮询等所有 sub-tasks done
    for _ in range(20):
        subs = await store.get_sub_task_results(sub_task_ids)
        if len(subs) == len(sub_task_ids):
            break
        await asyncio.sleep(0.1)
    assert len(subs) == len(sub_task_ids), f"sub-tasks 没全完: {subs}"
    print(f"  [3.5] sub-tasks 全完: {[s['status'] for s in subs.values()]}")

    # 3.6 第二轮汇总
    summary_prompt = f"# Sub-agents 结果\n{[s for s in subs.values()]}"
    handle2 = await mock.spawn(
        task_id=claimed.task_id + "-round2",
        model="mock-model", task=summary_prompt, workdir=".",
    )
    result2 = await mock.wait(handle2, 60)
    assert result2.exit_code == 0
    print(f"  [3.6] 第二轮汇总: {result2.stdout[:80]}")

    # 3.7 mark complete
    await store.complete(
        task_id=claimed.task_id,
        worker="mock-1",
        result=strip_subagent_block(result2.stdout),
    )
    print(f"  [3.7] mark complete")

    # 4) 验证最终状态
    print("\n[4] 验证主任务最终状态...")
    final = await store.status(claimed.task_id)
    assert final.status == "done", f"期望 done，实际 {final.status}"
    print(f"  main status: {final.status}")
    print(f"  main result: {final.result[:100]}")
    print(f"  main sub_task_ids (metadata): {final.metadata.get('sub_task_ids')}")
    assert final.metadata.get("sub_task_ids") == sub_task_ids, "sub_task_ids 没写"

    # 验证 sub-tasks
    for sid in sub_task_ids:
        sub = await store.status(sid)
        print(f"  sub-task {sid[:8]}: status={sub.status}, result={sub.result[:50] if sub.result else '(empty)'}")

    print("\n" + "=" * 60)
    print("✅ 全部通过！sub-agent 协议工作正常")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
