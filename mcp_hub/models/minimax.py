"""MiniMax adapter —— 同样 OpenAI 兼容，单独放一个类方便以后接 M2 多模态。"""

from __future__ import annotations

from .openai import OpenAIAdapter


class MiniMaxAdapter(OpenAIAdapter):
    """MiniMax 文本模型走 OpenAI 兼容；多模态以后单独加方法。"""

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
