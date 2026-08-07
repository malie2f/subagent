"""antigravity `--output-format json` 输出的解析与 usage 事件映射测试。

fixture 来自 antigravity v1.1.9
`antigravity --print ... --model gemini-3.6-flash-low --output-format json`
的真实输出（整段单行 JSON，字段名未改动）。
"""

from __future__ import annotations

import json

from mcp_hub.runtimes.antigravity import (
    _build_transcript,
    _extract_artifacts,
    _extract_summary,
    _map_usage,
    _parse_json_output,
)

# v1.1.9 实测输出原样截段（ag_json.txt）
AG_JSON_LINE = (
    '{"conversation_id":"ed1864e1-d7e9-46d5-8270-531603d79d98",'
    '"status":"SUCCESS","response":"收到\\n","duration_seconds":2.4638334,'
    '"num_turns":1,"usage":{"input_tokens":10095,"output_tokens":42,'
    '"thinking_tokens":38,"cache_read_tokens":8141,"total_tokens":10137}}'
)


def test_real_json_output_parsed():
    """真实 v1.1.9 输出：文本提取 + usage 映射 + 事件结构。"""
    parsed = _parse_json_output(AG_JSON_LINE, "", 0)
    assert parsed is not None
    events, summary, artifacts = parsed

    assert summary == "收到"
    assert artifacts == []

    types = [e["type"] for e in events]
    assert types == ["turn", "usage", "final"]
    assert events[0]["role"] == "assistant"
    assert events[0]["content"] == "收到"

    usage = events[1]
    assert usage["cost"] == 0.0  # 无 cost 源
    tok = usage["tokens"]
    assert tok["input"] == 10095
    assert tok["output"] == 42
    assert tok["reasoning"] == 38   # thinking_tokens
    assert tok["total"] == 10137
    assert tok["cache"]["read"] == 8141  # cache_read_tokens
    assert tok["cache"]["write"] == 0    # 无 cache write 源

    final = events[2]
    assert final["content"] == "收到"
    assert final["stop_reason"] == "ok"


def test_json_with_noise_and_ansi():
    """JSON 前后有噪声行 / ANSI 已由调用方清理，但前后空白要容错。"""
    stdout = f"\n  {AG_JSON_LINE}\n"
    parsed = _parse_json_output(stdout, "", 0)
    assert parsed is not None
    _, summary, _ = parsed
    assert summary == "收到"


def test_response_alias_keys():
    """response 缺失时兼容 text / result 别名。"""
    doc = json.dumps({"status": "SUCCESS", "text": "OK",
                      "usage": {"input_tokens": 3, "output_tokens": 1}})
    parsed = _parse_json_output(doc, "", 0)
    assert parsed is not None
    assert parsed[1] == "OK"
    doc2 = json.dumps({"status": "SUCCESS", "result": "DONE"})
    parsed2 = _parse_json_output(doc2, "", 0)
    assert parsed2 is not None
    assert parsed2[1] == "DONE"


def test_file_links_in_response_make_artifacts_and_file_change():
    """JSON 模式下仍从回复文本里提取文件链接。"""
    doc = json.dumps({
        "status": "SUCCESS",
        "response": "created [foo.txt](file:///C:/work/foo.txt) and updated [bar.md](file:///C:/work/bar.md)",
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    })
    parsed = _parse_json_output(doc, "", 0)
    assert parsed is not None
    events, summary, artifacts = parsed
    assert artifacts == ["C:/work/foo.txt", "C:/work/bar.md"]
    fc = [e for e in events if e["type"] == "file_change"]
    assert {e["path"]: e["action"] for e in fc} == {
        "C:/work/foo.txt": "create",
        "C:/work/bar.md": "modify",
    }


def test_usage_total_fallback_and_no_cache():
    """total_tokens 缺失时 input+output 兜底；cache_read 缺失时 read=0。"""
    ev = _map_usage({"input_tokens": 100, "output_tokens": 40})
    assert ev is not None
    assert ev["tokens"]["total"] == 140
    assert ev["tokens"]["cache"] == {"read": 0, "write": 0}


def test_map_usage_empty_returns_none():
    assert _map_usage({}) is None
    assert _map_usage({"unrelated": 1}) is None


def test_fallback_on_plain_text():
    """老版本 CLI 的纯文本输出：JSON 路径返回 None，老提取路径照常工作。"""
    stdout = "任务完成。\ncreated [a.txt](file:///C:/work/a.txt)\n"
    assert _parse_json_output(stdout, "", 0) is None
    # 老路径行为不变
    assert _extract_summary(stdout).startswith("任务完成")
    assert _extract_artifacts(stdout) == ["C:/work/a.txt"]
    events = _build_transcript(
        prompt="p", stdout=stdout, stderr="",
        artifacts=["C:/work/a.txt"], summary=_extract_summary(stdout),
        exit_code=0,
    )
    types = [e["type"] for e in events]
    assert types == ["file_change", "final"]  # 老结构，无 usage/turn


def test_fallback_on_malformed_or_irrelevant_json():
    """坏 JSON / 与回答无关的 JSON 都回退，不崩。"""
    assert _parse_json_output("{broken json", "", 0) is None
    assert _parse_json_output("", "", 0) is None
    assert _parse_json_output("[1,2,3]", "", 0) is None
    # 合法 JSON 但既无回答文本也无 usage → 视为噪声，回退
    assert _parse_json_output('{"progress": 42}', "", 0) is None


