"""zcode --json 输出的 usage 事件映射测试。

fixture 来自 zcode 0.15.2 `zcode --prompt ... --json --mode yolo` 的真实输出
（整段 JSON，已脱敏缩短）。
"""

from __future__ import annotations

import json

from mcp_hub.runtimes.zcode import _extract_usage


def _zcode_doc(usage: dict) -> str:
    return json.dumps({
        "sessionId": "sess_0d3d70ba-0000-4000-8000-000000000000",
        "traceId": "4cc878cc-0000-4000-8000-000000000000",
        "turnId": "turn_9d2cc011-0000-4000-8000-000000000000",
        "response": "OK",
        "usage": usage,
        "eventCount": 11,
        "projection": {"status": "idle", "turnCount": 1},
    }, ensure_ascii=False, indent=2)


def test_json_doc_emits_usage():
    stdout = _zcode_doc({
        "source": "provider",
        "modelRequestCount": 1,
        "inputTokens": 7623,
        "outputTokens": 2,
        "totalTokens": 7625,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "reasoningTokens": 0,
        "webFetchRequests": 0,
        "webSearchRequests": 0,
    })
    ev = _extract_usage(stdout)
    assert ev is not None
    assert ev["type"] == "usage"
    tok = ev["tokens"]
    assert tok["input"] == 7623
    assert tok["output"] == 2
    assert tok["reasoning"] == 0
    assert tok["total"] == 7625
    assert tok["cache"]["read"] == 0
    assert tok["cache"]["write"] == 0
    assert ev["cost"] == 0.0  # 源数据没有 cost 字段


def test_cache_and_reasoning_tokens_mapped():
    stdout = _zcode_doc({
        "inputTokens": 100,
        "outputTokens": 40,
        "reasoningTokens": 25,
        "cacheReadTokens": 900,
        "cacheWriteTokens": 300,
        # totalTokens 缺失 → input+output 兜底
    })
    ev = _extract_usage(stdout)
    assert ev is not None
    tok = ev["tokens"]
    assert tok["reasoning"] == 25
    assert tok["cache"] == {"read": 900, "write": 300}
    assert tok["total"] == 140


def test_plain_text_stdout_no_usage():
    """老格式（纯文本输出）不产生 usage 事件，也不报错。"""
    assert _extract_usage("OK，任务已完成。\n写了 3 个文件。") is None


def test_malformed_usage_no_event():
    assert _extract_usage(_zcode_doc({"source": "provider"})) is None
    assert _extract_usage(_zcode_doc("not-a-dict")) is None
    assert _extract_usage("") is None
    assert _extract_usage("{broken json") is None


def test_json_doc_on_one_line_with_noise():
    """容错：JSON 混在其它行里时逐行扫描。"""
    doc = json.dumps({
        "response": "OK",
        "usage": {"inputTokens": 5, "outputTokens": 1, "totalTokens": 6},
    }, ensure_ascii=False)
    ev = _extract_usage(f"some noise line\n{doc}\n trailing")
    assert ev is not None
    assert ev["tokens"]["input"] == 5
    assert ev["tokens"]["total"] == 6
