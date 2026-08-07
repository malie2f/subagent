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
_runtimes: dict[str, Any] = {}  # runtime 名 -> adapter（与 server 共享同一 dict，孤儿补救用）


def init_registry(
    *,
    settings: HubSettings,
    logger: logging.Logger,
    subagents: dict[str, SubagentHandle],
    subagent_results: dict[str, SubagentResult],
    runtimes: dict[str, Any] | None = None,
) -> None:
    """注入 server 的全局引用（dict 传引用，双方读写同一张表）。"""
    global _settings, _logger, _subagents, _subagent_results, _runtimes
    _settings = settings
    _logger = logger
    _subagents = subagents
    _subagent_results = subagent_results
    if runtimes is not None:
        _runtimes = runtimes


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


def _registry_register(
    handle: SubagentHandle,
    caller: str,
    reasoning_effort: str = "",
    webhook: str = "",
    resumed_from: str = "",
) -> None:
    """spawn 成功后登记一条 running 记录。"""
    try:
        with _registry_lock():
            data = _registry_load_unlocked()
            entry = {
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
            if webhook:
                entry["webhook"] = webhook
            if resumed_from:
                entry["resumed_from"] = resumed_from
            data["subagents"][handle.task_id] = entry
            _registry_save_unlocked(data)
    except Exception:  # noqa: BLE001
        if _logger:
            _logger.exception("registry register failed id=%s", handle.task_id)


def _registry_mark(
    task_id: str,
    status: str,
    result: SubagentResult | None = None,
    stats: dict[str, Any] | None = None,
) -> None:
    """把 registry 里的条目标成终态（done/dead）。

    带 result 时把死亡现场一并落盘：exit_code/duration/summary/stderr 尾部/采样峰值。
    stats 可带 peak_rss_mb / peak_tokens（守候循环采的）。exit_code_source:
      real    = 从进程/结果拿到的真实退出码
      unknown = 拿不到（孤儿死透后 OpenProcess 失败等），不再谎报 0
    """
    try:
        with _registry_lock():
            data = _registry_load_unlocked()
            entry = data["subagents"].get(task_id)
            if entry is None:
                return
            entry["status"] = status
            entry["finished_at"] = time.time()
            if result is not None:
                entry["exit_code"] = result.exit_code
                entry["exit_code_source"] = "real" if result.exit_code is not None else "unknown"
                entry["duration_sec"] = round(result.duration_sec, 1)
                if result.session_id:
                    entry["session_id"] = result.session_id
                if result.summary:
                    entry["summary"] = result.summary[-1000:]
                if result.stderr:
                    entry["stderr_tail"] = result.stderr[-2000:]
                if result.error:
                    entry["error"] = str(result.error)[-500:]
            if stats:
                if stats.get("peak_rss_mb"):
                    entry["peak_rss_mb"] = round(stats["peak_rss_mb"], 1)
                if stats.get("peak_tokens"):
                    entry["peak_tokens"] = stats["peak_tokens"]
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
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
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


def _pid_exit_code(pid: int) -> int | None:
    """尽力拿已死进程的真实退出码（Windows：OpenProcess + GetExitCodeProcess）。

    进程刚死、内核对象还没回收时可用；拿不到（对象已回收/非 Windows）返回 None。
    STILL_ACTIVE(259) 说明还活着，也返回 None——调用方不应把它当退出码。
    """
    if sys.platform != "win32" or pid <= 0:
        return None
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return None
        return None if code.value == 259 else int(code.value)
    finally:
        kernel32.CloseHandle(h)


def _pid_create_time(pid: int) -> float | None:
    """pid 复用防护：记录进程创建时间，之后比对可发现 pid 被复用。"""
    if psutil is None or pid <= 0:
        return None
    try:
        return float(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001
        return None


def _read_log_tail(path: Path, nbytes: int = 65536) -> str:
    """读日志尾部（大文件不整读）。"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > nbytes:
                f.seek(-nbytes, 2)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


_STEP_FINISH_TOKENS_RE = None


def _extract_peak_tokens(text: str) -> int | None:
    """从日志文本里找最大的 step-finish tokens.total（opencode JSONL；兼容 step_finish）。

    用于 context 水位监控：舰队猝死都发生在 ~200k tokens 处，盯这个值比盯 RSS 对症。
    """
    global _STEP_FINISH_TOKENS_RE
    import re

    if _STEP_FINISH_TOKENS_RE is None:
        _STEP_FINISH_TOKENS_RE = re.compile(
            r'"type"\s*:\s*"step[-_]finish".{0,400}?"tokens"\s*:\s*\{[^{}]*"total"\s*:\s*(\d+)',
            re.DOTALL,
        )
    peak = None
    for m in _STEP_FINISH_TOKENS_RE.finditer(text):
        v = int(m.group(1))
        if peak is None or v > peak:
            peak = v
    return peak


async def _sample_process_stats(
    pid: int | None,
    output_file: Path | None,
    stats: dict[str, Any],
    interval: float = 5.0,
) -> None:
    """守候采样循环：RSS 峰值（psutil）+ 日志 tokens 峰值。跑到被取消为止。

    stats 就地更新：peak_rss_mb / peak_tokens。被取消（CancelledError）时正常返回。
    进循环先采一次（sleep 在末尾）——短命进程也有一条读数，不会全 null。
    """
    try:
        while True:
            if pid and psutil is not None:
                try:
                    rss_mb = psutil.Process(pid).memory_info().rss / 1024 / 1024
                    if rss_mb > stats.get("peak_rss_mb", 0):
                        stats["peak_rss_mb"] = rss_mb
                except Exception:  # noqa: BLE001
                    pass
            if output_file is not None:
                peak = _extract_peak_tokens(_read_log_tail(output_file))
                if peak is not None and peak > stats.get("peak_tokens", 0):
                    stats["peak_tokens"] = peak
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        return


async def _fire_subagent_webhook(task_id: str, event: str, extra: dict[str, Any]) -> None:
    """子 agent 终态事件推送：从 registry 条目取 webhook 地址 POST（没有就不发）。"""
    assert _logger is not None
    try:
        with _registry_lock():
            entry = _registry_load_unlocked()["subagents"].get(task_id) or {}
    except Exception:  # noqa: BLE001
        entry = {}
    url = entry.get("webhook") or ""
    if not url:
        return
    from .notify import post_webhook, valid_webhook_url

    if not valid_webhook_url(url):
        _logger.warning("subagent id=%s webhook 地址非法，跳过: %s", task_id, url)
        return
    body = {
        "event": event,
        "task_id": task_id,
        "runtime": entry.get("runtime"),
        "model": entry.get("model"),
        "pid": entry.get("pid"),
        "caller": entry.get("caller"),
        "resumed_from": entry.get("resumed_from"),
        "at": time.time(),
        **extra,
    }
    try:
        status = await post_webhook(url, body)
        _logger.info("subagent id=%s webhook %s -> %s", task_id, event, status)
    except Exception as e:  # noqa: BLE001
        _logger.warning("subagent id=%s webhook POST failed: %s", task_id, e)


async def _poll_orphan_subagent(handle: SubagentHandle) -> None:
    """恢复出来的"只有 pid"的孤儿任务：轮询等它退出，捞死亡现场。

    没法 proc.wait()（不是我们 fork 的），每 5s 查一次 pid 存活。
    死后尽力还原真相：真实 exit_code（OpenProcess，拿不到就标 unknown，不再
    谎报 0）+ RSS/tokens 采样峰值 + 日志尾部摘要 + adapter 补救解析（transcript/usage）。
    最后按需推 webhook。
    """
    assert _logger is not None
    task_id = handle.task_id
    stats: dict[str, Any] = {}
    create_time = _pid_create_time(handle.pid) if handle.pid else None
    try:
        while handle.pid and _pid_alive(handle.pid):
            await asyncio.sleep(5)
            # pid 复用防护：create_time 变了说明原进程已死、pid 被别人复用
            if create_time is not None:
                now_ct = _pid_create_time(handle.pid)
                if now_ct is not None and abs(now_ct - create_time) > 1.0:
                    break
            # 顺手采样（孤儿路不另起 task，直接在轮询里做）
            if handle.pid and psutil is not None:
                try:
                    rss_mb = psutil.Process(handle.pid).memory_info().rss / 1024 / 1024
                    if rss_mb > stats.get("peak_rss_mb", 0):
                        stats["peak_rss_mb"] = rss_mb
                except Exception:  # noqa: BLE001
                    pass
            if handle.output_file:
                peak = _extract_peak_tokens(_read_log_tail(handle.output_file))
                if peak is not None and peak > stats.get("peak_tokens", 0):
                    stats["peak_tokens"] = peak

        # 进程已退出：先抢真实退出码（越快拿到概率越大），再读日志尾
        exit_code = _pid_exit_code(handle.pid) if handle.pid else None
        tail = _read_log_tail(handle.output_file) if handle.output_file else ""
        peak = _extract_peak_tokens(tail)
        if peak is not None and peak > stats.get("peak_tokens", 0):
            stats["peak_tokens"] = peak
        # 拿不到退出码时的启发：日志尾部有终端 error 事件 → 按死处理
        log_has_error = '"type":"error"' in tail
        if exit_code is None:
            status = "dead" if log_has_error else "done"
        else:
            status = "done" if exit_code == 0 else "dead"
        duration = time.time() - handle.started_at if handle.started_at else 0.0
        res = SubagentResult(
            runtime=handle.runtime,
            model=handle.model,
            task_id=task_id,
            exit_code=exit_code,
            stdout=tail[-4000:],
            stderr="",
            duration_sec=duration,
            summary=tail[-500:].strip(),
            error=None,
            prompt=handle.prompt,
        )
        # adapter 补救：从完整日志解析 transcript/usage 落盘（opencode 等有解析器的 runtime）
        rt = _runtimes.get(handle.runtime)
        finish_orphan = getattr(rt, "finish_orphan", None) if rt else None
        if callable(finish_orphan):
            try:
                finish_orphan(handle, res)
            except Exception:  # noqa: BLE001
                _logger.exception("finish_orphan failed id=%s", task_id)
        _subagent_results[task_id] = res
        _registry_mark(task_id, status, result=res, stats=stats)
        _logger.info(
            "orphan subagent id=%s (pid=%s) finished: status=%s exit=%s peak_rss=%sMB peak_tokens=%s",
            task_id, handle.pid, status,
            exit_code if exit_code is not None else "unknown",
            stats.get("peak_rss_mb"), stats.get("peak_tokens"),
        )
        event = "subagent.done" if status == "done" else "subagent.failed"
        await _fire_subagent_webhook(task_id, event, {
            "status": status,
            "exit_code": exit_code,
            "exit_code_source": "real" if exit_code is not None else "unknown",
            "duration_sec": round(duration, 1),
            "summary": res.summary,
            "peak_rss_mb": stats.get("peak_rss_mb"),
            "peak_tokens": stats.get("peak_tokens"),
            "log_file": str(handle.output_file) if handle.output_file else "",
        })
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
                prompt=str(info.get("prompt") or ""),
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
