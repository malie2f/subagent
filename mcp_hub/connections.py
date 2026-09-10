"""本机 CLI / 工具连接登记。

发布版本默认没有任何 runtime 或网关是「已连接」的——用户必须在
dashboard「连接」页亲手点连接。密钥只写在本机 data/connections.json
（已 gitignore），GET API 永不回传 api_key。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

CONNECTIONS_FILE = Path("./data/connections.json")

_CONNECT_HINT = (
    "该 CLI 尚未在仪表盘连接。打开 http://127.0.0.1:8766 「连接」页，"
    "对要使用的运行时点击「连接」后再派活。"
)


def connections_path() -> Path:
    return CONNECTIONS_FILE


def load_connections() -> dict[str, Any]:
    path = connections_path()
    if not path.exists():
        return {"runtimes": {}, "tools": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"runtimes": {}, "tools": {}}
    if not isinstance(data, dict):
        return {"runtimes": {}, "tools": {}}
    data.setdefault("runtimes", {})
    data.setdefault("tools", {})
    if not isinstance(data["runtimes"], dict):
        data["runtimes"] = {}
    if not isinstance(data["tools"], dict):
        data["tools"] = {}
    return data


def save_connections(data: dict[str, Any]) -> None:
    path = connections_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def runtime_connected(name: str) -> bool:
    entry = load_connections().get("runtimes", {}).get(name) or {}
    return bool(entry.get("connected"))


def tool_connected(name: str) -> bool:
    entry = load_connections().get("tools", {}).get(name) or {}
    return bool(entry.get("connected"))


def tool_settings(name: str) -> dict[str, Any]:
    """本机工具连接配置（可能含 api_key，仅内部使用，禁止进 HTTP 响应）。"""
    entry = load_connections().get("tools", {}).get(name) or {}
    return dict(entry) if isinstance(entry, dict) else {}


def public_tool_view(name: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    entry = tool_settings(name)
    view = {
        "name": name,
        "connected": bool(entry.get("connected")),
        "connected_at": entry.get("connected_at"),
        "has_api_key": bool(entry.get("api_key")),
        "base_url": entry.get("base_url") or "",
        "region": entry.get("region") or "",
    }
    if extra:
        view.update(extra)
    return view


def set_runtime_connected(name: str, connected: bool, *, binary: str = "") -> dict[str, Any]:
    data = load_connections()
    runtimes = data.setdefault("runtimes", {})
    if connected:
        runtimes[name] = {
            "connected": True,
            "connected_at": time.time(),
            "binary": binary,
        }
    else:
        runtimes.pop(name, None)
    save_connections(data)
    return {"ok": True, "name": name, "connected": connected}


def set_tool_connected(
    name: str,
    connected: bool,
    *,
    base_url: str = "",
    api_key: str = "",
    region: str = "",
    keep_existing_key: bool = True,
) -> dict[str, Any]:
    data = load_connections()
    tools = data.setdefault("tools", {})
    if not connected:
        tools.pop(name, None)
        save_connections(data)
        return {"ok": True, "name": name, "connected": False}

    prev = tools.get(name) or {}
    key = api_key if api_key else (prev.get("api_key") if keep_existing_key else "")
    tools[name] = {
        "connected": True,
        "connected_at": time.time(),
        "base_url": (base_url or prev.get("base_url") or "").rstrip("/"),
        "api_key": key,
        "region": region or prev.get("region") or "",
    }
    save_connections(data)
    return public_tool_view(name)


def connection_denied(kind: str, name: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": _CONNECT_HINT if kind == "runtime" else (
            f"工具 '{name}' 尚未在仪表盘连接。打开 http://127.0.0.1:8766 「连接」页配置后再调用。"
        ),
        kind: name,
        "hint": "dashboard 连接页 http://127.0.0.1:8766",
    }
