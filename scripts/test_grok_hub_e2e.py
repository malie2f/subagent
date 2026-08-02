import asyncio, json, sys
sys.path.insert(0, r"C:\Users\Lenovo\.minimax\agents\mavis\workspace\mcp-hub")
from mcp_hub.server import spawn_subagent

async def main():
    out = await spawn_subagent(
        runtime="grok", model="grok-4.5",
        task="只回复两个字：收到",
        workdir=r"C:\Users\Lenovo\.kimi-code\recovered",
        timeout_sec=300, wait=True, reasoning_effort="low",
    )
    d = json.loads(out)
    print(json.dumps({k: d.get(k) for k in ("ok", "runtime", "model", "exit_code", "summary", "duration_sec")}, ensure_ascii=False, indent=1))

asyncio.run(main())
