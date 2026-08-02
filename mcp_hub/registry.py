"""子 agent 注册表 —— 跨进程持久化 registry 与孤儿任务恢复（pid 轮询 + 日志捞结果）。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:  # noqa: BLE001
    psutil = None  # type: ignore[assignment]

from .config import HubSettings
from .runtimes.base import SubagentHandle, SubagentResult

# ---------- 注入的全局引用（由 server._init() 调 init_registry 装配，避免循环 import） ----------

_settings: HubSettings | None = None
_logger: logging.Logger | None = None
_subagents: dict[str, SubagentHandle] = {}  # task_id -> handle（与 server 共享同一 dict）
_subagent_results: dict[str, SubagentResult] = {}  # task_id -> result（与 server 共享同一 dict）
_orphan_pending: set[str] = set()  # 已恢复但还没起轮询 task 的孤儿 task_id


def init_registry(
    *,
    settings: HubSettings,
    logger: logging.Logger,
    subagents: dict[str, SubagentHandle],
    subagent_results: dict[str, SubagentResult],
) -> None:
    """注入 server 的全局引用（dict 传引用，双方读写同一张表）。"""
    global _settings, _logger, _subagents, _subagent_results
    _settings = settings
    _logger = logger
    _subagents = subagents
    _subagent_results = subagent_results


# ---------- 子 agent 注册表（跨进程持久化，孤儿任务恢复） ----------
#
# 每次 spawn 成功都把 task_id → {runtime, model, pid, workdir, log_file,
# started_at, caller, status} 追加写入 data/subagents_registry.json。
# hub 重启后 _init() 读 registry：pid 还活着的重新登记进 _subagents
# （process=None，只有 pid），起后台轮询等它自然退出后从日志文件捞结果；
# pid 已死的标记 dead。
#
# 跨进程文件锁：跟 queue/store.py 的 TaskStore 同款（msvcrt.locking /
# fcntl.flock），这里是最小复制，不依赖 TaskStore 实例。

try:
    import msvcrt  # type: ignore[import-not-found]
    _HAS_MSVCRT = True
except ImportError:
    _HAS_MSVCRT = False
try:
    import fcntl  # type: ignore[import-not-found]
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

_REGISTRY_LOCK_TIMEOUT_SEC = 5.0


def _registry_path() -> Path:
    """registry 放在 tasks.json 同目录（默认 ./data/）。"""
    assert _settings is not None
    queue_path = Path(_settings.hub_queue_path).expanduser().resolve()
    return queue_path.parent / "subagents_registry.json"


@contextlib.contextmanager
def _registry_lock():
    """registry 的跨进程文件锁（最小实现，语义同 TaskStore._file_lock）。"""
    lock_path = _registry_path().with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    fp = open(lock_path, "r+", encoding="utf-8")
    try:
        if _HAS_MSVCRT:
            deadline = time.monotonic() + _REGISTRY_LOCK_TIMEOUT_SEC
            while True:
                try:
                    fp.seek(0)
                    msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"获取 registry 文件锁超时: {lock_path}")
                    time.sleep(0.05)
        elif _HAS_FCNTL:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if _HAS_MSVCRT:
                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
            elif _HAS_FCNTL:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fp.close()


def _registry_load_unlocked() -> dict[str, Any]:
    path = _registry_path()
    if not path.exists():
        return {"subagents": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"subagents": {}}
    if not isinstance(data, dict) or not isinstance(data.get("subagents"), dict):
        return {"subagents": {}}
    return data


def _registry_save_unlocked(data: dict[str, Any]) -> None:
    """原子写：tmp → replace。"""
    path = _registry_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _registry_register(handle: SubagentHandle, caller: str, reasoning_effort: str = "") -> None:
    """spawn 成功后登记一条 running 记录。"""
    try:
        with _registry_lock():
            data = _registry_load_unlocked()
            data["subagents"][handle.task_id] = {
                "runtime": handle.runtime,
                "model": handle.model,
                "pid": handle.pid,
                "workdir": handle.workdir,
                "log_file": str(handle.output_file) if handle.output_file else "",
                "started_at": handle.started_at,
                "caller": caller,
                "status": "running",
                "prompt": handle.prompt,
                "reasoning_effort": reasoning_effort,
            }
            _registry_save_unlocked(data)
    except Exception:  # noqa: BLE001
        if _logger:
            _logger.exception("registry register failed id=%s", handle.task_id)


def _registry_mark(task_id: str, status: str) -> None:
    """把 registry 里的条目标成 done / dead。"""
    try:
        with _registry_lock():
            data = _registry_load_unlocked()
            entry = data["subagents"].get(task_id)
            if entry is not None:
                entry["status"] = status
                entry["finished_at"] = time.time()
                _registry_save_unlocked(data)
    except Exception:  # noqa: BLE001
        if _logger:
            _logger.exception("registry mark failed id=%s status=%s", task_id, status)


def _pid_alive(pid: int) -> bool:
    """检查 pid 是否存活。

    注意：Windows 上 **不能** 用 os.kill(pid, 0) —— CPython 的 Windows 实现
    对非 console-control 信号会直接 TerminateProcess，把进程真的杀掉。
    这里用 psutil.pid_exists（psutil 是软依赖），没有 psutil 时退到
    ctypes OpenProcess + GetExitCodeProcess（STILL_ACTIVE=259）。
    """
    if pid <= 0:
        return False
    if psutil is not None:
        try:
            return bool(psutil.pid_exists(pid))
        except Exception:  # noqa: BLE001
            return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = kernel32.OpenProcess(0x100000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _handle_alive(h: SubagentHandle) -> bool:
    """subagent_status 的 is_alive 判断，兼容 process=None（恢复出来的孤儿句柄）。"""
    if h.process is not None:
        return h.process.returncode is None
    if h.pid:
        return _pid_alive(h.pid)
    return False


def _kill_pid(pid: int) -> bool:
    """按 pid 杀进程（给恢复出来的孤儿句柄用，process=None 没法 proc.kill()）。"""
    try:
        if psutil is not None:
            proc = psutil.Process(pid)
            proc.kill()
            return True
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            h = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
            if not h:
                return False
            try:
                return bool(kernel32.TerminateProcess(h, 1))
            finally:
                kernel32.CloseHandle(h)
        os.kill(pid, 9)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _poll_orphan_subagent(handle: SubagentHandle) -> None:
    """恢复出来的"只有 pid"的孤儿任务：轮询等它退出，然后从日志文件捞结果。

    没法 proc.wait()（不是我们 fork 的），只能每 5s 查一次 pid 存活。
    进程死后把日志尾部作为 result 存进 _subagent_results，并标 registry done。
    """
    assert _logger is not None
    task_id = handle.task_id
    try:
        while handle.pid and _pid_alive(handle.pid):
            await asyncio.sleep(5)
        # 进程已退出：从日志文件读尾部当结果（无法还原 exit_code，按 0 处理）
        tail = ""
        if handle.output_file:
            try:
                text = handle.output_file.read_text(encoding="utf-8", errors="replace")
                tail = text[-4000:]
            except OSError:
                pass
        duration = time.time() - handle.started_at if handle.started_at else 0.0
        res = SubagentResult(
            runtime=handle.runtime,
            model=handle.model,
            task_id=task_id,
            exit_code=0,
            stdout=tail,
            stderr="",
            duration_sec=duration,
            summary=tail[-500:].strip(),
            error=None,
            prompt=handle.prompt,
        )
        _subagent_results[task_id] = res
        _registry_mark(task_id, "done")
        _logger.info(
            "orphan subagent id=%s (pid=%s) finished, result recovered from log tail",
            task_id, handle.pid,
        )
    except Exception:  # noqa: BLE001
        _logger.exception("orphan subagent poll failed id=%s", task_id)


def _ensure_orphan_pollers() -> None:
    """给 _orphan_pending 里的孤儿起轮询 task（需要 running loop）。

    _init() 可能在无线程循环的上下文里被调（FastMCP 启动钩子），
    恢复出来的孤儿先挂到 pending，等任何 async 工具被调时再补起轮询。
    """
    for task_id in list(_orphan_pending):
        handle = _subagents.get(task_id)
        if handle is None or task_id in _subagent_results:
            _orphan_pending.discard(task_id)
            continue
        try:
            asyncio.get_running_loop().create_task(_poll_orphan_subagent(handle))
        except RuntimeError:
            continue  # 还是没 loop，下次再试
        _orphan_pending.discard(task_id)


def _recover_orphan_subagents() -> None:
    """_init() 时从 registry 恢复上次 hub 进程留下的子 agent。"""
    assert _logger is not None
    try:
        with _registry_lock():
            data = _registry_load_unlocked()
    except Exception:  # noqa: BLE001
        _logger.exception("registry load failed, skip orphan recovery")
        return
    recovered = 0
    dead = 0
    for task_id, info in data.get("subagents", {}).items():
        if not isinstance(info, dict) or info.get("status") != "running":
            continue
        pid = info.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            _registry_mark(task_id, "dead")
            dead += 1
            continue
        if _pid_alive(pid):
            handle = SubagentHandle(
                pid=pid,
                runtime=str(info.get("runtime") or ""),
                model=str(info.get("model") or ""),
                task_id=task_id,
                workdir=str(info.get("workdir") or "."),
                started_at=float(info.get("started_at") or time.time()),
                process=None,  # 不是我们 fork 的，只有 pid
                output_file=Path(info["log_file"]) if info.get("log_file") else None,
                prompt="",
            )
            _subagents[task_id] = handle
            _orphan_pending.add(task_id)
            _ensure_orphan_pollers()
            recovered += 1
        else:
            _registry_mark(task_id, "dead")
            dead += 1
    if recovered or dead:
        _logger.info("orphan subagent recovery: %d recovered, %d dead", recovered, dead)
