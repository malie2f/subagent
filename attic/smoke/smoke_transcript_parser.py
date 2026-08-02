"""快速验证 transcript 解析器 —— 不跑真 CLI。"""
import json
import sys

from mcp_hub.runtimes.claude import (
    ClaudeAdapter,
    _looks_like_stream_json,
    _parse_claude_stream_json,
)
from mcp_hub.runtimes.opencode import _build_transcript as opencode_build

print("=" * 60)
print("1) claude adapter available / stream-json support")
print("=" * 60)
a = ClaudeAdapter()
print("available:", a.is_available())
print("stream-json support:", a.supports_stream_json())

print()
print("=" * 60)
print("2) claude stream-json parser (mock data)")
print("=" * 60)
sample = (
    '{"type":"message_start","message":{"role":"assistant"}}\n'
    '{"type":"content_block_start","content_block":{"type":"text","text":""}}\n'
    '{"type":"content_block_delta","delta":{"type":"text_delta","text":"你好"}}\n'
    '{"type":"content_block_delta","delta":{"type":"text_delta","text":"，"}}\n'
    '{"type":"content_block_delta","delta":{"type":"text_delta","text":"世界"}}\n'
    '{"type":"content_block_stop"}\n'
    '{"type":"content_block_start","content_block":{"type":"tool_use","id":"t1","name":"Read","input":{}}}\n'
    '{"type":"content_block_delta","delta":{"type":"input_json_delta","partial_json":"{\\"file_path\\": \\"/tmp/x.py\\""}}\n'
    '{"type":"content_block_stop"}\n'
    '{"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n'
    '{"type":"message_stop"}\n'
    '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"file content here"}]}}\n'
    '{"type":"message_start","message":{"role":"assistant"}}\n'
    '{"type":"content_block_start","content_block":{"type":"text","text":""}}\n'
    '{"type":"content_block_delta","delta":{"type":"text_delta","text":"搞定了。"}}\n'
    '{"type":"content_block_stop"}\n'
    '{"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n'
    '{"type":"message_stop"}\n'
)
print("looks like stream:", _looks_like_stream_json(sample))
events = _parse_claude_stream_json(sample)
print(f"events count: {len(events)}")
for e in events:
    print(f"  - {e.get('type')}: {json.dumps(e, ensure_ascii=False)[:120]}")

print()
print("=" * 60)
print("3) opencode text-mode transcript builder")
print("=" * 60)
oc_events = opencode_build(
    prompt="修一下 foo.py",
    stdout="● Read foo.py\n● Edited foo.py\n● Wrote file bar.py\n\n改好了。",
    stderr="",
    artifacts=["foo.py", "bar.py"],
    summary="改好了。",
    exit_code=0,
)
print(f"events count: {len(oc_events)}")
for e in oc_events:
    print(f"  - {e.get('type')}: {json.dumps(e, ensure_ascii=False)[:120]}")
