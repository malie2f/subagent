"""模型 adapter 基类与统一消息结构。"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    """统一消息格式 —— 所有 adapter 都接收/返回这个。"""

    role: str  # system | user | assistant
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class ChatRequest:
    """统一请求结构。"""

    messages: list[Message]
    max_tokens: int = 1024
    temperature: float = 0.7
    system: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatResponse:
    """统一响应结构。"""

    text: str
    model: str
    provider: str
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


class ModelAdapter(abc.ABC):
    """所有模型 adapter 必须实现 chat()。"""

    def __init__(self, name: str, config):
        self.name = name
        self.config = config

    @abc.abstractmethod
    async def chat(self, req: ChatRequest) -> ChatResponse:
        """同步对话接口。"""

    @abc.abstractmethod
    async def health(self) -> bool:
        """健康检查。"""

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.config.provider,
            "model": self.config.model,
            "base_url": self.config.base_url,
        }
