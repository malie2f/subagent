"""任务链示例：A 发布任务 → B 接任务 → B 完成后发布新任务 → C 接着干。

模拟一个真实的多模型流水线：
  Kimi（长文本）→ Claude（逻辑推理）→ GPT（生成最终交付物）

运行：
  终端 1：python examples/task_chain.py worker claude
  终端 2：python examples/task_chain.py worker kimi
  终端 3：python examples/task_chain.py worker gpt
  终端 4：python examples/task_chain.py trigger "写一篇关于 MoE 架构的科普短文"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_hub.config import ensure_queue_dir, load_settings
from mcp_hub.models import build_adapters
from mcp_hub.models.base import ChatRequest, Message
from mcp_hub.queue import TaskStore


WORKER_TOPIC = "task-chain"  # 所有 worker 监听同一个 topic


def _build():
    settings = load_settings()
    store = TaskStore(ensure_queue_dir(settings.hub_queue_path))
    adapters = build_adapters(settings.model_configs())
    return settings, store, adapters


# ---------- 触发：发起任务链 ----------

async def trigger(payload: str) -> None:
    _, store, _ = _build()
    t = await store.publish(
        topic=WORKER_TOPIC,
        payload=json.dumps(
            {"stage": "outline", "input": payload},
            ensure_ascii=False,
        ),
        from_model="user",
    )
    print(f"任务链已启动 task_id={t.task_id}，等 worker 处理…")


# ---------- Worker：循环 claim → 干 → 写回结果 → 派下一个 ----------

async def worker_loop(worker: str) -> None:
    """worker 循环：
       1) claim 一个 task
       2) 用 worker 对应的模型处理
       3) complete 当前 task
       4) 根据 stage 派下一个 task
    """
    _, store, adapters = _build()
    if worker not in adapters:
        print(f"worker {worker} 未配置，可用：{list(adapters.keys())}", file=sys.stderr)
        sys.exit(1)
    adapter = adapters[worker]
    print(f"[{worker}] 已就绪，监听 topic={WORKER_TOPIC}")

    while True:
        task = await store.claim(topic=WORKER_TOPIC, worker=worker, for_model=worker)
        if task is None:
            await asyncio.sleep(2)
            continue

        try:
            data = json.loads(task.payload)
        except Exception:
            data = {"stage": "raw", "input": task.payload}

        stage = data.get("stage")
        print(f"\n[{worker}] claim task={task.task_id} stage={stage}")

        # 简单的流水线：outline → refine → deliver
        if stage == "outline":
            # 写大纲
            prompt = f"为下面主题写一个 3 点大纲：\n{data['input']}"
            resp = await adapter.chat(
                ChatRequest(messages=[Message(role="user", content=prompt)], max_tokens=512)
            )
            data["outline"] = resp.text
            data["stage"] = "refine"
            # 让 claude 接手精修
            await store.publish(
                topic=WORKER_TOPIC,
                payload=json.dumps(data, ensure_ascii=False),
                from_model=worker,
                for_model="claude",
            )
        elif stage == "refine":
            prompt = f"基于下面大纲写一段 200 字的精炼版本：\n{data['outline']}"
            resp = await adapter.chat(
                ChatRequest(messages=[Message(role="user", content=prompt)], max_tokens=1024)
            )
            data["refined"] = resp.text
            data["stage"] = "deliver"
            await store.publish(
                topic=WORKER_TOPIC,
                payload=json.dumps(data, ensure_ascii=False),
                from_model=worker,
                for_model="gpt",
            )
        elif stage == "deliver":
            prompt = (
                f"基于下面内容，输出一份最终交付的 Markdown 文章（含标题 + 正文）：\n"
                f"大纲：{data.get('outline','')}\n正文：{data.get('refined','')}"
            )
            resp = await adapter.chat(
                ChatRequest(messages=[Message(role="user", content=prompt)], max_tokens=1024)
            )
            data["final"] = resp.text
            await store.complete(
                task_id=task.task_id,
                worker=worker,
                result=json.dumps(data, ensure_ascii=False),
            )
            print(f"\n[{worker}] 任务链完成：\n{data['final']}\n")
            continue
        else:
            # 兜底
            await store.complete(
                task_id=task.task_id,
                worker=worker,
                result=json.dumps({"raw": task.payload}, ensure_ascii=False),
            )
            continue

        await store.complete(
            task_id=task.task_id,
            worker=worker,
            result=json.dumps({"passed_to_next": True}, ensure_ascii=False),
        )


# ---------- 入口 ----------

def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pt = sub.add_parser("trigger")
    pt.add_argument("payload")

    pw = sub.add_parser("worker")
    pw.add_argument("name", help="worker 名称（必须和 .env 里的模型名一致）")

    args = p.parse_args()
    if args.cmd == "trigger":
        asyncio.run(trigger(args.payload))
    else:
        try:
            asyncio.run(worker_loop(args.name))
        except KeyboardInterrupt:
            print(f"\n[{args.name}] 退出")


if __name__ == "__main__":
    main()
