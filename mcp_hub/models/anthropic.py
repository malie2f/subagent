"""Anthropic (Claude) adapter。"""

from __future__ import annotations

from .base import ChatRequest, ChatResponse, ModelAdapter


class AnthropicAdapter(ModelAdapter):
    """走 anthropic 官方 SDK（也支持自定义 base_url 做代理）。"""

    def __init__(self, name: str, config):
        super().__init__(name, config)
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:  # noqa: BLE001
            raise RuntimeError("anthropic SDK 未安装：pip install anthropic") from e
        self._client = AsyncAnthropic(
            api_key=config.api_key,
            base_url=config.base_url,
        )

    async def chat(self, req: ChatRequest) -> ChatResponse:
        # 拆出 system
        system = req.system
        messages = []
        for m in req.messages:
            if m.role == "system":
                system = system or m.content
            else:
                messages.append({"role": m.role, "content": m.content})

        resp = await self._client.messages.create(
            model=self.config.model,
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            system=system or "You are a helpful assistant.",
            messages=messages,
        )

        text = ""
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text += block.text

        return ChatResponse(
            text=text,
            model=self.config.model,
            provider="anthropic",
            usage={
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
            },
            raw=resp,
        )

    async def health(self) -> bool:
        try:
            # 一次极小调用做探活
            await self._client.messages.create(
                model=self.config.model,
                max_tokens=8,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except Exception:  # noqa: BLE001
            return False
