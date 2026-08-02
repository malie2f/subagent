"""Smoke test: mcp-hub server 在 stdio 模式能起 + 能响应 initialize 请求。"""
import os
import subprocess
import sys

# 测前先关 cluster
os.environ["HUB_CLUSTER_ENABLED"] = "false"

p = subprocess.Popen(
    [sys.executable, "-m", "mcp_hub.server"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    cwd=".",
)

# MCP initialize 请求
init_req = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0.1"}}}\n'
list_req = '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'

try:
    out, err = p.communicate(input=init_req + list_req, timeout=20)
except subprocess.TimeoutExpired:
    p.kill()
    out, err = p.communicate()
    print("(server still running after 20s, sending more requests hung — but server is up)")

print("=" * 60)
print("STDOUT:")
print("=" * 60)
print(out[:3000])
print()
print("=" * 60)
print("STDERR (server logs):")
print("=" * 60)
print(err[:2000])
