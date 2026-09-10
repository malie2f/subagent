"""连接登记：发布默认不预连；密钥不进 public view。"""
from __future__ import annotations

from pathlib import Path

from mcp_hub.connections import (
    connection_denied,
    load_connections,
    public_tool_view,
    runtime_connected,
    set_runtime_connected,
    set_tool_connected,
    tool_connected,
)


def test_default_not_connected(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runtime_connected("opencode") is False
    assert tool_connected("hedge") is False
    assert load_connections()["runtimes"] == {}


def test_runtime_connect_disconnect(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    set_runtime_connected("opencode", True, binary="opencode")
    assert runtime_connected("opencode") is True
    set_runtime_connected("opencode", False)
    assert runtime_connected("opencode") is False


def test_tool_key_not_in_public_view(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    set_tool_connected("hedge", True, base_url="http://127.0.0.1:9/v1", api_key="secret-key")
    view = public_tool_view("hedge")
    assert view["connected"] is True
    assert view["has_api_key"] is True
    assert view["base_url"].startswith("http://127.0.0.1")
    assert "secret-key" not in str(view)
    assert "api_key" not in view


def test_hedge_has_no_builtin_gateway():
    import mcp_hub.tools.hedge as mod
    assert getattr(mod, "_DEFAULT_BASE_URL", "") in ("", None)


def test_connection_denied_payload():
    d = connection_denied("runtime", "opencode")
    assert d["ok"] is False
    assert "连接" in d["error"]
    assert d["runtime"] == "opencode"
