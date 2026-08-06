"""hedge-gateway 工具适配器测试 —— 本地假网关（HTTPServer）全链路。

覆盖：
  - _image_to_uri（本地转 data URI / http 直传 / 缺文件 / 超 20MB 限）
  - vision：请求体形状 + content 提取
  - image_generate：b64_json 落盘 / url 下载落盘 / out_dir 命名
  - models：id 提取
  - HTTP 500 → ok=False 透传
"""

from __future__ import annotations

import base64
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from mcp_hub.tools.hedge import HedgeAdapter

_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image-bytes"


class _FakeGateway:
    """模拟 hedge-gateway：记录请求体，按路径回固定响应。"""

    def __init__(self):
        self.requests: list[dict[str, Any]] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0
        self.fail_status: int | None = None  # 设了就连 chat/images 都回这个错误码

    def start(self) -> str:
        gw = self

        class Handler(BaseHTTPRequestHandler):
            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                return json.loads(raw)

            def _reply(self, obj: Any, status: int = 200, raw: bytes | None = None):
                payload = raw if raw is not None else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):  # noqa: N802
                body = self._body()
                gw.requests.append({"path": self.path, "body": body})
                if gw.fail_status:
                    self._reply({"error": "boom"}, status=gw.fail_status)
                    return
                if self.path == "/v1/chat/completions":
                    self._reply({
                        "model": body.get("model"),
                        "choices": [{"message": {"role": "assistant", "content": "是一只橘猫"}}],
                        "usage": {"total_tokens": 42},
                    })
                elif self.path == "/v1/images/generations":
                    n = body.get("n", 1)
                    self._reply({
                        "data": [
                            ({"url": f"http://127.0.0.1:{gw.port}/img/{i}.png"} if i % 2 else {"b64_json": base64.b64encode(_PNG_BYTES).decode()})
                            for i in range(n)
                        ]
                    })
                else:
                    self._reply({"error": "not found"}, status=404)

            def do_GET(self):  # noqa: N802
                if self.path == "/v1/models":
                    self._reply({"data": [{"id": "qwen3.8-max-thinking"}, {"id": "qwen3-vl-plus"}]})
                elif self.path.startswith("/img/"):
                    self._reply(None, raw=_PNG_BYTES)
                else:
                    self._reply({"error": "not found"}, status=404)

            def log_message(self, *a, **k):  # noqa: ANN002, ANN003
                pass

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def gateway():
    gw = _FakeGateway()
    base_url = gw.start()
    yield gw, HedgeAdapter(base_url=base_url)
    gw.stop()


# ---------- _image_to_uri ----------

def test_image_to_uri_local_file(tmp_path: Path) -> None:
    img = tmp_path / "a.png"
    img.write_bytes(_PNG_BYTES)
    adapter = HedgeAdapter(base_url="http://x/v1")
    uri = adapter._image_to_uri(str(img))
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == _PNG_BYTES


def test_image_to_uri_http_passthrough() -> None:
    adapter = HedgeAdapter(base_url="http://x/v1")
    assert adapter._image_to_uri("https://cdn.example.com/a.jpg") == "https://cdn.example.com/a.jpg"


def test_image_to_uri_missing_file() -> None:
    adapter = HedgeAdapter(base_url="http://x/v1")
    with pytest.raises(ValueError, match="不存在"):
        adapter._image_to_uri("C:/no/such/file.png")


def test_image_to_uri_size_guard(tmp_path: Path) -> None:
    img = tmp_path / "big.png"
    img.write_bytes(b"x" * 100)
    adapter = HedgeAdapter(base_url="http://x/v1")
    adapter._max_image_bytes = 10  # 测试里把 20MB 限缩到 10B
    with pytest.raises(ValueError, match="上限"):
        adapter._image_to_uri(str(img))


# ---------- vision ----------

async def test_vision_roundtrip(gateway, tmp_path: Path) -> None:
    gw, adapter = gateway
    img = tmp_path / "q.png"
    img.write_bytes(_PNG_BYTES)
    r = await adapter.call("vision", image=str(img), prompt="图里是什么", model="qwen3.8-max-thinking")
    assert r.ok, r.error
    assert r.data["content"] == "是一只橘猫"
    assert r.data["usage"]["total_tokens"] == 42

    req = gw.requests[0]
    assert req["path"] == "/v1/chat/completions"
    body = req["body"]
    assert body["model"] == "qwen3.8-max-thinking"
    parts = body["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "图里是什么"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_vision_http_url_passthrough(gateway) -> None:
    gw, adapter = gateway
    r = await adapter.call("vision", image="https://cdn.example.com/x.png")
    assert r.ok
    parts = gw.requests[0]["body"]["messages"][0]["content"]
    assert parts[1]["image_url"]["url"] == "https://cdn.example.com/x.png"
    assert parts[0]["text"] == "描述这张图片"  # 默认 prompt


async def test_vision_http_error(gateway) -> None:
    gw, adapter = gateway
    gw.fail_status = 500
    r = await adapter.call("vision", image="https://cdn.example.com/x.png")
    assert not r.ok
    assert "HTTP 500" in r.error


# ---------- image_generate ----------

async def test_image_generate_saves_b64_and_url(gateway, tmp_path: Path) -> None:
    gw, adapter = gateway
    r = await adapter.call(
        "image_generate", prompt="一只猫", n=2, out_dir=str(tmp_path),
    )
    assert r.ok, r.error
    assert len(r.data["saved"]) == 2
    for p in r.data["saved"]:
        assert Path(p).read_bytes() == _PNG_BYTES
    assert gw.requests[0]["body"]["size"] == "1024x1024"


async def test_image_generate_out_single(gateway, tmp_path: Path) -> None:
    gw, adapter = gateway
    dest = tmp_path / "one.png"
    r = await adapter.call("image_generate", prompt="一只猫", n=1, out=str(dest))
    assert r.ok, r.error
    assert r.data["saved"] == [str(dest)]
    assert dest.read_bytes() == _PNG_BYTES


# ---------- models ----------

async def test_models(gateway) -> None:
    gw, adapter = gateway
    r = await adapter.call("models")
    assert r.ok
    assert r.data["models"] == ["qwen3.8-max-thinking", "qwen3-vl-plus"]


# ---------- 杂项 ----------

def test_available_and_operations() -> None:
    adapter = HedgeAdapter(base_url="http://x/v1")
    assert adapter.is_available()
    assert set(adapter.list_operations()) == {"vision", "image_generate", "video_generate", "models"}


async def test_unknown_operation() -> None:
    adapter = HedgeAdapter(base_url="http://x/v1")
    r = await adapter.call("nope")
    assert not r.ok
    assert "未知操作" in r.error
