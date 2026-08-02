"""v2 端到端 smoke：acceptance + verifier + webhook。"""

import asyncio
import json
import os

from mcp_hub.server import _init, publish_task, claim_task, complete_task, verify_task, queue_status, call_model


async def main():
    os.environ["MINIMAX_MODEL"] = "MiniMax-M3"
    _init()

    # 清理旧数据，避免前次测试残留
    from pathlib import Path
    qp = Path("./data/tasks.json")
    if qp.exists():
        qp.write_text('{"tasks":[]}', encoding="utf-8")

    # 启动一个本地 webhook 接收器
    from tests.test_smoke import _WebhookCapture
    cap = _WebhookCapture()
    cap.url = cap.start()

    try:
        print("=" * 60)
        # 1) publish + acceptance
        print("[1] publish_task with acceptance + webhook")
        r = await publish_task(
            topic="code-review",
            payload="写一个 Python 函数：输入任意嵌套列表，把所有叶子数字 +1",
            from_model="tester",
            for_model="minimax",  # 用 M3 跑
            acceptance_json=json.dumps({
                "criteria": ["代码能 import 成功", "递归正确处理 3 层嵌套"],
                "verifier": "minimax",
                "max_iterations": 2,
                "auto_retry": True,
            }),
            webhook=cap.url,
        )
        d = json.loads(r)
        print(f"  task_id={d['task_id']} has_acceptance={d['has_acceptance']} webhook={d['webhook_set']}")
        task_id = d["task_id"]

        # 2) claim
        print()
        print("[2] claim_task as minimax")
        r = await claim_task(topic="code-review", worker="minimax", for_model="minimax")
        d = json.loads(r)
        assert d.get("ok"), f"claim 失败: {d}"
        print(f"  claimed task_id={d['task']['task_id']}")

        # 3) 用 M3 真跑任务
        print()
        print("[3] M3 实际写代码")
        r = await call_model(
            model="minimax",
            prompt=(
                "写一个 Python 函数：输入任意嵌套列表，把所有叶子数字 +1。\n"
                "要求：\n"
                "- 递归实现\n"
                "- 数字（int/float）叶子 +1\n"
                "- 非数字叶子原样返回\n"
                "- 只输出代码，不要 markdown 包装"
            ),
            max_tokens=2000,
        )
        d = json.loads(r)
        if not d.get("ok"):
            print(f"  ❌ call_model 失败: {d}")
            return
        result_text = d["text"]
        # 去掉 thinking 块
        import re
        result_text = re.sub(r"<think>.*?</think>", "", result_text, flags=re.DOTALL).strip()
        print(f"  ✅ M3 输出 {len(result_text)} chars（前 200）: {result_text[:200]}")

        # 4) complete_task → 应进入 verifying
        print()
        print("[4] complete_task (因为有 acceptance，预期进入 verifying)")
        r = await complete_task(task_id=task_id, worker="minimax", result=result_text)
        d = json.loads(r)
        print(f"  ok={d['ok']} next_action={d['next_action']} status={d['task']['status']}")
        assert d["next_action"] == "verifying", f"期望 verifying，得到 {d['next_action']}"

        # 5) verify_task 通过
        print()
        print("[5] verify_task passed=True")
        r = await verify_task(
            task_id=task_id,
            verifier="claude",  # 假装是另一个 verifier
            passed=True,
            score=0.95,
            issues="",
        )
        d = json.loads(r)
        print(f"  ok={d['ok']} next_action={d['next_action']} status={d['task']['status']}")
        assert d["next_action"] == "verified"
        assert d["task"]["status"] == "done"

        # 6) 验证 webhook 收到了事件
        print()
        print("[6] 验证 webhook 收到的事件")
        await asyncio.sleep(0.3)  # 等 webhook 异步 POST 完成
        events = [r.get("event") for r in cap.received]
        print(f"  收到事件: {events}")
        assert "task.published" in events
        assert "task.completed_awaiting_verify" in events
        assert "task.verified" in events

        # 7) 失败 + 重试场景
        print()
        print("[7] 失败 + 自动重试 场景")
        r = await publish_task(
            topic="refactor",
            payload="做一些工作",
            from_model="t",
            acceptance_json=json.dumps({
                "criteria": ["good"],
                "verifier": "v",
                "max_iterations": 2,
                "auto_retry": True,
            }),
        )
        d = json.loads(r)
        tid2 = d["task_id"]

        r = await claim_task(topic="refactor", worker="w1")
        d_claim = json.loads(r)
        print(f"  claim: {d_claim}")
        cr = await complete_task(task_id=tid2, worker="w1", result="first try")
        print(f"  complete response: {cr[:300]}")
        r = await verify_task(task_id=tid2, verifier="v", passed=False, issues="太烂了")
        print(f"  raw verify response: {r[:300]}")
        d = json.loads(r)
        if "task" not in d:
            print(f"  ❌ 缺 'task' 字段: {d}")
            return
        print(f"  verify 失败 → next_action={d['next_action']} status={d['task']['status']} retries={d['task']['retries']}")
        assert d["next_action"] == "retry"
        assert d["task"]["status"] == "pending"  # 重置回 pending
        assert d["task"]["retries"] == 1

        # 8) queue status
        print()
        print("[8] queue_status 总览")
        r = await queue_status()
        d = json.loads(r)
        print(f"  total={d['stats']['total']} by_status={d['stats']['by_status']}")
        print(f"  topics={d['topics']}")

    finally:
        cap.stop()

    print()
    print("=" * 60)
    print("🎉 v2 全部跑通：acceptance + verify + webhook + retry 都 OK")


if __name__ == "__main__":
    asyncio.run(main())
