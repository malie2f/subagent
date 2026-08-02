"""verify dispatch with proper utf-8."""
import sys
import json
import urllib.request

sys.path.insert(0, ".")

req = urllib.request.Request(
    "http://127.0.0.1:8766/api/cluster/submit",
    data=json.dumps({
        "payload": "测试任务：把 src/auth.py 重构一下，让所有 IO 调用 await 化",
        "topic": "cluster.work",
        "from_model": "用户",
    }).encode("utf-8"),
    headers={"Content-Type": "application/json; charset=utf-8"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=5) as r:
    body = json.loads(r.read().decode("utf-8"))
    print(f"status: {r.status}")
    print(f"body: {json.dumps(body, ensure_ascii=False, indent=2)}")

# 看下任务详情
import time
time.sleep(1)
req2 = urllib.request.Request(f"http://127.0.0.1:8766/api/cluster/task/{body['task_id']}")
with urllib.request.urlopen(req2, timeout=5) as r:
    task = json.loads(r.read().decode("utf-8"))
    print(f"\ntask status:")
    print(f"  {task['task']['status']} by {task['task'].get('claimed_by') or '-'}")
    print(f"  from_model: {task['task'].get('from_model')}")
