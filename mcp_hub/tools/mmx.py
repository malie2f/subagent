"""mmx 多模态工具适配器 —— 包装 MiniMax MMX-CLI（mmx <resource> <command>）。

支持的操作：
  - chat           文本对话
  - image_generate 图像生成
  - speech         语音合成
  - music_generate 音乐生成
  - search         搜索
  - vision         看图理解
  - quota          配额查询
  - voices         列语音 preset

调用方式：调 mmx CLI 子进程，stdout 抽 JSON，返回结构化结果。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .base import ToolAdapter, ToolResult


class MmxAdapter(ToolAdapter):
    name = "mmx"
    binary = "mmx"

    _OPERATIONS = [
        "chat",
        "image_generate",
        "speech",
        "music_generate",
        "search",
        "vision",
        "quota",
        "voices",
        "video_generate",
        "video_get",
    ]

    def __init__(self, api_key: str = ""):
        """mmx 不读环境变量认 key，只能通过 --api-key flag 传或写 ~/.mmx/config.json。

        这里用 flag 方式，简单直接。
        key 优先级：显式传入 > 环境变量 MMX_API_KEY > 空
        """
        super().__init__()
        if not api_key:
            import os
            api_key = os.environ.get("MMX_API_KEY", "")
        self._api_key = api_key

    def is_available(self) -> bool:
        return (
            shutil.which(self.binary) is not None
            or shutil.which(self.binary + ".cmd") is not None
            or shutil.which(self.binary + ".exe") is not None
        )

    def list_operations(self) -> list[str]:
        return list(self._OPERATIONS)

    def info(self) -> dict[str, Any]:
        d = super().info()
        d["has_api_key"] = bool(self._api_key)
        return d

    def _with_key(self, cmd: list[str]) -> list[str]:
        """如果有 api_key，给每个 mmx 子命令加 --api-key flag。

        重要：mmx 要求 --api-key 放在子命令之前（在 resource 前面），
        实测 `mmx --api-key xxx quota show` 成功，而 `mmx quota --api-key xxx show` 失败。
        """
        if not self._api_key:
            return cmd
        # cmd[0] 是绝对路径（如 C:\\...\\mmx.CMD），不能直接和 self.binary 比。
        # 只要 cmd 至少有 binary + resource + command 3 段就注入。
        if len(cmd) >= 3:
            new_cmd = [cmd[0], "--api-key", self._api_key] + cmd[1:]
            return new_cmd
        return cmd

    async def call(self, operation: str, **kwargs: Any) -> ToolResult:
        binary = self._resolve_cmd()
        if not binary:
            return ToolResult(
                tool=self.name,
                operation=operation,
                ok=False,
                error="mmx not in PATH",
            )

        handler = {
            "chat": self._op_chat,
            "image_generate": self._op_image_generate,
            "speech": self._op_speech,
            "music_generate": self._op_music_generate,
            "search": self._op_search,
            "vision": self._op_vision,
            "quota": self._op_quota,
            "voices": self._op_voices,
            "video_generate": self._op_video_generate,
            "video_get": self._op_video_get,
        }.get(operation)

        if handler is None:
            return ToolResult(
                tool=self.name,
                operation=operation,
                ok=False,
                error=f"unknown operation '{operation}'；可选：{self._OPERATIONS}",
            )

        return await handler(binary, **kwargs)

    # ---------- 各操作 ----------

    async def _op_chat(self, binary: str, *, message: str, model: str = "MiniMax-M3",
                       system: str = "", max_tokens: int = 4096,
                       temperature: float = 0.7, **_) -> ToolResult:
        cmd = [binary, "text", "chat", "--message", message, "--output", "json"]
        if model:
            cmd += ["--model", model]
        if system:
            cmd += ["--system", system]
        cmd += ["--max-tokens", str(max_tokens), "--temperature", str(temperature)]
        return await self._run(self._with_key(cmd), operation="chat", timeout_sec=120)

    async def _op_image_generate(self, binary: str, *, prompt: str,
                                 aspect_ratio: str = "1:1", n: int = 1,
                                 out_dir: str = "", out: str = "",
                                 seed: int = 0, model: str = "",
                                 **_) -> ToolResult:
        cmd = [binary, "image", "generate", "--prompt", prompt,
               "--response-format", "url", "--n", str(n), "--output", "json"]
        if model:
            cmd += ["--model", model]
        if aspect_ratio:
            cmd += ["--aspect-ratio", aspect_ratio]
        if seed:
            cmd += ["--seed", str(seed)]
        if out:
            cmd += ["--out", out]
        elif out_dir:
            cmd += ["--out-dir", out_dir]
        return await self._run(self._with_key(cmd), operation="image_generate", timeout_sec=180)

    async def _op_speech(self, binary: str, *, text: str, voice: str = "",
                         out: str = "", format: str = "mp3",
                         speed: float = 0, pitch: int = 0,
                         **_) -> ToolResult:
        cmd = [binary, "speech", "synthesize", "--text", text, "--format", format]
        if voice:
            cmd += ["--voice", voice]
        if speed:
            cmd += ["--speed", str(speed)]
        if pitch:
            cmd += ["--pitch", str(pitch)]
        if out:
            cmd += ["--out", out]
        return await self._run(self._with_key(cmd), operation="speech", timeout_sec=120, expect_json=False)

    async def _op_music_generate(self, binary: str, *, prompt: str,
                                 out: str = "", lyrics: str = "",
                                 sample_rate: int = 0, bitrate: int = 0,
                                 **_) -> ToolResult:
        cmd = [binary, "music", "generate", "--prompt", prompt]
        if lyrics:
            cmd += ["--lyrics", lyrics]
        if out:
            cmd += ["--out", out]
        if sample_rate:
            cmd += ["--sample-rate", str(sample_rate)]
        if bitrate:
            cmd += ["--bitrate", str(bitrate)]
        return await self._run(self._with_key(cmd), operation="music_generate", timeout_sec=180, expect_json=False)

    async def _op_search(self, binary: str, *, query: str, count: int = 10,
                         **_) -> ToolResult:
        cmd = [binary, "search", "query", query, "--output", "json"]
        return await self._run(self._with_key(cmd), operation="search", timeout_sec=60)

    async def _op_vision(self, binary: str, *, image: str, prompt: str = "",
                         **_) -> ToolResult:
        cmd = [binary, "vision", "describe", "--output", "json"]
        if image.startswith("http"):
            cmd += ["--url", image]
        else:
            cmd += ["--image", image]
        if prompt:
            cmd += ["--prompt", prompt]
        return await self._run(self._with_key(cmd), operation="vision", timeout_sec=120)

    async def _op_quota(self, binary: str, **_) -> ToolResult:
        cmd = [binary, "quota", "show", "--output", "json"]
        return await self._run(self._with_key(cmd), operation="quota", timeout_sec=30)

    async def _op_voices(self, binary: str, **_) -> ToolResult:
        cmd = [binary, "speech", "voices", "--output", "json"]
        return await self._run(self._with_key(cmd), operation="voices", timeout_sec=30)

    async def _op_video_generate(self, binary: str, *, prompt: str,
                                 out: str = "", duration: int = 6,
                                 resolution: str = "768P",
                                 model: str = "",
                                 **_) -> ToolResult:
        cmd = [binary, "video", "generate", "--prompt", prompt, "--output", "json"]
        if model:
            cmd += ["--model", model]
        cmd += ["--duration", str(duration), "--resolution", resolution]
        if out:
            cmd += ["--out", out]
        return await self._run(self._with_key(cmd), operation="video_generate", timeout_sec=600)

    async def _op_video_get(self, binary: str, *, task_id: str, **_) -> ToolResult:
        cmd = [binary, "video", "task", "get", task_id, "--output", "json"]
        return await self._run(self._with_key(cmd), operation="video_get", timeout_sec=30)
