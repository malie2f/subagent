"""codex JSONL 解析的 usage 事件映射测试。"""

from __future__ import annotations

import json

from mcp_hub.runtimes.codex import _parse_codex_jsonl


def _line(ev: dict) -> str:
    return json.dumps(ev, ensure_ascii=False)


def test_turn_completed_emits_usage():
    stdout = "\n".join([
        _line({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
        _line({"type": "turn.completed", "usage": {
            "input_tokens": 1000, "cached_input_tokens": 200,
            "output_tokens": 50, "reasoning_output_tokens": 30,
        }}),
    ])
    events = _parse_codex_jsonl(stdout)
    usage = [e for e in events if e.get("type") == "usage"]
    assert len(usage) == 1
    tok = usage[0]["tokens"]
    assert tok["input"] == 1000
    assert tok["output"] == 50
    assert tok["reasoning"] == 30
    assert tok["total"] == 1050  # 没给 total_tokens 时 input+output 兜底
    assert tok["cache"]["read"] == 200
    # final 事件仍然正常生成
    assert any(e.get("type") == "final" for e in events)


def test_turn_completed_without_usage_no_event():
    stdout = _line({"type": "turn.completed"})
    events = _parse_codex_jsonl(stdout)
    assert not any(e.get("type") == "usage" for e in events)
