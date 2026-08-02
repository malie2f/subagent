"""模型 adapter 注册表。"""

from __future__ import annotations

from .anthropic import AnthropicAdapter
from .base import ChatRequest, ChatResponse, Message, ModelAdapter
from .generic import OpenAICompatibleAdapter
from .minimax import MiniMaxAdapter
from .moonshot import MoonshotAdapter
from .openai import OpenAIAdapter

ADAPTERS = {
    "anthropic": AnthropicAdapter,
    "openai": OpenAIAdapter,
    "moonshot": MoonshotAdapter,
    "minimax": MiniMaxAdapter,
    "custom": OpenAICompatibleAdapter,  # 通用 OpenAI 兼容（Ollama / OpenRouter / vLLM）
}


def build_adapters(configs) -> dict[str, ModelAdapter]:
    """根据配置列表构造 adapter 字典。"""
    out: dict[str, ModelAdapter] = {}
    for cfg in configs:
        cls = ADAPTERS.get(cfg.provider)
        if cls is None:
            continue
        try:
            out[cfg.name] = cls(cfg.name, cfg)
        except Exception as e:  # noqa: BLE001
            # 配置不完整就跳过，不阻塞 hub 启动
            print(f"[hub] skip adapter {cfg.name}: {e}")
    return out


__all__ = [
    "ADAPTERS",
    "AnthropicAdapter",
    "ChatRequest",
    "ChatResponse",
    "Message",
    "MiniMaxAdapter",
    "ModelAdapter",
    "MoonshotAdapter",
    "OpenAIAdapter",
    "OpenAICompatibleAdapter",
    "build_adapters",
]
