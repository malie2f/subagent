"""check routes registered."""
import sys
sys.path.insert(0, ".")
from mcp_hub.dashboard.server import create_app
app = create_app()
print("routes:")
for r in app.url_map.iter_rules():
    methods = ",".join(sorted(r.methods - {"HEAD", "OPTIONS"}))
    print(f"  [{methods}] {r.rule}")
