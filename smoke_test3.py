"""独立测 test_3 逻辑。"""
import asyncio
import json
from pathlib import Path
from mcp_hub.queue import TaskStore

test_file = Path("data/_audit_test_3_isolated.json")
for p in test_file.parent.glob("_audit_test_3_isolated.corrupted-*.json"):
    p.unlink()
test_file.parent.mkdir(parents=True, exist_ok=True)
test_file.write_text(json.dumps({"tasks": [
    {"task_id": "old1", "topic": "t", "status": "pending", "payload": "p", "from_model": "u", "for_model": None,
     "metadata": {}, "created_at": 1, "claimed_at": None, "completed_at": None, "retries": 0, "max_retries": 3,
     "acceptance": {}, "webhook": "", "verify_history": [], "notify_history": []}
]}), encoding="utf-8")
store = TaskStore(test_file)
test_file.write_text('{"tasks": [{"id": "1", "sta', encoding="utf-8")
asyncio.run(store.publish(topic="t", payload="new"))
data = store._load()
tasks = data.get("tasks", [])
has_new = any(t.get("payload") == "new" for t in tasks)
print("has_new:", has_new, "total:", len(tasks))
backups = list(test_file.parent.glob("_audit_test_3_isolated.corrupted-*.json"))
print("backups:", len(backups))
print("condition (has_new and >=1 backup):", has_new and len(backups) >= 1)
if backups:
    print("first backup:", backups[0].name, "size:", backups[0].stat().st_size)
    print("content (first 100 bytes):", backups[0].read_text(encoding="utf-8")[:100])
for p in backups:
    p.unlink()
test_file.unlink()
