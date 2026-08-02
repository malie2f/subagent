"""端到端：派活到 opencode/qwen3.7-plus。"""
import json
import sys
import urllib.request

sys.path.insert(0, ".")

req = urllib.request.Request(
    "http://127.0.0.1:8766/api/cluster/submit",
    data=json.dumps({
        "payload": "用一句话解释 Go 语言的 goroutine",
        "topic": "opencode.work",
        "from_model": "用户",
        "runtime": "opencode",
        "model": "qwen/qwen3.7-plus",
    }).encode("utf-8"),
    headers={"Content-Type": "application/json; charset=utf-8"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=5) as r:
    body = json.loads(r.read().decode("utf-8"))
    print(f"submit status: {r.status}")
    print(f"  task_id: {body.get('task_id')}")
    print(f"  for_model: {body.get('for_model')}")
    print(f"  topic: {body.get('topic')}")
    print(f"  from_model: {body.get('from_model')}")
