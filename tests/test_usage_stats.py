"""usage_stats 聚合逻辑测试：造临时 registry + transcript，验证求和和分组。"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_hub.usage_stats import collect_usage


def _write_registry(root: Path, entries: dict) -> Path:
    reg = root / "subagents_registry.json"
    reg.write_text(json.dumps({"subagents": entries}, ensure_ascii=False), encoding="utf-8")
    return reg


def _write_transcript(root: Path, task_id: str, events: list[dict]) -> str:
    d = root / "wd" / ".mcp-hub" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    log = d / f"{task_id}.log"
    log.write_text("", encoding="utf-8")
    tp = d / f"{task_id}.transcript.jsonl"
    lines = [{"type": "prompt", "ts": 1, "content": "x"}] + events
    tp.write_text("\n".join(json.dumps(e) for e in lines), encoding="utf-8")
    return str(log)


def _usage(inp: int, out: int, cost: float, cache_read: int = 0) -> dict:
    tokens = {"input": inp, "output": out, "total": inp + out}
    if cache_read:
        tokens["cache"] = {"read": cache_read, "write": 0}
    return {"type": "usage", "tokens": tokens, "cost": cost}


def test_collect_usage_aggregates(tmp_path):
    wd = str(tmp_path / "wd")
    e1 = {"runtime": "grok", "model": "grok-4.5", "workdir": wd, "started_at": 1785658000.0}
    e2 = {"runtime": "opencode", "model": "opencode-go/deepseek-v4-flash", "workdir": wd, "started_at": 1785658000.0}
    e1["log_file"] = _write_transcript(tmp_path, "t1", [_usage(100, 20, 0.01)])
    e2["log_file"] = _write_transcript(tmp_path, "t2", [_usage(200, 40, 0.02, cache_read=50), _usage(300, 60, 0.03)])
    reg = _write_registry(tmp_path, {"t1": e1, "t2": e2})

    d = collect_usage(reg)
    assert d["tasks_total"] == 2
    assert d["tasks_with_usage"] == 2
    assert d["tasks_without_usage"] == 0
    assert d["total"]["tokens"]["input"] == 600
    assert d["total"]["tokens"]["output"] == 120
    assert d["total"]["tokens"]["cache"]["read"] == 50
    assert abs(d["total"]["cost"] - 0.06) < 1e-9
    assert d["by_model"]["grok/grok-4.5"]["tokens"]["input"] == 100
    assert d["by_model"]["opencode/opencode-go/deepseek-v4-flash"]["tasks"] == 1
    assert len(d["by_day"]) == 1


def test_collect_usage_missing_transcript(tmp_path):
    e = {"runtime": "claude", "model": "botcf-claude/claude-opus-5", "workdir": str(tmp_path / "nowhere"), "started_at": 1785658000.0, "log_file": ""}
    reg = _write_registry(tmp_path, {"t3": e})
    d = collect_usage(reg)
    assert d["tasks_with_usage"] == 0
    assert d["tasks_without_usage"] == 1
    assert d["total"]["tokens"]["total"] == 0


def test_collect_usage_empty_registry(tmp_path):
    d = collect_usage(tmp_path / "nonexistent.json")
    assert d["tasks_total"] == 0
    assert d["by_model"] == {}
