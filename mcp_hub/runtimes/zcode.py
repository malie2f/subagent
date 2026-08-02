"""ZCode runtime adapter —— 包装 `zcode -p "..." --json --mode yolo`。

注意：
  - ZCode CLI 0.15.x 的 headless JSON 输出格式尚未稳定文档化。
  - 本 adapter 按 stdout 文本聚合作为 summary，后续可按实际输出结构增强。
  - 需要用户先在 `~/.zcode/cli/config.json` 里配置好 provider 和默认模型，否则
    `zcode -p` 会报 "Model config is missing"。
  - ZCode CLI 目前没有稳定的 `--model` 选项，因此本 adapter 会读取用户配置，
    为每次 spawn 生成一个隔离的临时 HOME 目录，并在其中写入只包含目标模型的
    `~/.zcode/cli/config.json`，从而实现按 model 切换；如果目标模型找不到，
    则回退到用户配置的默认模型。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .base import (
    RuntimeAdapter,
    SubagentHandle,
    SubagentResult,
    open_subagent_logs,
    resolve_cmd_prefix,
    wait_and_collect,
    write_transcript,
)


class ZcodeAdapter(RuntimeAdapter):
    name = "zcode"
    binary = "zcode"

    def __init__(self) -> None:
        self._user_config: dict[str, Any] | None = None
        self._user_config_path = Path.home() / ".zcode" / "cli" / "config.json"

    def _load_config(self) -> dict[str, Any]:
        """读取用户 zcode CLI 配置，失败返回空字典。"""
        if self._user_config is None:
            try:
                self._user_config = json.loads(
                    self._user_config_path.read_text(encoding="utf-8")
                )
            except Exception:  # noqa: BLE001
                self._user_config = {}
        return self._user_config

    def _enabled_model_refs(self) -> list[tuple[str, str]]:
        """扫描配置中启用的 provider，返回 (provider_id, model_id) 列表。"""
        cfg = self._load_config()
        refs: list[tuple[str, str]] = []
        for provider_id, provider in (cfg.get("provider") or {}).items():
            if not provider.get("enabled", True):
                continue
            for model_id in (provider.get("models") or {}).keys():
                refs.append((provider_id, model_id))
        return refs

    def is_available(self) -> bool:
        """二进制存在即可。"""
        return shutil.which(self.binary) is not None

    def list_models(self) -> list[str]:
        """返回用户配置中启用的 provider 提供的模型列表。"""
        return [model_id for _, model_id in self._enabled_model_refs()]

    def info(self) -> dict[str, Any]:
        """覆盖 info，附加 model 列表。"""
        return {
            "name": self.name,
            "binary": self.binary,
            "available": self.is_available(),
            "models": self.list_models(),
        }

    def _resolve_model_ref(self, model: str) -> str | None:
        """把 model 参数解析成 zcode 能识别的 'provider_id/model_id'。"""
        refs = self._enabled_model_refs()
        if not refs:
            return None

        # 已经是完整 ref
        if "/" in model:
            for provider_id, model_id in refs:
                if f"{provider_id}/{model_id}" == model:
                    return model
            return None

        # 按 model_id 匹配
        for provider_id, model_id in refs:
            if model_id == model:
                return f"{provider_id}/{model_id}"

        # 回退：使用配置里的默认 model（可能是字符串或对象）
        cfg = self._load_config()
        default = cfg.get("model")
        if isinstance(default, str):
            return default
        if isinstance(default, dict):
            main = default.get("main") or {}
            provider_id = main.get("provider")
            model_id = main.get("model")
            if provider_id and model_id:
                return f"{provider_id}/{model_id}"
        return None

    def _prepare_env_with_model(
        self, model: str, workdir: Path
    ) -> tuple[dict[str, str], Path] | None:
        """生成临时 HOME，写入只含目标模型的 config.json，返回 env 和临时目录。"""
        model_ref = self._resolve_model_ref(model)
        if model_ref is None:
            return None

        cfg = self._load_config()
        if not cfg:
            return None

        # 创建临时 HOME 目录
        home_dir = workdir / ".mcp-hub" / "subagents" / f"{self.name}-home"
        home_dir.mkdir(parents=True, exist_ok=True)
        cli_dir = home_dir / ".zcode" / "cli"
        cli_dir.mkdir(parents=True, exist_ok=True)

        # 复制配置，并把 model 设为想要的 ref
        spawn_cfg = json.loads(json.dumps(cfg))
        spawn_cfg["model"] = model_ref

        # 只保留与目标模型相关的 provider，避免未授权 provider 触发校验错误
        target_provider_id = model_ref.split("/", 1)[0]
        providers = spawn_cfg.get("provider") or {}
        spawn_cfg["provider"] = {
            pid: pdata
            for pid, pdata in providers.items()
            if pid == target_provider_id
        }

        (cli_dir / "config.json").write_text(
            json.dumps(spawn_cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        env = {**os.environ, "HOME": str(home_dir), "USERPROFILE": str(home_dir)}
        return env, home_dir

    async def spawn(
        self,
        task_id: str,
        model: str,
        task: str,
        workdir: str,
        timeout_sec: int = 600,
        reasoning_effort: str = "",
        mode: str = "",
    ) -> SubagentHandle:
        """fork 一个 `zcode -p` headless 进程。

        mode: yolo / plan / build / edit。空字符串表示用环境变量
              `ZCODE_DEFAULT_MODE` 或回退到 `yolo`。
              注意：`plan` 模式只输出计划，不会执行文件编辑/工具调用。
        """
        out_dir = Path(workdir) / ".mcp-hub" / "subagents"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{task_id}.log"

        # --prompt 和 -p 在某些版本里解析有坑，用 --prompt 并把 task 放在末尾
        cmd_prefix = resolve_cmd_prefix(self.binary)  # 解开 npm .cmd shim
        effective_mode = mode or os.environ.get("ZCODE_DEFAULT_MODE", "yolo")
        cmd = [
            *cmd_prefix,
            "--prompt", task,
            "--json",
            "--mode", effective_mode,
            "--cwd", str(Path(workdir).resolve()),
        ]

        env = os.environ
        env_note = ""
        prepared = self._prepare_env_with_model(model, out_dir)
        if prepared:
            env, home_dir = prepared
            env_note = f" (isolated HOME={home_dir})"

        # stdout/stderr 直接重定向到日志文件（不走 PIPE）。
        # stderr 单独进 .err.log：summary 直接吃整个 stdout，不能被噪音污染。
        log_fp, err_file, err_fp = open_subagent_logs(out_file)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workdir,
                stdin=asyncio.subprocess.DEVNULL,   # 防止 CLI 意外读 stdin 等权限确认而挂起
                stdout=log_fp,
                stderr=err_fp,
                env=env,
            )
        except BaseException:
            log_fp.close()
            err_fp.close()
            raise

        return SubagentHandle(
            pid=proc.pid,
            runtime=self.name,
            model=model,
            task_id=task_id,
            workdir=workdir,
            started_at=time.time(),
            process=proc,
            output_file=out_file,
            prompt=task,
            log_fp=log_fp,
            err_file=err_file,
            err_fp=err_fp,
        )

    async def wait(self, handle: SubagentHandle, timeout_sec: int) -> SubagentResult:
        """等待 zcode 进程结束，输出作为文本聚合。"""
        proc = handle.process
        if proc is None:
            return SubagentResult(
                runtime=handle.runtime,
                model=handle.model,
                task_id=handle.task_id,
                exit_code=-1,
                stdout="",
                stderr="",
                duration_sec=0,
                error="no process",
                prompt=handle.prompt,
            )

        started = time.time()
        stdout, stderr = await wait_and_collect(handle, proc, timeout_sec)

        exit_code = proc.returncode or 0
        # 目前把 stdout 整体当作 summary；如果 stdout 是 JSONL，可后续解析
        summary = stdout.strip()
        if len(summary) > 4000:
            summary = summary[:4000] + "\n...（已截断）"

        transcript: list[dict[str, Any]] = []
        if stdout.strip():
            transcript.append({"type": "final", "content": summary})
        if exit_code != 0 and stderr.strip():
            transcript.append({"type": "error", "message": stderr[-2000:]})

        if handle.output_file:
            write_transcript(handle, handle.prompt, transcript)

        return SubagentResult(
            runtime=handle.runtime,
            model=handle.model,
            task_id=handle.task_id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_sec=time.time() - started,
            summary=summary,
            artifacts=[],
            error=stderr if exit_code != 0 else None,
            prompt=handle.prompt,
            transcript=transcript,
        )

    async def cancel(self, handle: SubagentHandle) -> bool:
        proc = handle.process
        if proc is None or proc.returncode is not None:
            return False
        try:
            proc.kill()
            await proc.wait()
            return True
        except Exception:  # noqa: BLE001
            return False
