"""Flask dashboard server + CLI 命令。

Web: python -m mcp_hub.dashboard
CLI: python -m mcp_hub.dashboard status
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Flask 是可选依赖（dashboard 才用，mcp-hub 核心不依赖）
try:
    from flask import Flask, jsonify, request, Response, send_from_directory
except ImportError:
    Flask = None  # type: ignore[assignment]


from .api import DashboardState


def create_app() -> "Flask":
    """建 Flask app。"""
    if Flask is None:
        raise RuntimeError("需要 flask：pip install flask")
    app = Flask(
        "mcp-hub-dashboard",
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    state = DashboardState()

    @app.after_request
    def no_store(resp):
        # 面板前端迭代频繁：禁止浏览器缓存 HTML/JS/CSS，
        # 否则旧 app.js 缓存会导致"列表空白/开关失灵"之类的陈旧 bug。
        # SSE 路由自己已带 no-cache，重复设置无副作用。
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # ---------- 页面 ----------

    @app.route("/")
    def index():
        return send_from_directory(app.template_folder, "index.html")

    # ---------- API ----------

    @app.route("/api/overview")
    def api_overview():
        return jsonify(state.overview())

    @app.route("/api/runtimes")
    def api_runtimes():
        return jsonify(state.runtimes())

    @app.route("/api/usage")
    def api_usage():
        """token / cost 用量聚合（按模型、按天）。"""
        return jsonify(state.usage())

    @app.route("/api/risk")
    def api_risk():
        """调用热力图（近 24h × model）+ 账号风控表。"""
        return jsonify(state.risk_board())

    @app.route("/api/subagents")
    def api_subagents():
        include_archived = request.args.get("include_archived", "false").lower() == "true"
        return jsonify(state.subagents(include_archived=include_archived))

    @app.route("/api/subagents/<task_id>/log")
    def api_subagent_log(task_id: str):
        tail_kb = int(request.args.get("tail_kb", 64))
        return jsonify(state.subagent_log(task_id, tail_kb))

    @app.route("/api/subagents/<task_id>/transcript")
    def api_subagent_transcript(task_id: str):
        """结构化 transcript（events 流）—— 验收前看 agent 干了啥。"""
        return jsonify(state.subagent_transcript(task_id))

    @app.route("/api/subagents/<task_id>/stream")
    def api_subagent_stream(task_id: str):
        """实时日志流（SSE）—— 子 agent 跑的过程中也能看 stdout/stderr。"""
        return Response(
            state.subagent_stream(task_id),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.route("/api/subagents/<task_id>/history")
    def api_subagent_history(task_id: str):
        limit = int(request.args.get("limit", 5))
        return jsonify(state.subagent_history(task_id, limit))

    @app.route("/api/subagents/<task_id>/message", methods=["POST"])
    def api_subagent_message(task_id: str):
        """保存用户手动干预消息。"""
        data = request.get_json(silent=True) or {}
        message = data.get("message", "")
        from_user = data.get("from_user", "用户")
        return jsonify(state.save_user_message(task_id, message, from_user))

    @app.route("/api/subagents/<task_id>/messages", methods=["GET"])
    def api_subagent_messages(task_id: str):
        """读用户手动干预消息列表。"""
        return jsonify(state.get_user_messages(task_id))

    @app.route("/api/subagents/<task_id>/continue", methods=["POST"])
    def api_subagent_continue(task_id: str):
        """用用户消息续跑任务（spawn 新的子 agent）。"""
        data = request.get_json(silent=True) or {}
        message = data.get("message", "")
        from_user = data.get("from_user", "用户")
        return jsonify(state.continue_subagent(task_id, message, from_user))

    @app.route("/api/subagents/<task_id>/cancel", methods=["POST"])
    def api_subagent_cancel(task_id: str):
        """停止运行中的子 agent（按 registry 里的 pid 杀进程）。"""
        return jsonify(state.cancel_subagent(task_id))

    @app.route("/api/cluster")
    def api_cluster():
        return jsonify(state.cluster())

    @app.route("/api/cluster/submit", methods=["POST"])
    def api_cluster_submit():
        """从 web 派活到 cluster（默认 DeepSeek 集群，from_model=用户）。"""
        data = request.get_json(silent=True) or {}
        payload = data.get("payload", "")
        topic = data.get("topic", "cluster.work")
        from_model = data.get("from_model", "用户")
        runtime = data.get("runtime", "")
        model = data.get("model", "")
        acceptance_json = data.get("acceptance_json", "")
        acceptance_criteria = data.get("acceptance_criteria") or []
        acceptance_verifier = data.get("acceptance_verifier", "")
        acceptance_max_iterations = data.get("acceptance_max_iterations", 2)
        acceptance_auto_retry = data.get("acceptance_auto_retry", True)
        timeout_sec = int(data.get("timeout_sec") or 0)
        webhook = data.get("webhook", "")
        return jsonify(state.cluster_submit(
            payload=payload, topic=topic, from_model=from_model,
            runtime=runtime, model=model,
            acceptance_json=acceptance_json,
            acceptance_criteria=acceptance_criteria,
            acceptance_verifier=acceptance_verifier,
            acceptance_max_iterations=acceptance_max_iterations,
            acceptance_auto_retry=acceptance_auto_retry,
            timeout_sec=timeout_sec,
            webhook=webhook,
        ))

    @app.route("/api/models/all")
    def api_models_all():
        """所有 runtime + model 列表（按 runtime 分组），用于模型卡片墙。"""
        return jsonify(state.models_all())

    @app.route("/api/dashboard/pinned-models", methods=["GET"])
    def api_get_pinned():
        """获取当前白名单（.env + dashboard 存的合并）。"""
        return jsonify(state.get_pinned_models())

    @app.route("/api/dashboard/pinned-models", methods=["POST"])
    def api_set_pinned():
        """设置白名单（写 data/dashboard_state.json）。"""
        data = request.get_json(silent=True) or {}
        models = data.get("pinned_models", [])
        return jsonify(state.set_pinned_models(models))

    @app.route("/api/cluster/task/<task_id>")
    def api_cluster_task(task_id: str):
        """轮询 cluster 任务结果。"""
        return jsonify(state.cluster_task(task_id))

    @app.route("/api/tasks")
    def api_tasks():
        topic = request.args.get("topic", "")
        limit = int(request.args.get("limit", 100))
        include_archived = request.args.get("include_archived", "false").lower() == "true"
        return jsonify(state.tasks(topic=topic, limit=limit, include_archived=include_archived))

    @app.route("/api/tasks/<task_id>")
    def api_task(task_id: str):
        return jsonify(state.task(task_id))

    @app.route("/api/tasks/<task_id>/details")
    def api_task_details(task_id: str):
        """任务完整详情（含 log + 历史）。点开任务条目用。"""
        limit = int(request.args.get("history_limit", 5))
        return jsonify(state.task_details(task_id, limit))

    @app.route("/api/tasks/<task_id>/verify", methods=["POST"])
    def api_task_verify(task_id: str):
        """在 dashboard 写 verify 记录（给 dashboard 用户当 verifier 用）。

        真实使用中 verifier 应该是另一个 model（kimi/claude/gpt），
        但先让 dashboard 用户能手动验或代验。
        """
        data = request.get_json(silent=True) or {}
        verifier = data.get("verifier", "用户")
        passed = bool(data.get("passed", True))
        score = float(data.get("score", 1.0 if passed else 0.0))
        issues = data.get("issues", "")
        return jsonify(state.verify_task(task_id, verifier, passed, score, issues))

    return app


# ---------- CLI ----------

def _print(obj) -> None:
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(obj)


def cmd_status(args) -> None:
    """一屏概览。"""
    state = DashboardState()
    o = state.overview()

    print("=" * 70)
    print("MCP Hub Dashboard — Status")
    print("=" * 70)
    print()
    print(f"队列: {o['config']['queue_path']}")
    print(f"Cluster: {'ON' if o['config']['cluster_enabled'] else 'OFF'}"
          f"  size={o['config']['cluster_size']}  model={o['config']['cluster_model']}"
          f"  topic={o['config']['cluster_topic']}")
    print()
    print(f"Runtimes ({o['runtimes']['count']}):")
    for r in o["runtimes"]["items"]:
        mark = "✓" if r["available"] else "✗"
        models = r.get("models", [])
        models_str = f"  models={models[:3]}{'...' if len(models) > 3 else ''}" if models else ""
        print(f"  [{mark}] {r['name']:<10}  binary={r['binary']}{models_str}")
    print()
    print(f"Tools ({o['tools']['count']}):")
    for t in o["tools"]["items"]:
        mark = "✓" if t["available"] else "✗"
        print(f"  [{mark}] {t['name']:<10}  ops={t.get('operations', [])[:5]}")
    print()
    print("Queue:")
    for k, v in (o["queue"].get("stats") or {}).items():
        print(f"  {k}: {v}")
    print()
    print(f"子 agent 日志文件: {len(o.get('subagent_logs', []))} 个")


def cmd_subagent(args) -> None:
    """看单个 subagent 详情。"""
    state = DashboardState()
    log = state.subagent_log(args.task_id, tail_kb=args.tail_kb)
    if not log["ok"]:
        print(f"未找到: {log.get('error')}")
        sys.exit(1)
    print(f"=== {log['task_id']} ({log['size']} bytes, tail {log['tail_kb']}KB) ===")
    print(log["content"])


def cmd_watch(args) -> None:
    """实时刷新（默认 2s）。"""
    state = DashboardState()
    interval = args.interval
    print(f"watching mcp-hub，每 {interval}s 刷新，Ctrl-C 停止")
    try:
        while True:
            # 清屏
            sys.stdout.write("\033[2J\033[H")
            cmd_status(args)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped")


def cmd_tasks(args) -> None:
    """列任务。"""
    state = DashboardState()
    r = state.tasks(topic=args.topic or "", limit=args.limit)
    if not r["ok"]:
        print("error")
        return
    print(f"tasks: {r['count']}")
    for t in r["tasks"][:args.limit]:
        print(f"  [{t['status']:<10}] {t['task_id']} topic={t['topic']:<20} by={t.get('claimed_by') or '-':<20} {t.get('payload', '')[:60]}")


def cmd_cluster(args) -> None:
    """看 cluster 状态。"""
    state = DashboardState()
    c = state.cluster()
    _print(c)


def main() -> None:
    """入口。"""
    parser = argparse.ArgumentParser(
        prog="mcp-hub-dashboard",
        description="mcp-hub 的可观测性面板（web + CLI）",
    )
    sub = parser.add_subparsers(dest="cmd", required=False)

    # web（默认）
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--debug", action="store_true")

    # CLI 子命令
    p_status = sub.add_parser("status", help="一屏概览")
    p_sub = sub.add_parser("subagent", help="看单个子 agent 日志")
    p_sub.add_argument("task_id")
    p_sub.add_argument("--tail-kb", type=int, default=64)
    p_watch = sub.add_parser("watch", help="实时刷新 status")
    p_watch.add_argument("--interval", type=float, default=2.0)
    p_tasks = sub.add_parser("tasks", help="列任务")
    p_tasks.add_argument("--topic", default="")
    p_tasks.add_argument("--limit", type=int, default=20)
    p_cluster = sub.add_parser("cluster", help="看 cluster 状态")

    args = parser.parse_args()

    if args.cmd is None:
        # 默认起 web
        if Flask is None:
            print("需要 flask：pip install flask")
            sys.exit(1)
        app = create_app()
        print(f"mcp-hub dashboard: http://{args.host}:{args.port}")
        app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
        return

    handler = {
        "status": cmd_status,
        "subagent": cmd_subagent,
        "watch": cmd_watch,
        "tasks": cmd_tasks,
        "cluster": cmd_cluster,
    }.get(args.cmd)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
