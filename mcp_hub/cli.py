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
    mcp-hub-cli service start|stop|status|restart   # 管理本地 hub / dashboard 进程
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .config import ensure_queue_dir, load_settings
from .models import build_adapters
from .models.base import ChatRequest, Message
from .queue import TaskStore

# hub 服务进程相关常量（与 start_mcp_hub.ps1 保持一致）
HUB_PORT = 8765
DASHBOARD_PORT = 8766
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"


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


# ---------- service：hub / dashboard 进程管理（同步，不走 asyncio） ----------

def _find_listeners(port: int) -> list[int]:
    """解析 netstat -ano，返回正在监听指定端口的 PID 列表（Windows）。"""
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    pids: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        # 形如：TCP    127.0.0.1:8765    0.0.0.0:0    LISTENING    16372
        if (
            len(parts) >= 5
            and parts[1].endswith(f":{port}")
            and parts[3].upper() == "LISTENING"
        ):
            try:
                pids.add(int(parts[-1]))
            except ValueError:
                pass
    return sorted(pids)


def _port_listening(port: int) -> bool:
    """TCP 探测 127.0.0.1:port 是否在监听（等价 ps1 里的 Test-PortInUse）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _spawn_detached(argv: list[str], out_log: Path, err_log: Path) -> int:
    """等价 ps1 的 Start-Process：cwd=项目根，stdout/stderr 重定向到 logs/，
    Windows 上脱离当前终端运行。返回子进程 PID。"""
    LOG_DIR.mkdir(exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        flags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    with open(out_log, "a", encoding="utf-8") as out, open(
        err_log, "a", encoding="utf-8"
    ) as err:
        proc = subprocess.Popen(
            argv, cwd=PROJECT_ROOT, stdout=out, stderr=err, creationflags=flags
        )
    return proc.pid


def _start_one(name: str, port: int, argv: list[str], out_log: str, err_log: str) -> None:
    if _port_listening(port):
        print(f"{name} 已在端口 {port} 监听，跳过启动")
        return
    pid = _spawn_detached(argv, LOG_DIR / out_log, LOG_DIR / err_log)
    print(f"{name} 已启动（端口 {port}，PID {pid}，日志 logs/{out_log} / {err_log}）")


def _stop_one(name: str, port: int) -> None:
    pids = _find_listeners(port)
    if not pids:
        print(f"{name}（端口 {port}）没有进程在监听，跳过")
        return
    for pid in pids:
        r = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True
        )
        if r.returncode == 0:
            print(f"{name}（端口 {port}）已停止，PID {pid}")
        else:
            msg = (r.stdout or r.stderr).strip()
            print(f"taskkill PID {pid} 失败：{msg}", file=sys.stderr)


def cmd_service_start(args) -> None:
    _start_one(
        "hub MCP 服务",
        HUB_PORT,
        [sys.executable, "-m", "mcp_hub", "--transport", "sse", "--port", str(HUB_PORT)],
        "hub.out.log",
        "hub.err.log",
    )
    _start_one(
        "dashboard",
        DASHBOARD_PORT,
        [sys.executable, "-m", "mcp_hub.dashboard", "--port", str(DASHBOARD_PORT)],
        "dashboard.out.log",
        "dashboard.err.log",
    )


def cmd_service_stop(args) -> None:
    _stop_one("hub MCP 服务", HUB_PORT)
    _stop_one("dashboard", DASHBOARD_PORT)


def cmd_service_status(args) -> None:
    for name, port in (("hub MCP 服务", HUB_PORT), ("dashboard", DASHBOARD_PORT)):
        pids = _find_listeners(port)
        if pids:
            print(f"{name}: 端口 {port} LISTENING，PID {', '.join(map(str, pids))}")
        else:
            print(f"{name}: 端口 {port} 未监听")
    # dashboard 在跑时顺便请求 /api/overview 打印概要，失败不崩
    if _port_listening(DASHBOARD_PORT):
        try:
            url = f"http://127.0.0.1:{DASHBOARD_PORT}/api/overview"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            summary = {}
            for k, v in data.items():
                if isinstance(v, list):
                    summary[k] = f"{len(v)} 项"
                elif isinstance(v, dict):
                    scalars = {
                        kk: vv
                        for kk, vv in v.items()
                        if isinstance(vv, (int, float, str, bool))
                    }
                    summary[k] = scalars or f"{len(v)} 个键"
                else:
                    summary[k] = v
            print("\ndashboard /api/overview 概要：")
            _print(summary)
        except Exception as e:  # noqa: BLE001
            print(f"\n（请求 /api/overview 失败：{e}）", file=sys.stderr)


def cmd_service_restart(args) -> None:
    cmd_service_stop(args)
    cmd_service_start(args)


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

    psv = sub.add_parser("service", help="管理本地 hub / dashboard 进程")
    sv = psv.add_subparsers(dest="service_cmd", required=True)
    sv.add_parser("start", help="启动 hub + dashboard（已在跑就跳过）")
    sv.add_parser("stop", help="按端口找 PID 并停止 hub + dashboard")
    sv.add_parser("status", help="查看端口监听状态 + dashboard 概要")
    sv.add_parser("restart", help="先 stop 再 start")

    args = p.parse_args()
    handlers = {
        "models": cmd_models,
        "call": cmd_call,
        "publish": cmd_publish,
        "claim": cmd_claim,
        "complete": cmd_complete,
        "status": cmd_status,
        "watch": cmd_watch,
    }
    service_handlers = {
        "start": cmd_service_start,
        "stop": cmd_service_stop,
        "status": cmd_service_status,
        "restart": cmd_service_restart,
    }
    try:
        if args.cmd == "service":
            service_handlers[args.service_cmd](args)
        else:
            asyncio.run(handlers[args.cmd](args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
