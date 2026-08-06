"""轻量 webhook 通知（stdlib http.client）。

刻意不用 httpx：httpx 0.28+ 在 Windows + Python 3.14 上 POST 127.0.0.1 的
HTTP/1.1 server 稳定返回 502（httpx 自身的 bug，跟 server 无关）。
queue 的 task 事件和 spawn 的子 agent 终态事件共用这一个实现。
"""

from __future__ import annotations

import asyncio
import http.client
import json
from urllib.parse import urlparse


async def post_webhook(url: str, body: dict) -> int:
    """POST JSON 到 url，返回 HTTP 状态码。失败抛异常由调用方记日志。"""
    p = urlparse(url)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    host = p.hostname or "127.0.0.1"
    port = p.port or (443 if p.scheme == "https" else 80)
    path = p.path or "/"
    if p.query:
        path += "?" + p.query

    def _do_post() -> int:
        if p.scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=10)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=10)
        try:
            conn.request(
                "POST",
                path,
                body=payload,
                headers={"Content-Type": "application/json"},
            )
            r = conn.getresponse()
            r.read()  # drain
            return r.status
        finally:
            conn.close()

    return await asyncio.to_thread(_do_post)


def valid_webhook_url(url: str) -> bool:
    """只接受 http/https 的 webhook 地址。"""
    if not url:
        return False
    try:
        return urlparse(url).scheme in ("http", "https")
    except Exception:  # noqa: BLE001
        return False
