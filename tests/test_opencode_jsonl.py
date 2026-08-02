"""OpenCode adapter 的 --format json JSONL 解析单元测试。

构造样例事件行（参照 opencode 1.18.4 实测输出），覆盖：
  - text part 拼接成 summary / final
  - 流式重复推同一 text part 的去重
  - tool part → tool_call + file_change 映射（write→create, edit→modify）
  - step_finish → usage 事件（tokens/cost 聚合）
  - 非 JSON 行降级保留为原始文本，不崩
  - 未知 part.type 跳过
  - 版本解析 / HUB_OPENCODE_AUTO 配置
"""

from __future__ import annotations

import json

from mcp_hub.config import HubSettings
from mcp_hub.runtimes.opencode import (
    MIN_VERSION,
    _parse_opencode_jsonl,
    _parse_version,
)


def _ev(etype: str, part: dict, ts: int = 1750000000000) -> str:
    return json.dumps(
        {"type": etype, "timestamp": ts, "sessionID": "ses_test", "part": part},
        ensure_ascii=False,
    )


def _types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


# ---------- text 拼接 ----------

def test_text_parts_concat_to_summary() -> None:
    stdout = "\n".join([
        _ev("step_start", {"id": "prt_s1", "type": "step-start"}),
        _ev("text", {"id": "prt_t1", "type": "text", "text": "第一段回复"}),
        _ev("text", {"id": "prt_t2", "type": "text", "text": "第二段回复"}),
        _ev("step_finish", {
            "type": "step-finish", "reason": "stop",
            "tokens": {"total": 100, "input": 80, "output": 20},
            "cost": 0,
        }),
    ])
    events, summary, artifacts = _parse_opencode_jsonl(stdout)
    assert summary == "第一段回复\n\n第二段回复"
    finals = [e for e in events if e["type"] == "final"]
    assert len(finals) == 1
    assert finals[0]["content"] == summary
    assert finals[0]["stop_reason"] == "stop"
    turns = [e for e in events if e["type"] == "turn" and e["role"] == "assistant"]
    assert [t["content"] for t in turns] == ["第一段回复", "第二段回复"]
    assert artifacts == []


def test_streaming_duplicate_text_part_deduped() -> None:
    """同一个 part.id 流式推多次（增量全文），只保留最后一次。"""
    stdout = "\n".join([
        _ev("text", {"id": "prt_t1", "type": "text", "text": "hel"}),
        _ev("text", {"id": "prt_t1", "type": "text", "text": "hello"}),
        _ev("text", {"id": "prt_t1", "type": "text", "text": "hello world"}),
    ])
    events, summary, _ = _parse_opencode_jsonl(stdout)
    assert summary == "hello world"
    turns = [e for e in events if e["type"] == "turn"]
    assert len(turns) == 1


# ---------- tool → tool_call + file_change ----------

def test_tool_call_and_file_change_mapping() -> None:
    stdout = "\n".join([
        _ev("tool_use", {
            "id": "prt_w1", "type": "tool", "callID": "call_1", "tool": "write",
            "state": {"status": "completed",
                      "input": {"filePath": "hello.txt", "content": "hi"},
                      "output": "ok"},
        }),
        _ev("tool_use", {
            "id": "prt_e1", "type": "tool", "callID": "call_2", "tool": "edit",
            "state": {"status": "completed",
                      "input": {"filePath": "hello.txt", "oldString": "hi", "newString": "yo"},
                      "output": "ok"},
        }),
        _ev("tool_use", {
            "id": "prt_b1", "type": "tool", "callID": "call_3", "tool": "bash",
            "state": {"status": "completed",
                      "input": {"command": "ls"},
                      "output": "hello.txt"},
        }),
        _ev("text", {"id": "prt_t1", "type": "text", "text": "完成"}),
    ])
    events, summary, artifacts = _parse_opencode_jsonl(stdout)

    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert [t["name"] for t in tool_calls] == ["write", "edit", "bash"]
    assert tool_calls[0]["args"]["filePath"] == "hello.txt"
    assert tool_calls[0]["result_preview"] == "ok"

    # write → create，之后 edit → modify（modify 最强，覆盖 create）
    fcs = [e for e in events if e["type"] == "file_change"]
    assert fcs == [{"type": "file_change", "path": "hello.txt", "action": "modify"}]
    assert artifacts == ["hello.txt"]
    assert summary == "完成"


