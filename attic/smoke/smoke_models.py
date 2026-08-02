"""test models_all endpoint."""
import sys
sys.path.insert(0, ".")
from mcp_hub.dashboard.api import DashboardState

s = DashboardState()
m = s.models_all()
print("models all ok:", m.get("ok"))
print("runtimes:", list(m.get("by_runtime", {}).keys()))
print("total models:", len(m.get("all", [])))
print("top 5 opencode models:")
for x in m["by_runtime"].get("opencode", [])[:5]:
    print("  " + x["name"])
print("all runtimes + their model counts:")
for r, models in m.get("by_runtime", {}).items():
    print(f"  {r}: {len(models)} models")
