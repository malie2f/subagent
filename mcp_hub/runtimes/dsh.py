"""DeepSeek Harness runtime adapter —— 包装 `dsh --profile headless "<task>"`。

实测命令（dsh 0.1.0-rc.6，DeepSeek Harness 开发者预览版）：
    dsh --profile headless [--patch <yml>] <task>

关键：
    - headless 一次性任务：打印最终 assistant 消息到 stdout，然后退出
    - 无 --format json、无 --resume（一次性），supports_resume=False
    - 模型选择没有 CLI flag，靠 cordis patch：model 非默认时临时写一个
      patch yml 覆盖 agent-default-model，用 --patch 传进去
    - API key 走 ~/.dsh/.env 的 DEEPSEEK_API_KEY（credentials-local 插件读）
    - Windows 上 dsh 是 npm .cmd shim → resolve_cmd_prefix 解开成 [node, script]

模型名：裸名（deepseek-v4-pro / deepseek-v4-flash），也容忍 "dsh/" 前缀。
"""

from __future__ import annotations

import asyncio
import re
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

# 内置模型（dsh-base 的 deepseek-official provider 自带）
_MODELS = ["deepseek-v4-pro", "deepseek-v4-flash"]
# 与 ~/.dsh/profiles/headless/cordis.patch.yml 的默认模型保持一致
_DEFAULT_MODEL = "deepseek-v4-pro"


class DshAdapter(RuntimeAdapter):
    name = "dsh"
    binary = "dsh"
    supports_resume = False

    def is_available(self) -> bool:
        return resolve_cmd_prefix(self.binary) != [self.binary]

    def list_models(self) -> list[str]:
        return list(_MODELS)

    async def spawn(
        self,
        task_id: str,
        model: str,
        task: str,
        workdir: str,
        timeout_sec: int = 600,
        reasoning_effort: str = "",
    ) -> SubagentHandle:
        """fork 一个 `dsh --profile headless <task>` 进程。"""
        out_dir = Path(workdir) / ".mcp-hub" / "subagents"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{task_id}.log"

        cmd = self._build_cmd(model, task, task_id, out_dir)

        log_fp, err_file, err_fp = open_subagent_logs(out_file)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workdir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_fp,
                stderr=err_fp,
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
        """等 dsh 跑完；stdout 就是最终消息文本。"""
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
        clean_stdout = _clean_ansi(stdout)
        clean_stderr = _clean_ansi(stderr)

        exit_code = proc.returncode or 0
        summary = clean_stdout.strip()
        transcript: list[dict[str, Any]] = []
        if summary:
            transcript.append({"type": "turn", "role": "assistant", "content": summary})
            transcript.append({"type": "final", "content": summary, "stop_reason": "stop" if exit_code == 0 else "error"})
        if exit_code != 0 and clean_stderr:
            transcript.append({"type": "error", "message": clean_stderr[-2000:]})
        if handle.output_file:
            write_transcript(handle, handle.prompt, transcript)

        return SubagentResult(
            runtime=handle.runtime,
            model=handle.model,
            task_id=handle.task_id,
            exit_code=exit_code,
            stdout=clean_stdout,
            stderr=clean_stderr,
            duration_sec=time.time() - started,
            summary=summary,
            artifacts=[],
            error=clean_stderr if exit_code != 0 else None,
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

    # ---- 内部工具 ----

    def _build_cmd(self, model: str, task: str, task_id: str, out_dir: Path) -> list[str]:
        cmd = resolve_cmd_prefix(self.binary) + ["--profile", "headless"]
        # 非默认模型：临时 patch yml 覆盖 agent-default-model
        m = model.split("/", 1)[1] if model.startswith("dsh/") else model
        if m and m != _DEFAULT_MODEL:
            patch = out_dir / f"{task_id}.patch.yml"
            patch.write_text(
                "- id: agent-default-model\n"
                "  config:\n"
                "    provider: deepseek-official\n"
                f"    model: {m}\n",
                encoding="utf-8",
            )
            cmd += ["--patch", str(patch)]
        cmd.append(task)
        return cmd


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)
