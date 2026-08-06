"""Tools —— 同步调用型工具（多模态、搜索等），跟 subagent runtime 解耦。

runtimes/ 是 spawn-and-forget 的长任务；
tools/   是 request-response 的短任务，调用即等结果。
"""

from __future__ import annotations

from .base import ToolAdapter, ToolResult
from .hedge import HedgeAdapter
from .mmx import MmxAdapter

REGISTRY = {
    "mmx": MmxAdapter,
    "hedge": HedgeAdapter,
}


def detect_all() -> dict[str, ToolAdapter]:
    """检测所有可用的 tool adapter。"""
    out: dict[str, ToolAdapter] = {}
    for name, cls in REGISTRY.items():
        try:
            a = cls()
            if a.is_available():
                out[name] = a
        except Exception:  # noqa: BLE001
            pass
    return out


__all__ = [
    "HedgeAdapter",
    "MmxAdapter",
    "REGISTRY",
    "ToolAdapter",
    "ToolResult",
    "detect_all",
]
