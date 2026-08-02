"""Usage stats —— 从 registry + transcript 聚合各模型的 token / cost 用量。

数据来源：
  - registry（data/subagents_registry.json）：每个 subagent 的 runtime/model/
    workdir/log_file/started_at/status
  - transcript（<log_file 去掉 .log>.transcript.jsonl）：adapter 解析时落盘的
    {"type":"usage", "tokens": {...}, "cost": ...} 事件

usage 事件覆盖情况（2026-08-02 时点）：
  - opencode ✅（聚合所有 step_finish 后单次发出，tokens 含 cache read/write + cost）
  - grok     ✅（单 JSON 输出映射，cost = total_cost_usd 刊例估算价）
  - codex    ✅（turn.completed 的 usage 映射；多 turn 会话会多次发出，按出现求和）
  - claude   ✅ / zcode ✅（2026-08-02 补）
  - kimi     ✅（stdout 无 usage，改从 session 的 wire.jsonl 解析：新版
    agents/main/wire.jsonl 的 usage.record + 旧版 StatusUpdate.token_usage 双格式）
  - codebuddy / qoder ✗（text 模式无数据源；改 spawn 加 --output-format json 可解，未做）
  - antigravity ✗（text 模式无数据源；--output-format json 已实测可用，切换待做）
  以上 ✗ 任务计入 tasks_without_usage，不编造数据。

聚合口径：一个 transcript 文件内所有 usage 事件求和；按 (runtime, model) 和
按天（started_at 本地日期）两个维度分组。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

# tokens 里参与求和的字段
_TOKEN_KEYS = ("input", "output", "reasoning", "total")
_CACHE_KEYS = ("read", "write")


def _transcript_path(task_id: str, entry: dict[str, Any]) -> Path | None:
    """从 registry 条目推 transcript 路径：优先 log_file 推导，回退 workdir 约定路径。"""
    log_file = entry.get("log_file") or ""
    candidates: list[Path] = []
    if log_file:
        lf = Path(log_file)
        if lf.suffix == ".log":
            candidates.append(lf.with_name(lf.stem + ".transcript.jsonl"))
        else:
            candidates.append(Path(str(lf) + ".transcript.jsonl"))
    workdir = entry.get("workdir") or ""
    if workdir:
        candidates.append(
            Path(workdir) / ".mcp-hub" / "subagents" / f"{task_id}.transcript.jsonl"
        )
    for p in candidates:
        if p.is_file():
            return p
    return None


def _sum_usage_events(transcript: Path) -> dict[str, Any] | None:
    """读一个 transcript.jsonl，把所有 usage 事件求和。没有则返回 None。"""
    tokens: dict[str, int] = {k: 0 for k in _TOKEN_KEYS}
    cache: dict[str, int] = {k: 0 for k in _CACHE_KEYS}
    cost = 0.0
    found = False
    try:
        with transcript.open("r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
                if not line.startswith("{") or '"usage"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict) or ev.get("type") != "usage":
                    continue
                tok = ev.get("tokens")
                if isinstance(tok, dict):
                    for k in _TOKEN_KEYS:
                        v = tok.get(k)
                        if isinstance(v, (int, float)):
                            tokens[k] += int(v)
                            found = True
                    c = tok.get("cache")
                    if isinstance(c, dict):
                        for k in _CACHE_KEYS:
                            v = c.get(k)
                            if isinstance(v, (int, float)):
                                cache[k] += int(v)
                v = ev.get("cost")
                if isinstance(v, (int, float)):
                    cost += float(v)
                    found = True
    except OSError:
        return None
    if not found:
        return None
    return {"tokens": {**tokens, "cache": cache}, "cost": cost}


def _add_bucket(bucket: dict[str, Any], usage: dict[str, Any]) -> None:
    bucket["tasks"] += 1
    for k in _TOKEN_KEYS:
        bucket["tokens"][k] += usage["tokens"].get(k, 0)
    for k in _CACHE_KEYS:
        bucket["tokens"]["cache"][k] += usage["tokens"]["cache"].get(k, 0)
    bucket["cost"] += usage.get("cost", 0.0)


def _new_bucket() -> dict[str, Any]:
    return {
        "tasks": 0,
        "tokens": {**{k: 0 for k in _TOKEN_KEYS}, "cache": {k: 0 for k in _CACHE_KEYS}},
        "cost": 0.0,
    }


def collect_usage(registry_path: str | Path | None = None) -> dict[str, Any]:
    """扫 registry，聚合所有 subagent 的 token / cost 用量。

    返回：
      {
        "total": {...bucket},
        "by_model": {"runtime/model": {...bucket}},
        "by_day":   {"2026-08-02": {...bucket}},
        "tasks_total": N,
        "tasks_with_usage": N,
        "tasks_without_usage": N,   # adapter 不吐 usage 的任务数（见模块 docstring）
      }
    """
    if registry_path is None:
        from .config import load_settings

        queue_path = Path(load_settings().hub_queue_path).expanduser().resolve()
        registry_path = queue_path.parent / "subagents_registry.json"
    registry_path = Path(registry_path)

    entries: dict[str, Any] = {}
    if registry_path.is_file():
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("subagents"), dict):
                entries = data["subagents"]
        except (OSError, json.JSONDecodeError):
            entries = {}

    total = _new_bucket()
    by_model: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    with_usage = 0
    without_usage = 0

    for task_id, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        transcript = _transcript_path(task_id, entry)
        usage = _sum_usage_events(transcript) if transcript else None
        if usage is None:
            without_usage += 1
            continue
        with_usage += 1

        _add_bucket(total, usage)

        key = f"{entry.get('runtime', '?')}/{entry.get('model', '?')}"
        _add_bucket(by_model.setdefault(key, _new_bucket()), usage)

        started = entry.get("started_at")
        day = time.strftime("%Y-%m-%d", time.localtime(started)) if isinstance(started, (int, float)) else "unknown"
        _add_bucket(by_day.setdefault(day, _new_bucket()), usage)

    return {
        "total": total,
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1]["tokens"]["total"])),
        "by_day": dict(sorted(by_day.items())),
        "tasks_total": with_usage + without_usage,
        "tasks_with_usage": with_usage,
        "tasks_without_usage": without_usage,
    }
