import asyncio, sys
sys.path.insert(0, r"C:\Users\Lenovo\.minimax\agents\mavis\workspace\mcp-hub")
from mcp_hub.runtimes.grok import GrokAdapter

TASK = """这是一个测试，请原样回复下面两行标记，不要加其它内容：
A1_MARKER
B2_MARKER"""

async def main():
    a = GrokAdapter()
    print("available:", a.is_available())
    print("models:", a.list_models())
    h = await a.spawn("grok-smoke-1", "grok-4.5", TASK, r"C:\Users\Lenovo\.kimi-code\recovered", reasoning_effort="low")
    print("pid:", h.pid)
    r = await a.wait(h, 300)
    print("exit:", r.exit_code)
    print("summary:", repr(r.summary))
    print("session:", a.extract_session_id(h))
    for ev in r.transcript:
        if ev.get("type") == "usage":
            print("usage:", ev)
    assert "A1_MARKER" in r.summary and "B2_MARKER" in r.summary, "multiline prompt lost!"
    print("MULTILINE_OK")

asyncio.run(main())
