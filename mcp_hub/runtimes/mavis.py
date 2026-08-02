"""Mavis runtime adapter —— 当前是 STUB，CLI 入口未就绪。

状态（2026-07-18 实测）：
    - Mavis 是 desktop app（Electron 套壳），不暴露独立 CLI 入口
    - C:\\Users\\Lenovo\\.mavis\\bin\\mavis.cmd 指向 D:\\MiniMax Code\\resources\\resources\\daemon\\cli.js
      （跟 minimax.cmd 同源，路径都不存在）
    - 调 `mavis --help` 报 Cannot find module

跟 minimax 的关系：
    - minimax 是 Mavis 的"前身" / 同源桌面 app
    - mavis desktop app 当前在 D:\\MiniMax Code\\ 装着的，叫"MiniMax Code"
      （公司两个产品，包装一样，daemon 路径也一样）
    - mavis CLI 入口跟 minimax CLI 入口是同一个 stub

跟 minimax 不同的点：
    - mavis 的"本身"就是我（这个 agent），可以参与 mcp-hub 调度
    - minimax 是另一种 agent 包装
    - 两者底层 daemon 路径相同，所以修一个就两个都能用

什么时候这个 stub 可以变实：
    1) Mavis desktop app 把 daemon 暴露到可访问路径（如 cli.js 真存在），且
    2) daemon 接受非交互调用（task / workdir / model）

届时只需把 _BINARY 和 _EXTRA_ARGS 改对，spawn 就能跑起来。

为什么还占个 stub：
    - mcp-hub 设计原则是 agent-agnostic，把 mavis 列入 REGISTRY
      跟 opencode / claude / kimi / codex 并列，符合"任何 coding agent 都能挂"
    - 占位也方便 Mavis daemon 修好后直接填空
    - 不会污染 detect_all() —— is_available() 返回 False 就不会被列出来
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

from .base import (
    RuntimeAdapter,
    SubagentHandle,
    SubagentResult,
    open_subagent_logs,
    wait_and_collect,
    write_transcript,
)


class MavisAdapter(RuntimeAdapter):
    name = "mavis"
    binary = "mavis"

    # 候选 binary 名（按顺序探测）
    _CANDIDATE_BINARIES = ("mavis",)

    def is_available(self) -> bool:
        """探测哪个候选 binary 存在 + 真的能跑通。

        2026-07-18 实测：wrapper 在 PATH 里，但指向的 daemon 路径不存在。
        所以这里加一个 5s 超时的健康探针，run 不动的就算不可用。
        """
        for b in self._CANDIDATE_BINARIES:
            binary = self._resolve_specific_cmd(b)
            if not binary:
                continue
            try:
                import subprocess

                r = subprocess.run(
                    [binary, "--help"],
                    timeout=5,
                    capture_output=True,
                    text=True,
                )
                # 健康标准：能在 5s 内退出 + 报的不是 module-not-found
                if "Cannot find module" not in (r.stderr or "") and "MODULE_NOT_FOUND" not in (r.stderr or ""):
                    return True
            except subprocess.TimeoutExpired:
                continue
            except Exception:  # noqa: BLE001
                continue
        return False

    def list_models(self) -> list[str]:
        """Mavis desktop app 内部用的是 mavis daemon，支持的模型由 daemon 决定。

        等 CLI 修好后再通过 `mavis models` 之类的子命令拿真实列表。
        现在返回几个常用的猜测值（MiniMax M 系列 + OpenAI 5.6 系列 + Claude 系列）。
        """
        return [
            "minimax-M3",
            "minimax-M2.7",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "claude-sonnet",
            "claude-opus",
            "deepseek-v4-flash",
            "kimi-k3",
        ]

    async def spawn(
        self,
        task_id: str,
        model: str,
        task: str,
        workdir: str,
        timeout_sec: int = 600,
        reasoning_effort: str = "",
    ) -> SubagentHandle:
        """STUB：当前 mavis CLI wrapper 路径坏了，spawn 出来的子进程会立刻挂。

        等桌面 app 修好 daemon 入口后，改这里：
            cmd = [<real_binary>, '--task', task, '--workdir', workdir, '--model', model]
        """
        binary = self._resolve_cmd()
        abs_workdir = str(Path(workdir).resolve())

        out_dir = Path(abs_workdir) / ".mcp-hub" / "subagents"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{task_id}.log"

        # 当前 stub 的命令形式 —— 故意复现 wrapper 的失败模式
        cmd = [binary, "--help"]  # 立刻退出，错误信息友好

        log_fp, err_file, err_fp = open_subagent_logs(out_file)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=abs_workdir,
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
            workdir=abs_workdir,
            started_at=time.time(),
            process=proc,
            output_file=out_file,
            log_fp=log_fp,
            err_file=err_file,
            err_fp=err_fp,
        )

    async def wait(self, handle: SubagentHandle, timeout_sec: int) -> SubagentResult:
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
            )

        started = time.time()
        stdout, stderr = await wait_and_collect(handle, proc, timeout_sec)

        return SubagentResult(
            runtime=handle.runtime,
            model=handle.model,
            task_id=handle.task_id,
            exit_code=proc.returncode or -1,
            stdout=stdout,
            stderr=stderr,
            duration_sec=time.time() - started,
            summary="",
            artifacts=[],
            error=(
                "mavis CLI 当前不可用：桌面 app 的 daemon 入口路径不存在。"
                "请等待 Mavis 修复 CLI wrapper，"
                "或编辑 mcp_hub/runtimes/mavis.py 把 _resolve_cmd() 改成实际能跑的入口。"
            ),
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

    def _resolve_cmd(self) -> str:
        for cand in self._CANDIDATE_BINARIES:
            p = self._resolve_specific_cmd(cand)
            if p:
                return p
        return self.binary

    def _resolve_specific_cmd(self, name: str) -> str | None:
        for ext in ("", ".cmd", ".exe"):
            p = shutil.which(name + ext)
            if p:
                return p
        return None

    def info(self) -> dict:
        d = super().info()
        d["status"] = "stub"
        d["note"] = (
            "Mavis 桌面 app 当前未暴露可用的 CLI 入口。"
            "本 adapter 保留为占位，等 CLI 修好后即可启用。"
        )
        return d
