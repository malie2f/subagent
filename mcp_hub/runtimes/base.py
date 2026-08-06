"""Runtime adapter 基类。"""

from __future__ import annotations

import abc
import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any


# ---------- Sub-agent 协议 ----------
# 主 agent 想要派 sub-agents 时，在最终答案末尾输出：
#   <<<SUBAGENT>>>
#   任务描述 1
#   ---
#   任务描述 2
#   ---
#   任务描述 3
#   <<<END>>>
# mcp-hub 解析后会把每条任务派到 cluster worker pool，
# 等所有 sub-agent 完成后回到主 agent 跑第二轮"汇总"。

SUBAGENT_BLOCK_START = "<<<SUBAGENT>>>"
SUBAGENT_BLOCK_END = "<<<END>>>"
SUBAGENT_DELIMITER = "\n---\n"
# 拼给主 agent 的 prompt 协议说明（追加在用户任务原文后）
SUBAGENT_PROTOCOL_HINT = """

---

## Sub-agent 协议（可选）

如果你需要并行做多个独立子任务（比如同时搜索、验证、汇总），可以派
sub-agent 帮你跑。每个 sub-agent 是一个独立的 CLI 任务，会有自己的日志和
transcript，你只需要在最终答案末尾用下面格式声明要派的子任务即可。

格式（必须出现在最终答案的末尾）：

```
[你的答案正文...]

<<<SUBAGENT>>>
子任务 1 的完整描述（独立可执行）
---
子任务 2 的完整描述（独立可执行）
---
子任务 3 的完整描述（独立可执行）
<<<END>>>
```

规则：
- <<<SUBAGENT>>> 和 <<<END>>> 必须各占一行
- 每条子任务用一行 `---` 分隔（不是 markdown 的 `---`）
- 子任务描述要**独立可执行**（不能依赖主 agent 的中间状态）
- 不要用 markdown 代码块包住 <<<SUBAGENT>>> 块
- 如果不需要 sub-agent，直接给答案就行，不要输出这个块
- mcp-hub 会等所有 sub-agent 完成后回到你这里跑第二轮"汇总"，
  第二轮时 sub-agent 的结果会作为上下文给你
"""


def parse_subagent_block(text: str) -> list[str]:
    """从主 agent 输出里抽 sub-agent 任务描述列表。

    返回 [] = 没要 sub-agent。返回 ["task 1", "task 2", ...] = 要派这些。

    容错：
      - 大小写不敏感
      - <<<SUBAGENT>>> 之前的文本当成主答案（丢弃，只用 sub-agents）
      - 前后空白/空行跳过
      - 单个 sub-agent 也支持（没 --- 分隔符也能 parse）
    """
    if not text:
        return []
    # 找 SUBAGENT 块
    pattern = re.compile(
        re.escape(SUBAGENT_BLOCK_START) + r"\s*\n(.*?)\n\s*" + re.escape(SUBAGENT_BLOCK_END),
        re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(text)
    if not m:
        return []
    body = m.group(1).strip()
    if not body:
        return []
    # 拆 --- 分隔
    parts = [p.strip() for p in re.split(r"^---\s*$|^\s*---\s*$", body, flags=re.MULTILINE)]
    parts = [p for p in parts if p]
    # 每个 sub-agent 描述去掉 markdown 代码块标记
    cleaned = []
    for p in parts:
        p = re.sub(r"^```[a-z]*\s*\n", "", p, flags=re.MULTILINE)
        p = re.sub(r"\n```\s*$", "", p)
        p = p.strip()
        if p:
            cleaned.append(p)
    return cleaned


def strip_subagent_block(text: str) -> str:
    """从主答案里把 SUBAGENT 块删掉（最终展示给用户时不要 raw block）。"""
    if not text or SUBAGENT_BLOCK_START not in text:
        return text
    pattern = re.compile(
        r"\n?\s*" + re.escape(SUBAGENT_BLOCK_START) + r".*?" + re.escape(SUBAGENT_BLOCK_END) + r"\s*\n?",
        re.DOTALL | re.IGNORECASE,
    )
    return pattern.sub("\n", text).rstrip()


@dataclass
class SubagentHandle:
    """子 agent 进程的句柄。"""

    pid: int | None
    runtime: str
    model: str
    task_id: str
    workdir: str
    started_at: float
    process: asyncio.subprocess.Process | None = None
    output_file: Path | None = None
    prompt: str = ""  # 实际发给 CLI 的 prompt（wait 时写 transcript 用）
    # v4：stdout/stderr 直接重定向到日志文件（不再走 PIPE，杜绝 pipe buffer 堵死）
    log_fp: IO[str] | None = None       # output_file 的写句柄（子进程 stdout），wait 结束后关闭
    err_file: Path | None = None        # stderr 重定向目标（{task_id}.err.log）
    err_fp: IO[str] | None = None       # err_file 的写句柄，wait 结束后关闭
    # Claude Code CLI 配置隔离用的临时 home 目录（仅 claude runtime 使用）
    home_dir: Path | None = None


@dataclass
class SubagentResult:
    """子 agent 跑完后的结果。

    新增字段（v3）：
      - prompt: 实际发给 CLI 的完整 prompt
      - transcript: 结构化事件流（list of dict），按时间顺序
        每个 event 至少有 type 字段。常见 type：
          - "prompt": 初始 prompt
          - "turn": 一轮 assistant/user 消息
          - "tool_call": agent 调用工具
          - "tool_result": 工具返回
          - "file_change": agent 改了文件（path, action, size）
          - "final": 最后输出
          - "error": 出错
    """

    runtime: str
    model: str
    task_id: str
    exit_code: int | None  # None = 拿不到真实退出码（孤儿死透后），不谎报 0
    stdout: str
    stderr: str
    duration_sec: float
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    # v3 新增
    prompt: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "model": self.model,
            "task_id": self.task_id,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_sec": self.duration_sec,
            "summary": self.summary,
            "artifacts": self.artifacts,
            "error": self.error,
            "prompt": self.prompt,
            "transcript": self.transcript,
        }


