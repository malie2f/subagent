"""任务组：共同目标、黑板、卡死标注、纠正/解卡数据。"""
from __future__ import annotations

import json
from pathlib import Path

from mcp_hub.crew import (
    STUCK_SEC,
    add_member,
    annotate_members,
    blackboard_digest,
    create_crew,
    duty_from_prompt,
    flag_member,
    get_crew,
    inject_mcp_config,
    poll,
    post,
    preview_from_events,
    wrap_correct_prompt,
    wrap_worker_prompt,
)


def test_create_and_blackboard(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = create_crew("把登录改成 SSO", supervisor="用户")
    assert r["ok"]
    cid = r["crew"]["crew_id"]
    assert get_crew(cid)["goal"] == "把登录改成 SSO"
    post(cid, "implementer", "我先改 auth.py")
    digest = blackboard_digest(get_crew(cid))
    assert "auth.py" in digest
    prompt = wrap_worker_prompt(get_crew(cid)["goal"], "implementer", "改文件", digest, crew_id=cid)
    assert "SSO" in prompt
    assert "crew_poll" in prompt
    assert cid in prompt


def test_flag_off_track(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cid = create_crew("目标")["crew"]["crew_id"]
    add_member(cid, role="w", runtime="opencode", model="m", task_id="t1", prompt="p")
    mid = next(iter(get_crew(cid)["members"]))
    flag_member(cid, mid, "off_track", "改错文件了")
    m = get_crew(cid)["members"][mid]
    assert "off_track" in m["flags"]
    prompt = wrap_correct_prompt("目标", "w", "回到 auth.py", "黑板")
    assert "监督者纠正" in prompt
    assert "auth.py" in prompt


def test_stuck_when_log_stale(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cid = create_crew("目标")["crew"]["crew_id"]
    add_member(cid, role="w", runtime="opencode", model="m", task_id="dead1", prompt="p")
    crew = get_crew(cid)
    registry = {"subagents": {"dead1": {
        "status": "running",
        "started_at": 1.0,
        "pid": None,
        "log_file": "",
        "summary": "",
    }}}
    views = annotate_members(crew, registry)
    assert views[0]["stuck"] is True
    assert views[0]["idle_sec"] > STUCK_SEC


def test_poll_incremental_and_directed(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cid = create_crew("目标")["crew"]["crew_id"]
    a = post(cid, "implementer", "我改了 auth", to="reviewer")
    b = post(cid, "reviewer", "收到，开始看")
    assert a["seq"] == 2
    assert b["seq"] == 3
    inbox = poll(cid, since_seq=1, for_role="reviewer")
    assert inbox["ok"]
    blob = "\n".join(m["text"] for m in inbox["messages"])
    assert "我改了 auth" in blob
    assert "开始看" in blob
    only_impl = poll(cid, since_seq=1, for_role="implementer")
    impl_blob = "\n".join(m["text"] for m in only_impl["messages"])
    assert "我改了 auth" not in impl_blob
    assert "开始看" in impl_blob
    later = poll(cid, since_seq=3, for_role="reviewer")
    assert later["count"] == 0
    assert later["last_seq"] >= 3


def test_inject_mcp_json_merges(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    wd = tmp_path / "proj"
    wd.mkdir()
    (wd / ".mcp.json").write_text('{"mcpServers":{"other":{"url":"x"}}}', encoding="utf-8")
    path = inject_mcp_config(str(wd))
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert "other" in data["mcpServers"]
    assert data["mcpServers"]["subagent"]["url"].endswith("/sse")
    assert "mcp-hub" not in data["mcpServers"]


def test_inject_migrates_legacy_mcp_hub_id(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    wd = tmp_path / "proj"
    wd.mkdir()
    (wd / ".mcp.json").write_text(
        '{"mcpServers":{"mcp-hub":{"type":"sse","url":"http://127.0.0.1:8765/sse"}}}',
        encoding="utf-8",
    )
    inject_mcp_config(str(wd))
    data = json.loads((wd / ".mcp.json").read_text(encoding="utf-8"))
    assert "subagent" in data["mcpServers"]
    assert "mcp-hub" not in data["mcpServers"]


def test_preview_from_events_and_duty(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cid = create_crew("共同目标")["crew"]["crew_id"]
    prompt = wrap_worker_prompt("共同目标", "archivist-a", "年段 1936–1947", "（空）", crew_id=cid)
    assert "1936–1947" in duty_from_prompt(prompt)
    add_member(
        cid, role="archivist-a", runtime="opencode", model="m",
        task_id="t1", prompt=prompt, task="年段 1936–1947",
    )
    crew = get_crew(cid)
    mid = next(iter(crew["members"]))
    assert crew["members"][mid]["task"] == "年段 1936–1947"
    views = annotate_members(crew, {"subagents": {}})
    assert views[0]["task"].startswith("年段")
    prev = preview_from_events(
        [
            {"type": "turn", "role": "reasoning", "content": "先核对年份交叉"},
            {"type": "tool_call", "name": "crew_post"},
            {"type": "turn", "role": "assistant", "content": "我开始做 1936–1947"},
        ],
        task="年段 1936–1947",
    )
    assert prev["task"] == "年段 1936–1947"
    assert "年份交叉" in prev["thinking"]
    assert "开始做" in prev["latest"]
    assert prev["tools"] == ["crew_post"]
