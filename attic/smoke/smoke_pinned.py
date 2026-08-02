"""test pinned models 过滤."""
import os
import sys
sys.path.insert(0, ".")

# 临时 set pinned models
os.environ["HUB_DASHBOARD_PINNED_MODELS"] = '["opencode-go/deepseek-v4-flash", "opencode-go/kimi-k3", "opencode-go/qwen3.7-plus", "kimi-code/kimi-for-coding"]'

from mcp_hub.dashboard.api import DashboardState
from mcp_hub.config import load_settings

s = load_settings()
print(f"pinned config raw: {s.hub_dashboard_pinned_models}")

ds = DashboardState()
m = ds.models_all()
print(f"pinned: {m.get('pinned')}")
print(f"pinned_models: {m.get('pinned_models')}")
print(f"total models after filter: {len(m.get('all', []))}")
print("models:")
for x in m.get('all', []):
    print(f"  {x['runtime']}/{x['name']}")
