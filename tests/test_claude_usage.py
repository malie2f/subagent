"""claude stream-json 解析的 usage 事件映射测试。

fixture 来自真实历史日志（.mcp-hub/subagents/*.log，Claude Code CLI 2.x
--output-format stream-json），已脱敏缩短：同一 message.id 的 assistant 事件
流式重复推、usage 为累积快照；result 事件带整轮合计 usage + total_cost_usd。
"""

from __future__ import annotations

import json

from mcp_hub.runtimes.claude import _parse_claude_stream_json


def _line(ev: dict) -> str:
    return json.dumps(ev, ensure_ascii=False)


def _usage_block(**kw) -> dict:
    base = {
        "input_tokens": 1050,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "service_tier": "standard",
    }
    base.update(kw)
    return base


def _assistant_event(msg_id: str, text: str, usage: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [{"type": "text", "text": text}],
            "stop_reason": None,
            "usage": usage,
        },
        "parent_tool_use_id": None,
        "session_id": "ca98fec3-0000-4000-8000-000000000000",
        "uuid": "dd978ecf-0000-4000-8000-000000000000",
    }


def test_result_event_emits_usage_with_cost():
    """真实日志形态：assistant 快照 + 末尾 result 整轮合计 → 一个 usage 事件。"""
    stdout = "\n".join([
        _line({"type": "system", "subtype": "init", "session_id": "s", "model": "claude-opus-4-6"}),
        _line(_assistant_event("msg_1", "", _usage_block())),
        _line(_assistant_event("msg_1", "CLAUDE-MCP-OK", _usage_block(output_tokens=35))),
        _line({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 1178,
            "num_turns": 1,
            "result": "CLAUDE-MCP-OK",
            "stop_reason": "end_turn",
            "total_cost_usd": 0.006125,
            "usage": _usage_block(output_tokens=35),
        }),
    ])
    events = _parse_claude_stream_json(stdout)
    usage = [e for e in events if e.get("type") == "usage"]
    assert len(usage) == 1
    tok = usage[0]["tokens"]
    assert tok["input"] == 1050
    assert tok["output"] == 35
    assert tok["reasoning"] == 0  # 源数据没有 reasoning 字段
    # total 无源字段：input + output + cache.read + cache.write 派生
    assert tok["total"] == 1085
    assert tok["cache"]["read"] == 0
    assert tok["cache"]["write"] == 0
    assert usage[0]["cost"] == 0.006125


def test_result_usage_with_cache_tokens():
    stdout = _line({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "total_cost_usd": 0.12,
        "usage": _usage_block(
            input_tokens=200,
            output_tokens=50,
            cache_read_input_tokens=8000,
            cache_creation_input_tokens=1500,
        ),
    })
    events = _parse_claude_stream_json(stdout)
    usage = [e for e in events if e.get("type") == "usage"]
    assert len(usage) == 1
    tok = usage[0]["tokens"]
    assert tok["cache"]["read"] == 8000
    assert tok["cache"]["write"] == 1500
    assert tok["total"] == 200 + 50 + 8000 + 1500


def test_no_result_falls_back_to_assistant_snapshots():
    """进程被杀没有 result：按 message.id 去重取最后快照，跨 message 求和，cost=0。"""
    stdout = "\n".join([
        _line(_assistant_event("msg_1", "a", _usage_block(output_tokens=10))),
        _line(_assistant_event("msg_1", "ab", _usage_block(output_tokens=20))),  # 覆盖上一条
        _line(_assistant_event("msg_2", "cd", _usage_block(input_tokens=2100, output_tokens=5))),
    ])
    events = _parse_claude_stream_json(stdout)
    usage = [e for e in events if e.get("type") == "usage"]
    assert len(usage) == 1
    tok = usage[0]["tokens"]
    assert tok["input"] == 1050 + 2100
    assert tok["output"] == 20 + 5  # msg_1 只算最后一次快照
    assert tok["total"] == 3150 + 25
    assert usage[0]["cost"] == 0.0


def test_no_usage_anywhere_no_event():
    stdout = "\n".join([
        _line({"type": "system", "subtype": "init", "session_id": "s"}),
        _line({"type": "assistant", "message": {"id": "m1", "role": "assistant", "content": []}}),
        _line({"type": "result", "subtype": "error_during_execution", "is_error": True}),
    ])
    events = _parse_claude_stream_json(stdout)
    assert not any(e.get("type") == "usage" for e in events)


def test_garbage_lines_tolerated():
    stdout = "\n".join([
        "not json at all",
        "{broken json",
        _line({"type": "result", "subtype": "success", "is_error": False,
               "total_cost_usd": 0.001, "usage": _usage_block(output_tokens=3)}),
    ])
    events = _parse_claude_stream_json(stdout)
    usage = [e for e in events if e.get("type") == "usage"]
    assert len(usage) == 1
    assert usage[0]["tokens"]["output"] == 3
