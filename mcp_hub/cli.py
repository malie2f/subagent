"""mcp-hub-cli —— 不启 MCP server，直接在终端里调度模型和任务。

用法：
    mcp-hub-cli models
    mcp-hub-cli call kimi "用一句话介绍自己"
    mcp-hub-cli call claude "写一个 Python 装饰器：函数执行超过 1s 报警" --max-tokens 2048
    mcp-hub-cli publish <topic> "要做什么" --for kimi
    mcp-hub-cli claim <topic> --worker gpt
    mcp-hub-cli complete <task_id> --worker gpt --result "做完了"
    mcp-hub-cli status
    mcp-hub-cli watch <topic>   # 持续打印新任务
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from .config import ensure_queue_dir, load_settings
from .models import build_adapters
from .models.base import ChatRequest, Message
from .queue import TaskStore


def _print(obj) -> None:
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(obj)


def _build():
    settings = load_settings()
    store = TaskStore(ensure_queue_dir(settings.hub_queue_path))
    adapters = build_adapters(settings.model_configs())
    return settings, store, adapters


# ---------- 子命令 ----------

async def cmd_models(args) -> None:
    _, _, adapters = _build()
    print(f"已配置 {len(adapters)} 个模型：")
    for n, a in adapters.items():
        print(f"  - {n:<10} {a.config.provider:<10} {a.config.model}")


async def cmd_call(args) -> None:
    _, _, adapters = _build()
    adapter = adapters.get(args.model)
    if adapter is None:
        print(f"未找到模型 {args.model}，可用：{list(adapters.keys())}", file=sys.stderr)
        sys.exit(1)

    req = ChatRequest(
        messages=[Message(role="user", content=args.prompt)],
        max_tokens=args.max_tokens,
        temperature=args.temperature,
    )
    t0 = time.time()
    try:
        resp = await adapter.chat(req)
        dt = time.time() - t0
        if args.json:
            _print(
                {
                    "model": args.model,
                    "provider": resp.provider,
                    "text": resp.text,
                    "usage": resp.usage,
                    "elapsed_sec": round(dt, 2),
                }
            )
        else:
            print(f"\n[{args.model} | {resp.model} | {dt:.2f}s]\n{resp.text}")
    except Exception as e:  # noqa: BLE001
        print(f"调用失败：{e}", file=sys.stderr)
        sys.exit(1)


async def cmd_publish(args) -> None:
    _, store, _ = _build()
    task = await store.publish(
        topic=args.topic,
        payload=args.payload,
        from_model=args.from_,
        for_model=args.for_ or None,
        metadata=json.loads(args.metadata) if args.metadata else {},
    )
    _print({"ok": True, "task_id": task.task_id, "topic": task.topic})


async def cmd_claim(args) -> None:
    _, store, _ = _build()
    task = await store.claim(
        topic=args.topic,
        worker=args.worker,
        for_model=args.for_ or None,
    )
    if task is None:
        print("没有可认领的任务")
        return
    _print(task.to_dict())


async def cmd_complete(args) -> None:
    _, store, _ = _build()
    task, message = await store.complete(
        task_id=args.task_id,
        worker=args.worker,
        result=args.result,
        error=args.error or None,
    )
    if task is None:
        print(f"完成失败：{message}", file=sys.stderr)
        sys.exit(1)
    _print(task.to_dict())


async def cmd_status(args) -> None:
    _, store, _ = _build()
    if args.task_id:
        t = await store.status(args.task_id)
        if t is None:
            print("未找到")
            return
        _print(t.to_dict())
    else:
        stats = await store.stats()
        topics = await store.list_topics()
        _print({"stats": stats, "topics": topics})


async def cmd_watch(args) -> None:
    _, store, _ = _build()
    print(f"监听 topic={args.topic}，Ctrl-C 停止…", file=sys.stderr)
    seen: set[str] = set()
    while True:
        for t in await store.peek(args.topic, limit=20):
            if t.task_id in seen:
                continue
            seen.add(t.task_id)
            print(f"\n--- {t.task_id} | {t.created_at} ---")
            print(f"from: {t.from_model}  for: {t.for_model}")
            print(f"payload: {t.payload[:500]}")
        await asyncio.sleep(args.interval)


# ---------- 入口 ----------

def main() -> None:
    p = argparse.ArgumentParser(prog="mcp-hub-cli", description="MCP Hub 命令行客户端")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("models", help="列出所有模型")

    pc = sub.add_parser("call", help="直接调用一个模型")
    pc.add_argument("model", help="模型名（kimi / claude / gpt / minimax / ...）")
    pc.add_argument("prompt", help="提示词")
    pc.add_argument("--max-tokens", type=int, default=1024)
    pc.add_argument("--temperature", type=float, default=0.7)
    pc.add_argument("--json", action="store_true", help="输出 JSON")

    pp = sub.add_parser("publish", help="发布一个任务到队列")
    pp.add_argument("topic", help="任务主题")
    pp.add_argument("payload", help="任务内容")
    pp.add_argument("--from", dest="from_", default="cli", help="发布者")
    pp.add_argument("--for", dest="for_", default="", help="指定消费者")
    pp.add_argument("--metadata", default="", help="JSON 字符串")

    pcl = sub.add_parser("claim", help="认领一个待处理任务")
    pcl.add_argument("topic", help="任务主题")
    pcl.add_argument("--worker", required=True, help="当前 worker 标识")
    pcl.add_argument("--for", dest="for_", default="", help="当前模型名")

    pco = sub.add_parser("complete", help="完成任务")
    pco.add_argument("task_id", help="任务 ID")
    pco.add_argument("--worker", required=True, help="worker 标识（必须和 claim 时一致）")
    pco.add_argument("--result", required=True, help="结果")
    pco.add_argument("--error", default="", help="错误信息")

    ps = sub.add_parser("status", help="队列状态")
    ps.add_argument("task_id", nargs="?", default="", help="任务 ID（可选）")

    pw = sub.add_parser("watch", help="持续打印 topic 的新任务")
    pw.add_argument("topic", help="任务主题")
    pw.add_argument("--interval", type=float, default=2.0)

    args = p.parse_args()
    handler = {
        "models": cmd_models,
        "call": cmd_call,
        "publish": cmd_publish,
        "claim": cmd_claim,
        "complete": cmd_complete,
        "status": cmd_status,
        "watch": cmd_watch,
    }[args.cmd]
    try:
        asyncio.run(handler(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