# ---------- Transcript 工具 ----------

def write_transcript(handle: SubagentHandle, prompt: str,
                    events: list[dict[str, Any]]) -> Path | None:
    """把 transcript 写到 .log 同目录的 .transcript.jsonl。

    格式：每行一个 JSON 对象。
    第一行固定是 {type: "prompt", ts, content: prompt}。
    后面每行是 events 里的一个元素（带 ts）。
    """
    if not handle.output_file:
        return None
    path = handle.output_file.with_suffix(".transcript.jsonl")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            # 1) prompt 行
            f.write(json.dumps(
                {"type": "prompt", "ts": handle.started_at, "content": prompt},
                ensure_ascii=False,
            ) + "\n")
            # 2) 事件流
            for ev in events:
                if "ts" not in ev:
                    ev = {**ev, "ts": handle.started_at}
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return path
    except OSError:
        return None


def read_transcript(path: Path | str) -> list[dict[str, Any]]:
    """读 .transcript.jsonl 成 list of dict。容错：解析失败的行跳过。"""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


# ---------- 子进程日志文件（stdout/stderr 直接重定向，不走 PIPE） ----------

_LOG_HEADER_RE = re.compile(r"^=== 实时日志 \([^)]*\) ===\n?")


def open_subagent_logs(out_file: Path) -> tuple[IO[str], Path, IO[str]]:
    """打开子进程 stdout/stderr 的重定向目标文件。

    返回 (log_fp, err_file, err_fp)：
      - log_fp  → out_file（{task_id}.log），子进程 stdout 指过来
      - err_fp  → err_file（{task_id}.err.log），子进程 stderr 指过来

    stdout/stderr 分开两个文件而不是合并：各 runtime 的 JSONL / 文本解析只认
    纯 stdout（_looks_like_jsonl 等探测看前几行），stderr 的噪音混进来会
    破坏解析，分开对现有解析逻辑零破坏。

    调用方（各 runtime 的 spawn）把返回的句柄存到 SubagentHandle.log_fp /
    err_fp，wait_and_collect() 负责关闭并读回内容。
    """
    out_file.parent.mkdir(parents=True, exist_ok=True)
    err_file = out_file.with_suffix(".err.log")
    log_fp = out_file.open("w", encoding="utf-8", errors="replace")
    log_fp.write(f"=== 实时日志 ({time.strftime('%Y-%m-%d %H:%M:%S')}) ===\n")
    log_fp.flush()
    err_fp = err_file.open("w", encoding="utf-8", errors="replace")
    return log_fp, err_file, err_fp


def close_subagent_logs(handle: SubagentHandle) -> None:
    """关闭 handle 上挂着的日志句柄（幂等）。"""
    for attr in ("log_fp", "err_fp"):
        fp = getattr(handle, attr, None)
        if fp is not None:
            try:
                fp.close()
            except OSError:
                pass
            setattr(handle, attr, None)


def _read_log(path: Path | None, strip_header: bool = False) -> str:
    """读回日志文件内容；文件不存在/读失败时返回空串。"""
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if strip_header:
        text = _LOG_HEADER_RE.sub("", text, count=1)
    return text


async def wait_and_collect(
    handle: SubagentHandle,
    proc: asyncio.subprocess.Process,
    timeout_sec: int,
) -> tuple[str, str]:
    """等子进程退出（超时 kill），关闭日志句柄，从日志文件读回 (stdout, stderr)。

    替代旧的 stream_subagent_output（PIPE 方案）：子进程输出由 OS 直接写进
    日志文件，hub 不需要 drain 任何管道，wait=False 的任务也不会再因为
    pipe buffer 写满而冻死；hub 重启也不会拉断管道杀死子进程。
    """
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
    finally:
        close_subagent_logs(handle)

    stdout = _read_log(handle.output_file, strip_header=True)
    stderr = _read_log(handle.err_file)
    if timed_out:
        stderr += f"\n[SYSTEM] timeout after {timeout_sec}s\n"
    return stdout, stderr


