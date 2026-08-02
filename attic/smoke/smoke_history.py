"""test subagent history endpoint."""
import sys
sys.path.insert(0, ".")

from mcp_hub.dashboard.api import DashboardState

state = DashboardState()
subagents = state.subagents()
print(f"subagents count: {subagents.get('count')}")
if subagents.get('subagents'):
    tid = subagents['subagents'][0]['task_id']
    print(f"test task_id: {tid}")
    h = state.subagent_history(tid, limit=3)
    print(f"history ok: {h.get('ok')}")
    for k, v in h.get('history', {}).items():
        print(f"  {k}: {len(v)} items")
        for item in v[:2]:
            print(f"    {item['task_id']} {item['status']} by {item.get('claimed_by')}")
else:
    print("no subagents, test with a known task_id")
    h = state.subagent_history("kimi-agent-test", limit=3)
    print(f"history: {h}")
