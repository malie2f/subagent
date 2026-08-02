"""debug webhook 真的发出去没。"""
import asyncio
import json
import logging
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# 开详细日志
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logging.getLogger("httpx").setLevel(logging.INFO)

sys.path.insert(0, ".")

from mcp_hub.queue import TaskStore


received = []


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        print(f"  [server] do_POST called, path={self.path}, headers={dict(self.headers)}", flush=True)
        l = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(l) if l else b"{}"
        print(f"  [server] read {l} bytes, raw[:100]={raw[:100]!r}", flush=True)
        try:
            body = json.loads(raw)
        except Exception as e:
            print(f"  [server] json decode error: {e}", flush=True)
            body = {"raw": raw.decode("utf-8", "replace")}
        received.append(body)
        print(f"  [webhook] received event={body.get('event')}", flush=True)
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        except Exception as e:
            print(f"  [server] send_response error: {e}", flush=True)

    def log_message(self, *a, **k):
        pass


def start_server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = HTTPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}/hook"


async def main():
    srv, url = start_server()
    print(f"webhook server: {url}")

    store = TaskStore("./data/_webhook_debug.json")
    print("publishing...")
    t = await store.publish(topic="d", payload="p", from_model="t", webhook=url)
    print(f"publish done, task_id={t.task_id}")
    print(f"received so far: {len(received)}")
    await asyncio.sleep(2)
    print(f"after 2s sleep: received {len(received)}")
    for r in received:
        print(f"  {r.get('event')}")
    srv.shutdown()


asyncio.run(main())
