"""Moonshot (Kimi) adapter —— OpenAI 兼容协议，但走官方 SDK 避免 path 差异。"""

from __future__ import annotations

from .openai import OpenAIAdapter


class MoonshotAdapter(OpenAIAdapter):
    """Kimi 完全走 OpenAI 兼容协议，复用 OpenAIAdapter 即可。"""

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