def test_error_status_and_exit_code():
    """非零退出：final stop_reason=error，stderr 进 error 事件。"""
    doc = json.dumps({"status": "FAILED", "response": "",
                      "usage": {"input_tokens": 5, "output_tokens": 1,
                                "total_tokens": 6}})
    parsed = _parse_json_output(doc, "boom", 1)
    assert parsed is not None  # 有 usage，仍按 JSON 路径走
    events, summary, _ = parsed
    assert summary == ""
    types = [e["type"] for e in events]
    assert "usage" in types
    assert "error" in types
    err = [e for e in events if e["type"] == "error"][0]
    assert err["message"] == "boom"


def test_list_models_strips_tab_display_names(monkeypatch):
    """CLI 新版 `antigravity models` 输出 "id\tDisplay Name"：只留 id，
    否则 hub 精确匹配校验过不了、--model 传 tab 名 CLI 不认。"""
    from mcp_hub.runtimes import antigravity as ag

    adapter = ag.AntigravityAdapter()
    monkeypatch.setattr(adapter, "_resolve_cmd", lambda: "/fake/agy")
    monkeypatch.setattr(adapter, "_ensure_auth", lambda b: True)

    class FakeProc:
        returncode = 0
        stdout = (
            "gemini-3.6-flash-low\tGemini 3.6 Flash (Low)\n"
            "gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
            "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
            "bare-id-line\n"
        )
        stderr = ""

    monkeypatch.setattr(ag.subprocess, "run", lambda *a, **k: FakeProc())
    models = adapter.list_models()
    assert models == [
        "gemini-3.6-flash-low",
        "gemini-3.1-pro-high",
        "claude-sonnet-4-6",
        "bare-id-line",
    ]


# 2026-08-08 06:33 真实死亡现场（timeout waiting for response，403k tokens）
AG_ERROR_JSON_LINE = (
    '{"conversation_id":"c5ff7611-5f19-4911-ab89-6c60291f4dea","status":"ERROR",'
    '"response":"","error":"timeout waiting for response","duration_seconds":299.7221408,'
    '"num_turns":1,"usage":{"input_tokens":361746,"output_tokens":42026,'
    '"thinking_tokens":29999,"cache_read_tokens":3182759,"total_tokens":403772}}'
)


def test_error_json_surfaces_real_error_message():
    """ERROR 输出：error event 带真实消息（不再是泛泛的 status=ERROR），usage 仍在。"""
    from mcp_hub.runtimes.antigravity import _parse_json_output

    parsed = _parse_json_output(AG_ERROR_JSON_LINE, "", 1)
    assert parsed is not None
    events, summary, artifacts = parsed
    assert summary == ""  # response 为空
    errors = [e for e in events if e["type"] == "error"]
    assert errors and errors[0]["message"] == "timeout waiting for response"
    usage = [e for e in events if e["type"] == "usage"]
    assert usage and usage[0]["tokens"]["total"] == 403772


def test_extract_json_error():
    from mcp_hub.runtimes.antigravity import _extract_json_error

    assert _extract_json_error(AG_ERROR_JSON_LINE) == "timeout waiting for response"
    assert _extract_json_error(AG_JSON_LINE) == ""  # SUCCESS 无 error 字段
    assert _extract_json_error("not json at all") == ""


def test_extract_session_id_from_log(tmp_path):
    """日志（含实时日志头 + 最终 JSON）里能提取 conversation_id。"""
    from mcp_hub.runtimes.antigravity import AntigravityAdapter
    from mcp_hub.runtimes.base import SubagentHandle

    log = tmp_path / "dead.log"
    log.write_text(
        "=== 实时日志 (2026-08-08 06:33:56) ===\n" + AG_ERROR_JSON_LINE + "\n",
        encoding="utf-8",
    )
    h = SubagentHandle(
        pid=None, runtime="antigravity", model="gemini-3.6-flash-high",
        task_id="t", workdir=str(tmp_path), started_at=0.0, output_file=log,
    )
    assert AntigravityAdapter().extract_session_id(h) == "c5ff7611-5f19-4911-ab89-6c60291f4dea"


async def test_spawn_cmds_print_timeout_and_conversation(monkeypatch, tmp_path):
    """spawn 注入 --print-timeout（略小于 hub 超时）；resume 额外带 --conversation。"""
    from mcp_hub.runtimes import antigravity as ag
    from types import SimpleNamespace

    adapter = ag.AntigravityAdapter()
    monkeypatch.setattr(adapter, "_resolve_cmd", lambda: "/fake/agy")
    monkeypatch.setattr(adapter, "_ensure_auth", lambda b: True)

    captured: list[list[str]] = []

    async def fake_exec(*cmd, **kwargs):
        captured.append(list(cmd))
        return SimpleNamespace(pid=43210)

    monkeypatch.setattr(ag.asyncio, "create_subprocess_exec", fake_exec)

    await adapter.spawn("t1", "gemini-3.6-flash-low", "任务", str(tmp_path), 600)
    cmd1 = captured[0]
    assert "--print-timeout" in cmd1
    assert cmd1[cmd1.index("--print-timeout") + 1] == "585s"  # 600-15
    assert "--conversation" not in cmd1

    await adapter.resume_spawn("conv-abc", "t2", "gemini-3.6-flash-low", "继续", str(tmp_path), 300)
    cmd2 = captured[1]
    assert cmd2[cmd2.index("--conversation") + 1] == "conv-abc"
    assert cmd2[cmd2.index("--print-timeout") + 1] == "285s"  # 300-15