class RuntimeAdapter(abc.ABC):
    """所有 runtime adapter 的基类。"""

    name: str = "base"
    binary: str = ""
    # 超时后能否用同一 session 续跑（opencode / codex 覆写为 True）
    supports_resume: bool = False

    def __init__(self):
        pass

    @abc.abstractmethod
    def is_available(self) -> bool:
        """检测 CLI 是否安装。"""

    @abc.abstractmethod
    def list_models(self) -> list[str]:
        """列出此 runtime 支持的 model（简单文本列表）。"""

    @abc.abstractmethod
    async def spawn(
        self,
        task_id: str,
        model: str,
        task: str,
        workdir: str,
        timeout_sec: int = 600,
        reasoning_effort: str = "",
    ) -> SubagentHandle:
        """fork 子进程，返回 handle。

        reasoning_effort: 思考等级，可选 none/minimal/low/medium/high/xhigh/max（部分 runtime 支持，是否可用取决于模型），空串=不指定。
        """

    @abc.abstractmethod
    async def wait(self, handle: SubagentHandle, timeout_sec: int) -> SubagentResult:
        """阻塞等子进程完成（或超时），返回结果。"""

    @abc.abstractmethod
    async def cancel(self, handle: SubagentHandle) -> bool:
        """杀掉子进程。"""

    # ---- 超时断线续跑（默认不支持，opencode / codex 覆写） ----

    def extract_session_id(self, handle: SubagentHandle) -> str | None:
        """从 handle.output_file 的日志里提取 session id。不支持返回 None。"""
        return None

    async def resume_spawn(
        self,
        session_id: str,
        task_id: str,
        model: str,
        task: str,
        workdir: str,
        timeout_sec: int = 600,
    ) -> SubagentHandle:
        """基于已有 session 续跑（task 是调用方拼好的续跑提示词）。"""
        raise NotImplementedError(f"{self.name} 不支持续跑")

    # ---- 孤儿任务死后补救（默认不做，有日志解析能力的 runtime 覆写） ----

    def finish_orphan(self, handle: SubagentHandle, result: SubagentResult) -> None:
        """孤儿进程（hub 重启前 spawn 的）自然死亡后被调用。

        从 handle.output_file 的完整日志里解析 transcript/usage 落盘、
        回填 result.summary/artifacts 等——让死任务的 token 用量和现场不丢。
        默认什么都不做。
        """

    # ---- 通用工具 ----

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "binary": self.binary,
            "available": self.is_available(),
            "models": self.list_models() if self.is_available() else [],
        }

    def _find_binary(self) -> str | None:
        """在 PATH 中找命令的绝对路径。"""
        return shutil.which(self.binary)


# ---------- Windows npm .cmd shim 解包 ----------

_CMD_SHIM_EXE_RE = re.compile(r'"([^"]+\.exe)"\s+%\*', re.IGNORECASE)
_CMD_SHIM_NODE_RE = re.compile(r'(?:\bnode\b|"%_prog%")\s+"([^"]+)"\s+%\*', re.IGNORECASE)


def unwrap_cmd_shim(path: str) -> str:
    """把 exe 转发的 npm .cmd shim 解开成真实 exe 路径。

    npm shim 内容形如 `"%dp0%\\node_modules\\<pkg>\\bin\\xxx.exe" %*`。经 cmd.exe
    转发时，多行 prompt 的 argv 会在第一个换行处被切断——模型只收到任务书第一
    行（实测 6 个 input token，模型反问"请告诉我任务"）。直调真实 exe 走
    CreateProcess，引号内的换行可完整保留。非 shim 或解不开时原样返回。
    """
    if not path.lower().endswith(".cmd"):
        return path
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path
    m = _CMD_SHIM_EXE_RE.search(text)
    if not m:
        return path
    dp0 = str(Path(path).resolve().parent)
    exe = m.group(1).replace("%dp0%", dp0).replace("%~dp0%", dp0 + "\\")
    return exe if Path(exe).is_file() else path


def resolve_cmd_prefix(binary: str) -> list[str]:
    """解析 CLI 的启动命令前缀（解开 npm .cmd shim，绕开 cmd.exe）。

    返回值可直接展开进 argv：
      - exe 转发 shim（opencode/claude）→ [真实exe]
      - node 转发 shim（codebuddy/qoder/zcode）→ [node, 脚本路径]
      - 普通可执行文件 / 找不到 → [解析结果]
    """
    path = None
    for cand in (binary, binary + ".cmd", binary + ".exe"):
        path = shutil.which(cand)
        if path:
            break
    if not path:
        return [binary]
    if not path.lower().endswith(".cmd"):
        return [path]
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [path]
    dp0 = str(Path(path).resolve().parent)

    def _expand(p: str) -> str:
        return p.replace("%dp0%", dp0).replace("%~dp0%", dp0 + "\\")

    m = _CMD_SHIM_EXE_RE.search(text)
    if m:
        exe = _expand(m.group(1))
        return [exe] if Path(exe).is_file() else [path]
    m = _CMD_SHIM_NODE_RE.search(text)
    if m:
        script = _expand(m.group(1))
        node = str(Path(dp0) / "node.exe")
        if not Path(node).is_file():
            node = shutil.which("node") or "node"
        return [node, script] if Path(script).is_file() else [path]
    return [path]
