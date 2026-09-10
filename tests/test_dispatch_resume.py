"""dispatch_and_wait 辅助逻辑 + resume caller 校验。"""
from __future__ import annotations

from mcp_hub.config import load_settings
from mcp_hub.server import _resume_caller_allowed


def test_resume_caller_placeholder_allowed():
    assert _resume_caller_allowed("unknown", "opencode") is True
    assert _resume_caller_allowed("opencode", "unknown") is True
    assert _resume_caller_allowed("用户", "claude") is True
    assert _resume_caller_allowed("", "claude") is True


def test_resume_caller_same_ok():
    assert _resume_caller_allowed("opencode", "opencode") is True


def test_resume_caller_mismatch_denied():
    assert _resume_caller_allowed("opencode", "claude") is False
    assert _resume_caller_allowed("kimicode", "codex") is False


def test_cluster_disabled_in_env():
    s = load_settings()
    # 本机 .env 关着集群；发布默认也是关
    specs = s.cluster_pool_specs()
    if not s.hub_cluster_enabled:
        assert specs == []
