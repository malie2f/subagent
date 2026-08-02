"""OpenAI (Codex / GPT) adapter。"""

from __future__ import annotations

from .base import ChatRequest, ChatResponse, ModelAdapter


class OpenAIAdapter(ModelAdapter):
    """走 openai 官方 SDK。"""

    def __init__(self, name: str, config):
        super().__init__(name, config)
        try:
            from openai import AsyncOpenAI
        except ImportError as e:  # noqa: BLE001
            raise RuntimeError("openai SDK 未安装：pip install openai") from e
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )

    async def chat(self, req: ChatRequest) -> ChatResponse:
        msgs = []
        if req.system:
            msgs.append({"role": "system", "content": req.system})
        for m in req.messages:
            if m.role == "system" and req.system:
                continue
            msgs.append({"role": m.role, "content": m.content})

        resp = await self._client.chat.completions.create(
            model=self.config.model,
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            messages=msgs,
        )

        text = resp.choices[0].message.content or ""
        usage = {}
        if resp.usage:
            usage = {
                "input_tokens": resp.usage.prompt_tokens,
                "output_tokens": resp.usage.completion_tokens,
            }
        return ChatResponse(
            text=text,
            model=self.config.model,
            provider="openai",
            usage=usage,
            raw=resp,
        )

    async def health(self) -> bool:
        try:
            await self._client.chat.completions.create(
                model=self.config.model,
                max_tokens=8,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except Exception:  # noqa: BLE001
            return False
