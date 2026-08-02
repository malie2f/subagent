"""更小心的 webhook debug。"""
import asyncio
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, ".")

from mcp_hub.queue import TaskStore

# 起 server
received = []
ready = threading.Event()


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        print(f"  >>> [server] do_POST path={self.path}", flush=True)
        try:
            l = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(l) if l else b"{}"
            print(f"  >>> [server] body: {raw[:200]!r}", flush=True)
            body = json.loads(raw)
            received.append(body)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        except Exception as e:
            print(f"  >>> [server] ERROR: {e!r}", flush=True)
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def log_message(self, *a, **k):
        pass


s = socket.socket()
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
s.close()

srv = HTTPServer(("127.0.0.1", port), H)
print(f"server starting on 127.0.0.1:{port}", flush=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
print("server thread started", flush=True)

url = f"http://127.0.0.1:{port}/hook"
print(f"webhook url: {url}", flush=True)


async def main():
    store = TaskStore("./data/_webhook_debug2.json")
    # 等 server 完全启动
    import time as _t
    _t.sleep(0.5)
    print("publishing...", flush=True)
    t = await store.publish(topic="d", payload="p", from_model="t", webhook=url)
    print(f"publish done, task_id={t.task_id}", flush=True)
    print(f"received so far: {len(received)}", flush=True)
    for r in received:
        print(f"  {r.get('event')}", flush=True)


asyncio.run(main())
import time
time.sleep(2)
print(f"after 2s wait: received {len(received)}", flush=True)
srv.shutdown()
