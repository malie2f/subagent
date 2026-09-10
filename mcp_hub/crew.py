"""任务组（crew）：共同目标 + 经 mcp-hub 的黑板通信 + 监督者解卡/纠偏。

成员不能挂着群聊，但可以在跑的过程中调 mcp-hub：
  crew_post 留言（可定向 to=角色）
  crew_poll 拉比上次更新的消息
开工时往 workdir 写入 .mcp.json，让已接 MCP 的 CLI（尤其 opencode）连上本机 8765。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from mcp_hub.registry import _kill_pid, _pid_alive

STUCK_SEC = 300
MAX_BLACKBOARD = 200
CREW_FILE = Path("./data/crews.json")


def _path() -> Path:
    return CREW_FILE


def load() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {"crews": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"crews": {}}
    if not isinstance(data, dict):
        return {"crews": {}}
    data.setdefault("crews", {})
    return data


def save(data: dict[str, Any]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def create_crew(goal: str, supervisor: str = "用户") -> dict[str, Any]:
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "error": "goal 不能为空"}
    data = load()
    cid = _new_id()
    crew = {
        "crew_id": cid,
        "goal": goal,
        "status": "running",
        "supervisor": supervisor or "用户",
        "created_at": time.time(),
        "members": {},
        "next_seq": 1,
        "blackboard": [
            {
                "seq": 1,
                "at": time.time(),
                "from": "system",
                "to": "",
                "kind": "goal",
                "text": goal,
            }
        ],
    }
    data["crews"][cid] = crew
    save(data)
    return {"ok": True, "crew": public_crew(crew)}


def get_crew(crew_id: str) -> dict[str, Any] | None:
    return load().get("crews", {}).get(crew_id)


def list_crews(include_done: bool = False) -> list[dict[str, Any]]:
    out = []
    for c in load().get("crews", {}).values():
        if not include_done and c.get("status") in ("done", "stopped"):
            continue
        out.append(public_crew(c))
    out.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return out


def duty_from_prompt(prompt: str) -> str:
    """从开工 prompt 里抽出「你的分工」；旧成员没有单独存 task 时用。"""
    text = prompt or ""
    marker = "【你的分工】"
    if marker not in text:
        return ""
    rest = text.split(marker, 1)[1]
    rest = rest.split("【", 1)[0].strip()
    return rest[:2000]


def preview_from_events(
    events: list[dict[str, Any]] | None,
    *,
    task: str = "",
    prompt: str = "",
) -> dict[str, Any]:
    """给预览窗：分工、最近思考、最近输出、最近工具。不依赖 Flask。"""
    duty = (task or "").strip() or duty_from_prompt(prompt)
    if not duty:
        duty = (prompt or "").strip()[:800]
    thinking = ""
    latest = ""
    tools: list[str] = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        if kind == "turn":
            role = ev.get("role") or ""
            content = ev.get("content") or ""
            if role == "reasoning" and content:
                thinking = content
            elif role == "assistant" and content:
                latest = content
        elif kind == "final" and ev.get("content"):
            latest = ev.get("content") or latest
        elif kind == "tool_call":
            name = ev.get("name") or "?"
            tools.append(str(name))
    return {
        "task": duty[:2000],
        "thinking": thinking[-4000:],
        "latest": (latest or "")[-2500:],
        "tools": tools[-8:],
        "event_count": len(events or []),
    }


def add_member(
    crew_id: str,
    *,
    role: str,
    runtime: str,
    model: str,
    task_id: str,
    prompt: str,
    task: str = "",
) -> dict[str, Any]:
    data = load()
    crew = data.get("crews", {}).get(crew_id)
    if not crew:
        return {"ok": False, "error": f"crew 不存在: {crew_id}"}
    mid = _new_id()
    crew["members"][mid] = {
        "member_id": mid,
        "role": role or "worker",
        "runtime": runtime,
        "model": model,
        "task_id": task_id,
        "task": (task or "").strip(),
        "prompt": prompt,
        "status": "running",
        "flags": [],
        "started_at": time.time(),
        "last_action": "",
    }
    save(data)
    return {"ok": True, "member_id": mid, "task_id": task_id, "crew_id": crew_id}


def post(crew_id: str, from_role: str, text: str, kind: str = "note", to: str = "") -> dict[str, Any]:
    """留言。to 空=全员可见；填角色名则主要给该角色（全员 poll 仍能看到，方便监督）。"""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "消息不能为空"}
    data = load()
    crew = data.get("crews", {}).get(crew_id)
    if not crew:
        return {"ok": False, "error": f"crew 不存在: {crew_id}"}
    seq = int(crew.get("next_seq") or 0) + 1
    crew["next_seq"] = seq
    board = crew.setdefault("blackboard", [])
    msg = {
        "seq": seq,
        "at": time.time(),
        "from": from_role or "unknown",
        "to": (to or "").strip(),
        "kind": kind or "note",
        "text": text[:4000],
    }
    board.append(msg)
    if len(board) > MAX_BLACKBOARD:
        crew["blackboard"] = board[-MAX_BLACKBOARD:]
    save(data)
    return {"ok": True, "crew_id": crew_id, "seq": seq, "n": len(crew["blackboard"])}


def poll(crew_id: str, since_seq: int = 0, for_role: str = "") -> dict[str, Any]:
    """拉 seq > since_seq 的消息。for_role 非空时：to 为空或 to==该角色。"""
    crew = get_crew(crew_id)
    if not crew:
        return {"ok": False, "error": f"crew 不存在: {crew_id}"}
    since_seq = int(since_seq or 0)
    role = (for_role or "").strip()
    msgs = []
    for item in crew.get("blackboard") or []:
        seq = int(item.get("seq") or 0)
        if seq <= since_seq:
            continue
        dest = (item.get("to") or "").strip()
        if role and dest and dest != role and dest != "all":
            continue
        msgs.append(item)
    last = since_seq
    if msgs:
        last = max(int(m.get("seq") or 0) for m in msgs)
    elif crew.get("next_seq"):
        last = max(since_seq, int(crew["next_seq"]))
    return {
        "ok": True,
        "crew_id": crew_id,
        "since_seq": since_seq,
        "last_seq": last,
        "count": len(msgs),
        "messages": msgs,
        "goal": crew.get("goal"),
    }


def flag_member(crew_id: str, member_id: str, flag: str, reason: str = "") -> dict[str, Any]:
    if flag not in ("off_track", "stuck", "ok"):
        return {"ok": False, "error": "flag 只能是 off_track / stuck / ok"}
    data = load()
    crew = data.get("crews", {}).get(crew_id)
    if not crew:
        return {"ok": False, "error": f"crew 不存在: {crew_id}"}
    m = crew.get("members", {}).get(member_id)
    if not m:
        return {"ok": False, "error": f"成员不存在: {member_id}"}
    flags = list(m.get("flags") or [])
    if flag == "ok":
        flags = [f for f in flags if f not in ("off_track", "stuck")]
    elif flag not in flags:
        flags.append(flag)
    m["flags"] = flags
    m["last_action"] = f"flag:{flag}"
    save(data)
    post(
        crew_id,
        "supervisor",
        f"{m.get('role')} ({member_id[:6]}) → {flag}" + (f"：{reason}" if reason else ""),
        kind="flag",
        to=m.get("role") or "",
    )
    return {"ok": True, "member_id": member_id, "flags": flags}


def stop_crew(crew_id: str) -> dict[str, Any]:
    data = load()
    crew = data.get("crews", {}).get(crew_id)
    if not crew:
        return {"ok": False, "error": f"crew 不存在: {crew_id}"}
    crew["status"] = "stopped"
    crew["finished_at"] = time.time()
    save(data)
    return {"ok": True, "crew_id": crew_id, "status": "stopped"}


def update_member_task(crew_id: str, member_id: str, task_id: str, status: str = "running") -> None:
    data = load()
    crew = data.get("crews", {}).get(crew_id)
    if not crew:
        return
    m = crew.get("members", {}).get(member_id)
    if not m:
        return
    m["task_id"] = task_id
    m["status"] = status
    m["last_action_at"] = time.time()
    save(data)


def blackboard_digest(crew: dict[str, Any], n: int = 12) -> str:
    lines = []
    for item in (crew.get("blackboard") or [])[-n:]:
        who = item.get("from") or "?"
        kind = item.get("kind") or "note"
        text = (item.get("text") or "").replace("\n", " ")[:240]
        lines.append(f"- [{kind}] {who}: {text}")
    return "\n".join(lines) if lines else "（黑板为空）"


HUB_SSE = "http://127.0.0.1:8765/sse"
MCP_SERVER_ID = "subagent"
_LEGACY_MCP_SERVER_ID = "mcp-hub"


def wrap_worker_prompt(goal: str, role: str, task: str, digest: str, crew_id: str = "") -> str:
    cid = crew_id or "(crew_id)"
    return (
        f"你是任务组的一名成员，角色：{role}。crew_id={cid}\n"
        f"【共同目标】\n{goal}\n\n"
        f"【你的分工】\n{task}\n\n"
        f"【黑板快照】\n{digest}\n\n"
        "【同事通信——必须通过子智能体 MCP（接入名 subagent），不要假装能实时对讲】\n"
        f"工作目录已写入 subagent（{HUB_SSE}）。开工后、每完成一块、遇到阻塞，都要调这些工具：\n"
        f"1) crew_poll(crew_id=\"{cid}\", since_seq=你记下的序号, for_role=\"{role}\") "
        "拉新消息；把返回的 last_seq 记下来，下次接着用。\n"
        f"2) crew_post(crew_id=\"{cid}\", from_role=\"{role}\", text=\"...\", to=\"角色名或空\") "
        "发消息。to 留空=全员；填同事角色名=主要给他。\n"
        "先 poll 再动手；有结论或请求帮助就 post。不要干完一声不吭。\n"
        "若发现偏离共同目标，post 给 supervisor 并在交付里写明。"
    )


def wrap_correct_prompt(goal: str, role: str, instruction: str, digest: str, crew_id: str = "") -> str:
    cid = crew_id or ""
    extra = (
        f"\n纠正后继续用 crew_poll / crew_post（crew_id={cid}）和同事同步。\n"
        if cid else ""
    )
    return (
        f"【监督者纠正】角色 {role}。停止当前跑偏方向，按下面指令继续。"
        f"共同目标不变：{goal}\n\n"
        f"纠正指令：\n{instruction}\n\n"
        f"黑板：\n{digest}\n"
        f"{extra}"
    )


def wrap_unstick_prompt(goal: str, role: str, digest: str, crew_id: str = "") -> str:
    cid = crew_id or ""
    extra = f"\n用 crew_poll(crew_id=\"{cid}\") 看同事是否已经写了进展再继续。\n" if cid else ""
    return (
        f"【监督者解卡】你之前可能卡住或没产出。角色 {role}。"
        f"共同目标：{goal}\n"
        "请从中断处继续，不要从头来；先用一两句话说明卡在哪，再动手。\n"
        f"黑板：\n{digest}\n"
        f"{extra}"
    )


def inject_mcp_config(workdir: str) -> str:
    """在 workdir 写入/合并 .mcp.json，让子 CLI 能连本机子智能体 SSE。

    接入名 subagent。若仍有旧条目 mcp-hub 且指向同一 SSE，则删掉以免双连。
    不覆盖用户已有的其它 mcpServers 条目。返回写入路径。
    """
    root = Path(workdir or ".").expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".mcp.json"
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    entry = {"type": "sse", "url": HUB_SSE}
    servers[MCP_SERVER_ID] = entry
    legacy = servers.get(_LEGACY_MCP_SERVER_ID)
    if isinstance(legacy, dict) and (legacy.get("url") or "") == HUB_SSE:
        del servers[_LEGACY_MCP_SERVER_ID]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def _log_mtime(log_file: str) -> float:
    if not log_file:
        return 0.0
    try:
        return Path(log_file).stat().st_mtime
    except OSError:
        return 0.0


def annotate_members(crew: dict[str, Any], registry: dict[str, Any]) -> list[dict[str, Any]]:
    """给每个成员打 stuck / 走歪 / 是否还活着。"""
    now = time.time()
    subs = (registry or {}).get("subagents") or {}
    out = []
    for mid, m in (crew.get("members") or {}).items():
        tid = m.get("task_id") or ""
        entry = subs.get(tid) or {}
        status = entry.get("status") or m.get("status") or "unknown"
        log_file = entry.get("log_file") or ""
        mtime = _log_mtime(log_file)
        last = mtime or entry.get("started_at") or m.get("started_at") or 0
        pid = entry.get("pid")
        alive = bool(isinstance(pid, int) and pid > 0 and _pid_alive(pid))
        running = status in ("running", "claimed", "pending")
        auto_stuck = False
        reasons = []
        if running and last and (now - last) > STUCK_SEC:
            auto_stuck = True
            reasons.append(f"日志 {int(now - last)}s 未更新")
        if running and pid and not alive:
            auto_stuck = True
            reasons.append("进程已死但 registry 仍 running")
        flags = list(m.get("flags") or [])
        if auto_stuck and "stuck" not in flags:
            flags.append("stuck")
        view = {
            "member_id": mid,
            "role": m.get("role"),
            "runtime": m.get("runtime"),
            "model": m.get("model"),
            "task_id": tid,
            "task": (m.get("task") or duty_from_prompt(m.get("prompt") or ""))[:800],
            "status": status,
            "pid": pid,
            "alive": alive,
            "log_mtime": mtime or None,
            "idle_sec": round(now - last, 1) if last else None,
            "flags": flags,
            "stuck": auto_stuck or "stuck" in flags,
            "off_track": "off_track" in flags,
            "stuck_reason": "；".join(reasons),
            "summary": (entry.get("summary") or "")[:300],
            "log_file": log_file,
        }
        out.append(view)
    out.sort(key=lambda x: x.get("role") or "")
    return out


def public_crew(crew: dict[str, Any], registry: dict[str, Any] | None = None) -> dict[str, Any]:
    members = annotate_members(crew, registry or {}) if registry is not None else [
        {
            "member_id": mid,
            "role": m.get("role"),
            "runtime": m.get("runtime"),
            "model": m.get("model"),
            "task_id": m.get("task_id"),
            "task": (m.get("task") or duty_from_prompt(m.get("prompt") or ""))[:800],
            "flags": m.get("flags") or [],
        }
        for mid, m in (crew.get("members") or {}).items()
    ]
    n_stuck = sum(1 for m in members if m.get("stuck"))
    n_off = sum(1 for m in members if m.get("off_track"))
    n_run = sum(1 for m in members if m.get("status") == "running")
    return {
        "crew_id": crew.get("crew_id"),
        "goal": crew.get("goal"),
        "status": crew.get("status"),
        "supervisor": crew.get("supervisor"),
        "created_at": crew.get("created_at"),
        "n_members": len(crew.get("members") or {}),
        "n_running": n_run,
        "n_stuck": n_stuck,
        "n_off_track": n_off,
        "members": members,
        "blackboard": crew.get("blackboard") or [],
    }


def kill_member_pid(registry_entry: dict[str, Any]) -> bool:
    pid = registry_entry.get("pid")
    if isinstance(pid, int) and pid > 0 and _pid_alive(pid):
        return _kill_pid(pid)
    return False