def test_write_only_maps_to_create() -> None:
    stdout = _ev("tool_use", {
        "id": "prt_w1", "type": "tool", "callID": "call_1", "tool": "write",
        "state": {"status": "completed", "input": {"filePath": "a.txt", "content": "x"}},
    })
    events, _, artifacts = _parse_opencode_jsonl(stdout)
    fcs = [e for e in events if e["type"] == "file_change"]
    assert fcs[0]["action"] == "create"
    assert artifacts == ["a.txt"]


def test_pending_tool_not_recorded() -> None:
    """工具还在 running/pending 时不记 tool_call，完结后才记一次。"""
    stdout = "\n".join([
        _ev("tool_use", {
            "id": "prt_w1", "type": "tool", "callID": "call_1", "tool": "write",
            "state": {"status": "running", "input": {"filePath": "a.txt"}},
        }),
        _ev("tool_use", {
            "id": "prt_w1", "type": "tool", "callID": "call_1", "tool": "write",
            "state": {"status": "completed", "input": {"filePath": "a.txt"}},
        }),
        _ev("tool_use", {
            "id": "prt_w1", "type": "tool", "callID": "call_1", "tool": "write",
            "state": {"status": "completed", "input": {"filePath": "a.txt"}},
        }),
    ])
    events, _, _ = _parse_opencode_jsonl(stdout)
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) == 1


# ---------- usage ----------

def test_step_finish_aggregates_usage() -> None:
    stdout = "\n".join([
        _ev("step_finish", {
            "type": "step-finish", "reason": "tool-calls",
            "tokens": {"total": 100, "input": 80, "output": 20, "reasoning": 0,
                       "cache": {"write": 0, "read": 10}},
            "cost": 0.001,
        }),
        _ev("step_finish", {
            "type": "step-finish", "reason": "stop",
            "tokens": {"total": 200, "input": 150, "output": 50, "reasoning": 5,
                       "cache": {"write": 3, "read": 20}},
            "cost": 0.002,
        }),
    ])
    events, _, _ = _parse_opencode_jsonl(stdout)
    usages = [e for e in events if e["type"] == "usage"]
    assert len(usages) == 1
    u = usages[0]
    assert u["tokens"]["input"] == 230
    assert u["tokens"]["output"] == 70
    assert u["tokens"]["reasoning"] == 5
    assert u["tokens"]["total"] == 300
    assert u["tokens"]["cache"] == {"read": 30, "write": 3}
    assert abs(u["cost"] - 0.003) < 1e-9


# ---------- 降级 / 容错 ----------

def test_non_json_lines_degrade_to_raw_text() -> None:
    """非 JSON 行不崩；没有 text part 时 summary 降级为最后一行原始文本。"""
    stdout = "\n".join([
        "some TUI garbage line",
        "{broken json",
        _ev("step_start", {"id": "prt_s1", "type": "step-start"}),
        "最后一段纯文本输出",
    ])
    events, summary, artifacts = _parse_opencode_jsonl(stdout)
    assert summary == "最后一段纯文本输出"
    assert artifacts == []
    finals = [e for e in events if e["type"] == "final"]
    assert finals and finals[0]["content"] == summary


def test_unknown_part_type_skipped() -> None:
    stdout = "\n".join([
        _ev("step_start", {"id": "prt_s1", "type": "step-start"}),
        _ev("whatever", {"id": "prt_x1", "type": "some-future-part", "foo": 1}),
        _ev("text", {"id": "prt_t1", "type": "text", "text": "ok"}),
    ])
    events, summary, _ = _parse_opencode_jsonl(stdout)
    assert summary == "ok"
    assert set(_types(events)) <= {"turn", "final"}


def test_empty_stdout_no_crash() -> None:
    events, summary, artifacts = _parse_opencode_jsonl("")
    assert events == []
    assert summary == ""
    assert artifacts == []


# ---------- 版本解析 ----------

def test_parse_version() -> None:
    assert _parse_version("1.18.4") == (1, 18, 4)
    assert _parse_version("1.18.4\n") >= MIN_VERSION
    assert _parse_version("opencode version 1.18.0") == (1, 18, 0)
    assert _parse_version("0.9.1") < MIN_VERSION
    assert _parse_version("no version here") is None
    assert _parse_version("") is None


# ---------- 配置 ----------

def test_opencode_auto_default_true() -> None:
    s = HubSettings()  # type: ignore[call-arg]
    assert s.hub_opencode_auto is True


def test_opencode_auto_env_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HUB_OPENCODE_AUTO", "false")
    s = HubSettings()  # type: ignore[call-arg]
    assert s.hub_opencode_auto is False
