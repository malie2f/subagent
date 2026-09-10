"""hedge-gateway 多模态工具适配器 —— OpenAI 兼容 HTTP 网关（qwen 系）。

地址和 key 都由本机配置提供：HEDGE_BASE_URL / HEDGE_API_KEY，或 dashboard
「连接」页填写。发布版本不预置任何网关。

CDN 图片下载：直连失败时若设了 HEDGE_DOWNLOAD_PROXY 才走 HTTP 代理回退
（设 off/none/direct 禁用）。不设则不代理。

支持的操作：
  - vision          看图理解（chat/completions + image_url part；
                    本地文件读 bytes 转 base64 data URI，http(s) URL 直传网关代下载）
  - image_generate  生图（/v1/images/generations，b64/url 落盘，url 下载带代理回退）
  - video_generate  生视频（/v1/videos/generations——网关侧代码在未实测，尽力透传）
  - models          列模型（GET /v1/models）

刻意不用 httpx：httpx 0.28+ 在 Windows + Python 3.14 有 502 bug（见 notify.py）。
网关请求体上限 32MB，base64 膨胀 ~33%，故单图 raw 限 20MB（_MAX_IMAGE_BYTES）。
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import mimetypes
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .base import ToolAdapter, ToolResult

_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 网关 32MB 请求体上限，b64 膨胀 33% 后的安全线


class HedgeAdapter(ToolAdapter):
    name = "hedge"
    binary = ""  # 纯 HTTP 服务，无本地 CLI

    _OPERATIONS = ["vision", "image_generate", "video_generate", "models"]

    def __init__(self, base_url: str = "", api_key: str = ""):
        super().__init__()
        import os

        from mcp_hub.connections import tool_settings

        ts = tool_settings("hedge")
        settings_url = os.environ.get("HEDGE_BASE_URL", "")
        settings_key = os.environ.get("HEDGE_API_KEY", "")
        try:
            from mcp_hub.config import load_settings
            s = load_settings()
            settings_url = settings_url or (s.hedge_base_url or "")
            settings_key = settings_key or (s.hedge_api_key or "")
        except Exception:  # noqa: BLE001
            pass
        self._base_url = (base_url or ts.get("base_url") or settings_url or "").rstrip("/")
        self._api_key = api_key or ts.get("api_key") or settings_key or ""
        self._max_image_bytes = _MAX_IMAGE_BYTES
        # 只有显式配置了 HEDGE_DOWNLOAD_PROXY 才走代理，发布默认不指向任何第三方主机。
        proxy = os.environ.get("HEDGE_DOWNLOAD_PROXY") or ""
        self._download_proxy = "" if proxy.lower() in ("", "off", "none", "direct") else proxy

    def is_available(self) -> bool:
        # HTTP 服务没有"装没装"的概念；配了地址就算可用，通不通调用时见分晓
        return bool(self._base_url)

    def list_operations(self) -> list[str]:
        return list(self._OPERATIONS)

    def info(self) -> dict[str, Any]:
        d = super().info()
        d["base_url"] = self._base_url
        d["has_api_key"] = bool(self._api_key)
        d["download_proxy"] = self._download_proxy or None
        return d

    # ---------- 内部：HTTP ----------

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        timeout: int = 120,
    ) -> tuple[int, Any]:
        """同步 HTTP（在 to_thread 里跑）。返回 (status, json或text)。"""
        p = urlparse(self._base_url)
        host = p.hostname or "127.0.0.1"
        port = p.port or (443 if p.scheme == "https" else 80)
        prefix = p.path.rstrip("/")
        full_path = f"{prefix}{path}"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        conn_cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(host, port, timeout=timeout)
        try:
            conn.request(method, full_path, body=payload, headers=headers)
            r = conn.getresponse()
            raw = r.read()
            text = raw.decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(text)
            except json.JSONDecodeError:
                return r.status, text
        finally:
            conn.close()

    @staticmethod
    def _err(operation: str, started: float, msg: str) -> ToolResult:
        return ToolResult(
            tool="hedge", operation=operation, ok=False,
            error=msg, duration_sec=time.time() - started,
        )

    def _image_to_uri(self, image: str) -> str:
        """本地路径 → base64 data URI；http(s) → 原样（网关代下载，30s/20MB 上限）。"""
        if image.startswith(("http://", "https://")):
            return image
        path = Path(image)
        if not path.exists():
            raise ValueError(f"图片不存在: {image}（也不是 http(s) URL）")
        size = path.stat().st_size
        if size > self._max_image_bytes:
            raise ValueError(
                f"图片 {size / 1024 / 1024:.1f}MB 超过 {self._max_image_bytes / 1024 / 1024:.0f}MB 上限"
                "（网关请求体 32MB，base64 膨胀 ~33%）"
            )
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{b64}"

    # ---------- 操作 ----------

    async def call(self, operation: str, **kwargs: Any) -> ToolResult:
        started = time.time()
        if operation == "vision":
            return await self._vision(started, **kwargs)
        if operation == "image_generate":
            return await self._image_generate(started, **kwargs)
        if operation == "video_generate":
            return await self._video_generate(started, **kwargs)
        if operation == "models":
            return await self._models(started)
        return self._err(operation, started, f"未知操作: {operation}")

    async def _vision(
        self,
        started: float,
        image: str = "",
        prompt: str = "",
        model: str = "qwen3.8-max-thinking",
        max_tokens: int = 4096,
        **_: Any,
    ) -> ToolResult:
        if not image:
            return self._err("vision", started, "缺参数 image（本地路径或 http(s) URL）")
        try:
            uri = self._image_to_uri(image)
        except (ValueError, OSError) as e:
            return self._err("vision", started, str(e))
        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt or "描述这张图片"},
                        {"type": "image_url", "image_url": {"url": uri}},
                    ],
                }
            ],
            "max_tokens": max_tokens,
        }
        try:
            status, data = await asyncio.to_thread(
                self._request, "POST", "/chat/completions", body, 180
            )
        except Exception as e:  # noqa: BLE001
            return self._err("vision", started, f"请求失败: {e}")
        if status != 200 or not isinstance(data, dict):
            return self._err("vision", started, f"HTTP {status}: {str(data)[:500]}")
        try:
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, list):  # 有的实现把 content 拆成 parts
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        except (KeyError, IndexError, TypeError):
            return self._err("vision", started, f"响应结构异常: {str(data)[:500]}")
        return ToolResult(
            tool=self.name, operation="vision", ok=True,
            data={"content": content, "model": data.get("model", model), "usage": data.get("usage")},
            duration_sec=time.time() - started,
        )

    async def _image_generate(
        self,
        started: float,
        prompt: str = "",
        size: str = "1024x1024",
        n: int = 1,
        out_dir: str = "",
        out: str = "",
        model: str = "",
        **_: Any,
    ) -> ToolResult:
        if not prompt:
            return self._err("image_generate", started, "缺参数 prompt")
        body: dict[str, Any] = {"prompt": prompt, "n": max(1, n), "size": size}
        if model:
            body["model"] = model
        try:
            status, data = await asyncio.to_thread(
                self._request, "POST", "/images/generations", body, 300
            )
        except Exception as e:  # noqa: BLE001
            return self._err("image_generate", started, f"请求失败: {e}")
        if status != 200 or not isinstance(data, dict):
            return self._err("image_generate", started, f"HTTP {status}: {str(data)[:500]}")
        items = data.get("data") or []
        if not items:
            return self._err("image_generate", started, f"响应无 data: {str(data)[:500]}")
        saved = await self._save_images(items, out=out, out_dir=out_dir, started=started, op="image_generate")
        if isinstance(saved, ToolResult):
            return saved
        return ToolResult(
            tool=self.name, operation="image_generate", ok=True,
            data={"saved": saved}, duration_sec=time.time() - started,
            files=saved,
        )

    async def _video_generate(
        self,
        started: float,
        prompt: str = "",
        duration: int = 6,
        resolution: str = "768P",
        model: str = "",
        out: str = "",
        **_: Any,
    ) -> ToolResult:
        """生视频。注意：网关侧 /v1/videos/generations 代码在未实测，这里尽力透传。"""
        if not prompt:
            return self._err("video_generate", started, "缺参数 prompt")
        body: dict[str, Any] = {"prompt": prompt, "duration": duration, "resolution": resolution}
        if model:
            body["model"] = model
        try:
            status, data = await asyncio.to_thread(
                self._request, "POST", "/videos/generations", body, 600
            )
        except Exception as e:  # noqa: BLE001
            return self._err("video_generate", started, f"请求失败: {e}")
        if status != 200 or not isinstance(data, dict):
            return self._err("video_generate", started, f"HTTP {status}: {str(data)[:500]}")
        # 异步任务型响应（带 task_id/id）直接透传，调用方自行轮询
        items = data.get("data") or []
        saved: list[str] = []
        if items and out:
            maybe = await self._save_images(items, out=out, out_dir="", started=started, op="video_generate")
            if not isinstance(maybe, ToolResult):
                saved = maybe
        return ToolResult(
            tool=self.name, operation="video_generate", ok=True,
            data={"response": data, "saved": saved},
            duration_sec=time.time() - started, files=saved,
        )

    async def _models(self, started: float) -> ToolResult:
        try:
            status, data = await asyncio.to_thread(self._request, "GET", "/models", None, 30)
        except Exception as e:  # noqa: BLE001
            return self._err("models", started, f"请求失败: {e}")
        if status != 200 or not isinstance(data, dict):
            return self._err("models", started, f"HTTP {status}: {str(data)[:500]}")
        ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
        return ToolResult(
            tool=self.name, operation="models", ok=True,
            data={"models": ids, "raw": data},
            duration_sec=time.time() - started,
        )

    async def _save_images(
        self,
        items: list[dict],
        *,
        out: str,
        out_dir: str,
        started: float,
        op: str,
    ) -> list[str] | ToolResult:
        """把 OpenAI 风格的 data[]（b64_json 或 url）落盘，返回路径列表；失败返回 ToolResult。"""
        saved: list[str] = []
        target_dir = Path(out_dir) if out_dir else (Path(out).parent if out else Path("."))
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return self._err(op, started, f"创建输出目录失败: {e}")
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            dest = Path(out) if (out and len(items) == 1) else target_dir / f"image_{i + 1:03d}.png"
            if item.get("b64_json"):
                try:
                    dest.write_bytes(base64.b64decode(item["b64_json"]))
                except (ValueError, OSError) as e:
                    return self._err(op, started, f"写文件失败 {dest}: {e}")
            elif item.get("url"):
                try:
                    blob = await asyncio.to_thread(self._download, item["url"])
                    dest.write_bytes(blob)
                except Exception as e:  # noqa: BLE001
                    return self._err(op, started, f"下载失败 {item['url']}: {e}")
            else:
                continue
            saved.append(str(dest))
        if not saved:
            return self._err(op, started, f"data 里没有 b64_json/url 可保存: {str(items)[:300]}")
        return saved

    def _download(self, url: str, timeout: int = 120) -> bytes:
        """先直连；失败且配了下载代理则走代理（HTTPS 用 CONNECT 隧道）。

        代理是 gost 轮询上游池，偶有死节点回 503，故代理分支最多试 3 次。
        """
        try:
            return self._fetch(url, None, timeout)
        except Exception as direct_err:  # noqa: BLE001
            if not self._download_proxy:
                raise
            proxy_err: Exception | None = None
            for _ in range(3):
                try:
                    return self._fetch(url, self._download_proxy, timeout)
                except Exception as e:  # noqa: BLE001
                    proxy_err = e
            raise OSError(
                f"直连失败({direct_err})；代理 {self._download_proxy} 重试 3 次仍失败({proxy_err})"
            ) from proxy_err

    @staticmethod
    def _fetch(url: str, proxy: str | None, timeout: int) -> bytes:
        p = urlparse(url)
        host = p.hostname or "127.0.0.1"
        port = p.port or (443 if p.scheme == "https" else 80)
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        conn: http.client.HTTPConnection
        if proxy:
            prox = urlparse(proxy if "://" in proxy else f"http://{proxy}")
            phost = prox.hostname or "127.0.0.1"
            pport = prox.port or 8080
            if p.scheme == "https":
                conn = http.client.HTTPSConnection(phost, pport, timeout=timeout)
                conn.set_tunnel(host, port)
            else:
                conn = http.client.HTTPConnection(phost, pport, timeout=timeout)
                path = url  # 经代理取明文 HTTP 用绝对 URI
        else:
            conn_cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
            conn = conn_cls(host, port, timeout=timeout)
        try:
            conn.request("GET", path)
            r = conn.getresponse()
            if r.status != 200:
                raise OSError(f"HTTP {r.status}")
            return r.read()
        finally:
            conn.close()
