"""kimi wire.jsonl usage 解析测试（StatusUpdate.token_usage → 标准 usage 事件）。"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_hub.runtimes.kimi import _extract_session_id, _read_wire_usage


def _wire_line(msg_type: str, payload: dict) -> str:
    return json.dumps({"timestamp": 1.0, "message": {"type": msg_type, "payload": payload}})


def _make_session(root: Path, session_id: str, lines: list[str]) -> Path:
    d = root / "deadbeef" / session_id
    d.mkdir(parents=True)
    (d / "wire.jsonl").write_text("\n".join(lines), encoding="utf-8")
    return d


def test_extract_session_id():
    stdout = "\n".join([
        json.dumps({"role": "assistant", "content": "hi"}),
        json.dumps({"role": "meta", "type": "session.resume_hint",
                    "session_id": "abc-123", "command": "kimi", "content": "..."}),
    ])
    assert _extract_session_id(stdout) == "abc-123"
    assert _extract_session_id('{"role":"assistant","content":"x"}') is None
    assert _extract_session_id("not json at all") is None


def test_read_wire_usage_sums_and_dedupes(tmp_path):
    sid = "sess-1"
    _make_session(tmp_path, sid, [
        # 同一 message_id 两条（流式），应只算最后一条
        _wire_line("StatusUpdate", {"message_id": "m1", "token_usage": {
            "input_other": 100, "output": 5, "input_cache_read": 10, "input_cache_creation": 1}}),
        _wire_line("StatusUpdate", {"message_id": "m1", "token_usage": {
            "input_other": 100, "output": 20, "input_cache_read": 10, "input_cache_creation": 1}}),
        # 第二次 API 调用
        _wire_line("StatusUpdate", {"message_id": "m2", "token_usage": {
            "input_other": 200, "output": 30, "input_cache_read": 50, "input_cache_creation": 0}}),
        # 非 StatusUpdate 不带入
        _wire_line("AssistantText", {"text": "hello"}),
    ])
    ev = _read_wire_usage(sid, sessions_root=tmp_path)
    assert ev is not None and ev["type"] == "usage"
    tok = ev["tokens"]
    assert tok["input"] == 300
    assert tok["output"] == 50  # 20 + 30（m1 取最后一条）
    assert tok["cache"]["read"] == 60
    assert tok["cache"]["write"] == 1
    assert tok["total"] == 300 + 50 + 60 + 1
    assert ev["cost"] == 0.0


def test_read_wire_usage_missing(tmp_path):
    assert _read_wire_usage("nonexistent", sessions_root=tmp_path) is None


def test_read_wire_usage_no_token_usage(tmp_path):
    sid = "sess-2"
    _make_session(tmp_path, sid, [_wire_line("StatusUpdate", {"message_id": "m1"})])
    assert _read_wire_usage(sid, sessions_root=tmp_path) is None


def test_read_wire_usage_new_format(tmp_path):
    """新版 kimi-code 布局：agents/main/wire.jsonl + usage.record（camelCase）。"""
    sid = "session_9d5986ac-0149-438e-97a0-55066a882531"
    d = tmp_path / "wd_recovered_5356799f1520" / sid / "agents" / "main"
    d.mkdir(parents=True)
    (d / "wire.jsonl").write_text("\n".join([
        json.dumps({"type": "usage.record", "model": "kimi-code/kimi-for-coding",
                    "usage": {"inputOther": 13282, "output": 25,
                              "inputCacheRead": 10496, "inputCacheCreation": 0},
                    "usageScope": "turn", "time": 1785696584950}),
        json.dumps({"type": "usage.record", "model": "kimi-code/kimi-for-coding",
                    "usage": {"inputOther": 20000, "output": 100,
                              "inputCacheRead": 5000, "inputCacheCreation": 500},
                    "usageScope": "turn", "time": 1785696585000}),
        json.dumps({"type": "turn.started"}),  # 非 usage 事件跳过
    ]), encoding="utf-8")
    ev = _read_wire_usage(sid, sessions_root=tmp_path)
    assert ev is not None
    tok = ev["tokens"]
    assert tok["input"] == 33282
    assert tok["output"] == 125
    assert tok["cache"]["read"] == 15496
    assert tok["cache"]["write"] == 500
    assert tok["total"] == 33282 + 125 + 15496 + 500
