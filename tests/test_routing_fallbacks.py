"""routing / fallbacks 纯函数测试（不依赖 server.py 全局）。"""

from __future__ import annotations

from mcp_hub.fallbacks import (
    _is_antigravity_capacity_error,
    _is_timeout_error,
    _pick_antigravity_fallback,
)
from mcp_hub.routing import _classify_task, _fallback_recommend_model
from mcp_hub.runtimes.base import SubagentResult


def _result(stderr: str = "", error: str | None = None, exit_code: int = 1) -> SubagentResult:
    return SubagentResult(
        runtime="x", model="m", task_id="t", exit_code=exit_code,
        stdout="", stderr=stderr, duration_sec=1, error=error, prompt="p",
    )


# ---- routing ----

def test_classify_task_keywords():
    assert _classify_task("帮我写一个排序函数") == "coding"
    assert _classify_task("把这个 bug 修一下") == "coding"
    # 纯闲聊/问答不该判成 coding
    assert _classify_task("今天天气怎么样") != "coding"


def test_fallback_recommend_model_priorities():
    for prio in ("fast", "balanced", "quality"):
        r = _fallback_recommend_model("写个爬虫", prio)
        assert r["runtime"] and r["model"]
        assert "reason" in r


# ---- fallbacks ----

def test_timeout_detection():
    assert _is_timeout_error(_result(error="timed out after 600s"))
    assert _is_timeout_error(_result(stderr="TimeoutError: wait_and_collect"))
    assert not _is_timeout_error(_result(stderr="syntax error"))


def test_antigravity_capacity():
    assert _is_antigravity_capacity_error(_result(stderr="no capacity available"))
    assert _is_antigravity_capacity_error(_result(error="model unavailable (code 503)"))
    assert not _is_antigravity_capacity_error(_result(stderr="file not found"))


def test_antigravity_fallback_pick():
    fb = _pick_antigravity_fallback("gemini-3.6-flash-high")
    assert fb is None or fb != "gemini-3.6-flash-high"  # 回退不能是原模型
