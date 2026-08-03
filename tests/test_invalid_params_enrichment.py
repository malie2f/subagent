"""-32602 错误富化 + stateless 容错补丁的测试。

背景：SDK receive loop 把请求校验失败、会话未初始化等异常统一报成
-32602 "Invalid request parameters" + 空 data。server.py 顶部的补丁会：
  1. 把真实原因写进 message，细节 + CALLING_SPEC §2 自查清单写进 data
  2. 强制 ServerSession stateless，hub 重启后客户端重连不再卡 NotInitialized
"""

from __future__ import annotations

import anyio
import pytest
from mcp import types
from mcp.server.lowlevel.server import InitializationOptions
from mcp.server.session import InitializationState, ServerSession
from mcp.shared import session as shared_session

import mcp_hub.server as hub_server

_ENRICHED = hub_server._EnrichedErrorData
_INVALID_PARAMS = types.INVALID_PARAMS
_MAGIC_MESSAGE = "Invalid request parameters"


def _build_in_except(exc: BaseException, **kwargs):
    """模拟 receive loop：在 except 块里构造 ErrorData。"""
    try:
        raise exc
    except Exception:
        return _ENRICHED(**kwargs)


def test_patch_applied_to_session_module():
    assert shared_session.ErrorData is _ENRICHED


def test_not_initialized_error_is_explained():
    err = _build_in_except(
        RuntimeError("Received request before initialization was complete"),
        code=_INVALID_PARAMS,
        message=_MAGIC_MESSAGE,
        data="",
    )
    assert err.message.startswith("Invalid request parameters: ")
    assert "initialize" in err.message
    assert "重连" in err.message
    assert "CALLING_SPEC" in err.data
    assert "自查" in err.data


def test_validation_error_lists_fields():
    payload = {
        "method": "tools/call",
        "params": {"name": "list_runtimes", "arguments": ["not", "a", "dict"]},
    }
    try:
        types.ClientRequest.model_validate(payload)
        pytest.fail("应当抛出 ValidationError")
    except Exception as exc:  # noqa: BLE001
        err = _build_in_except(exc, code=_INVALID_PARAMS, message=_MAGIC_MESSAGE, data="")
    assert "校验失败" in err.message
    assert "arguments" in err.data
    assert "自查" in err.data
    # union 噪音（其它分支的 method 不匹配）不应出现
    assert "PingRequest" not in err.data
    assert "InitializeRequest" not in err.data


def test_generic_exception_shows_type_and_text():
    err = _build_in_except(
        ValueError("boom"),
        code=_INVALID_PARAMS,
        message=_MAGIC_MESSAGE,
        data="",
    )
    assert "ValueError: boom" in err.message


def test_other_error_messages_untouched():
    # 不是 receive loop 兜底分支的 ErrorData（message 不同），即使在 except 里也不动
    try:
        raise RuntimeError("whatever")
    except Exception:
        err = _ENRICHED(code=_INVALID_PARAMS, message="some other error", data="")
    assert err.message == "some other error"
    assert err.data == ""


def test_no_active_exception_untouched():
    err = _ENRICHED(code=_INVALID_PARAMS, message=_MAGIC_MESSAGE, data="")
    assert err.message == _MAGIC_MESSAGE
    assert err.data == ""


def test_existing_data_untouched():
    try:
        raise RuntimeError("Received request before initialization was complete")
    except Exception:
        err = _ENRICHED(code=_INVALID_PARAMS, message=_MAGIC_MESSAGE, data="已有内容")
    assert err.message == _MAGIC_MESSAGE
    assert err.data == "已有内容"


def test_server_session_created_stateless():
    send_stream, recv_stream = anyio.create_memory_object_stream(1)
    session = ServerSession(
        recv_stream,
        send_stream,
        InitializationOptions(
            server_name="test",
            server_version="0.0.0",
            capabilities=types.ServerCapabilities(),
        ),
    )
    assert session._initialization_state is InitializationState.Initialized


def test_string_arguments_still_tolerated():
    # 既有兼容补丁的回归保护：arguments 传 JSON 字符串也能解析
    payload = {
        "method": "tools/call",
        "params": {"name": "list_runtimes", "arguments": "{}"},
    }
    req = types.ClientRequest.model_validate(payload)
    assert req.root.params.arguments == {}
