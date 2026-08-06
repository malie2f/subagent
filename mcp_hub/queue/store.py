"""任务队列 —— 基于 JSON 文件的轻量持久化。

v2 升级：
  - 支持 acceptance（验收标准 + verifier + max_iterations）
  - 支持 webhook 通知
  - 支持 verify_task
  - 任务有 acceptance 时，complete 后自动进入 verifying 状态

v3 升级：
  - _load 解析失败时**不**丢数据（备份到 .corrupted-{ts}.json 后才返回空）
  - 跨进程文件锁（msvcrt.locking Windows，fcntl.flock POSIX）—— 防止 dashboard + cluster
    同时写 tasks.json 丢数据
  - _subscribers 加上限（防止内存泄漏）
  - subscriber queue 满时记录到 metric
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

# 跨进程文件锁：Windows 用 msvcrt，POSIX 用 fcntl
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


# ---------- 数据结构 ----------

TASK_STATUS_PENDING = "pending"
TASK_STATUS_CLAIMED = "claimed"
TASK_STATUS_DONE = "done"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_VERIFYING = "verifying"  # v2: 等 verifier 验证

# 终态集合
_TERMINAL = {TASK_STATUS_DONE, TASK_STATUS_FAILED}


@dataclass
class Task:
    task_id: str
    topic: str
    payload: str
    from_model: str
    for_model: str | None = None
    status: str = TASK_STATUS_PENDING
    result: str | None = None
    error: str | None = None
    claimed_by: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    claimed_at: float | None = None
    completed_at: float | None = None
    retries: int = 0
    max_retries: int = 3

    # ---- v2 新增字段 ----
    acceptance: dict[str, Any] = field(default_factory=dict)
    # 形如：
    # {
    #   "criteria": ["代码通过 pytest", "无 lint 错误"],
    #   "verifier": "claude",            # 谁来验（模型 alias / model id / worker name）
    #   "max_iterations": 2,             # 最多重做几次
    #   "auto_retry": true,              # 验不过是否自动重发
    # }
    webhook: str = ""                  # 状态变更时 POST 过去
    verify_history: list[dict[str, Any]] = field(default_factory=list)
    # 每条记录：{verifier, passed, score, issues, at}
    notify_history: list[dict[str, Any]] = field(default_factory=list)
    # 每条记录：{event, url, status_code, at}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        # 兼容老数据（没有新字段时补默认）
        return cls(
            task_id=d.get("task_id") or uuid.uuid4().hex[:12],
            topic=d.get("topic", ""),
            payload=d.get("payload", ""),
            from_model=d.get("from_model", "unknown"),
            for_model=d.get("for_model"),
            status=d.get("status", TASK_STATUS_PENDING),
            result=d.get("result"),
            error=d.get("error"),
            claimed_by=d.get("claimed_by"),
            metadata=d.get("metadata") or {},
            created_at=d.get("created_at", time.time()),
            claimed_at=d.get("claimed_at"),
            completed_at=d.get("completed_at"),
            retries=d.get("retries", 0),
            max_retries=d.get("max_retries", 3),
            acceptance=d.get("acceptance") or {},
            webhook=d.get("webhook", ""),
            verify_history=d.get("verify_history") or [],
            notify_history=d.get("notify_history") or [],
        )


# ---------- 存储层 ----------

class TaskStore:
    """文件版任务队列 + 通知 + 验证。

    并发模型：
    - 单进程内：asyncio.Lock（保证协程间互斥）
    - 跨进程：FileLock（msvcrt.locking / fcntl.flock，保证多进程间互斥）
    两者都用，避免 dashboard + cluster 同时改 tasks.json 丢数据。
    """

    # 单个 task 最多允许的 subscriber 数（防内存泄漏）
    MAX_SUBSCRIBERS_PER_TASK = 32
    # 整个 store 最多允许的 subscriber 总数
    MAX_TOTAL_SUBSCRIBERS = 256
    # 跨进程锁的最长等待时间（秒）
    FILE_LOCK_TIMEOUT_SEC = 5.0

    def __init__(self, path: str | Path, claim_timeout_sec: int = 600):
        self.path = Path(path)
        self.claim_timeout_sec = claim_timeout_sec
        self._lock = asyncio.Lock()
        self._log = logging.getLogger("mcp-hub.queue")
        # 确保文件存在
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._flush_unlocked({"tasks": []})

        # 通知订阅者（task_id -> [asyncio.Queue]）—— 用于 SSE / 实时推送
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def _acquire_file_lock(self, fp, blocking: bool = True) -> bool:
        """在已打开的 lock file 句柄上拿跨进程文件锁（advisory lock）。

        Windows msvcrt.locking 是字节级锁：LK_NBLCK 拿不到立刻抛 OSError，
        所以 blocking 模式用轮询实现，最长等 FILE_LOCK_TIMEOUT_SEC 秒。
        POSIX fcntl.flock 有原生 LOCK_EX 阻塞语义。
        """
        if _HAS_MSVCRT:
            deadline = time.monotonic() + self.FILE_LOCK_TIMEOUT_SEC
            while True:
                try:
                    fp.seek(0)
                    msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
                    return True
                except (OSError, IOError):
                    if not blocking or time.monotonic() >= deadline:
                        return False
                    time.sleep(0.05)
        elif _HAS_FCNTL:
            try:
                op = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
                fcntl.flock(fp.fileno(), op)
                return True
            except (OSError, IOError):
                return False
        return True  # 平台没有锁原语时退化为单进程锁

    def _release_file_lock(self, fp) -> None:
        if _HAS_MSVCRT:
            try:
                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        elif _HAS_FCNTL:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            except (OSError, IOError):
                pass

    @contextlib.contextmanager
    def _file_lock(self, blocking: bool = True):
        """跨进程文件锁 context manager。

        每次持锁都新打开 lock file 句柄、退出时释放锁并显式 close —— 不缓存句柄、
        不依赖 __del__。获取锁失败（超时 / 打不开 lock file）时抛错，
        调用方绝不无锁裸奔（无锁的 read-modify-write 是 tasks.json 被覆盖丢数据的根因）。
        """
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        # 创建 lock file（不存数据，仅作为 fcntl/msvcrt 的目标）
        lock_path.touch(exist_ok=True)
        try:
            fp = open(lock_path, "r+", encoding="utf-8")
        except OSError as e:
            raise OSError(f"无法打开任务队列锁文件 {lock_path}: {e}") from e
        try:
            if not self._acquire_file_lock(fp, blocking=blocking):
                raise TimeoutError(
                    f"获取任务队列文件锁超时（>{self.FILE_LOCK_TIMEOUT_SEC}s）: {lock_path}"
                )
            try:
                yield
            finally:
                self._release_file_lock(fp)
        finally:
            fp.close()

    # ---- 内部 ----

    def _load(self) -> dict[str, Any]:
        """读 tasks.json。容错：解析失败时**不**丢数据，备份损坏文件 + 返回空。

        之前版本会在 json.JSONDecodeError 时返回 {"tasks": []}，下一次 _flush 把所有
        任务覆盖为空 —— 这是数据神秘丢失的根因。
        现在：解析失败时把损坏文件备份成 {path}.corrupted-{ts}，返回空让 _flush 创建
        新文件但老数据已经在备份里，ops 可以手工恢复。

        跨进程锁：拿文件锁后再读，避免和 cluster 进程的写冲突。
        """
        if not self.path.exists():
            return {"tasks": []}
        with self._file_lock(blocking=True):
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, Any]:
        """_load 不加文件锁版本（已在外层持锁时用）。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as e:
            backup = self.path.with_suffix(
                f".corrupted-{int(time.time())}.json"
            )
            try:
                shutil.copy2(self.path, backup)
                self._log.error(
                    "tasks.json 解析失败，已备份到 %s：%s", backup, e
                )
            except OSError as copy_err:
                self._log.error(
                    "tasks.json 解析失败且备份也失败 (%s)：%s", copy_err, e
                )
            return {"tasks": []}
        if not isinstance(data, dict):
            self._log.error("tasks.json 顶层不是 dict：%r", type(data))
            return {"tasks": []}
        if "tasks" not in data or not isinstance(data["tasks"], list):
            data["tasks"] = []
        return data

    def _flush(self, data: dict[str, Any]) -> None:
        with self._file_lock(blocking=True):
            self._flush_unlocked(data)

    def _flush_unlocked(self, data: dict[str, Any]) -> None:
        """_flush 不加文件锁版本。原子写：tmp → rename。"""
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _read_modify_write(self, modifier):
        """跨进程安全的 read-modify-write：在文件锁里 load → modifier(data) → flush。

        modifier 是个 callable，签名 modifier(data) -> (return_value, new_data)。
        new_data 为 None 时不写盘（只读 / 提前失败的路径）。返回 return_value。
        """
        with self._file_lock(blocking=True):
            data = self._load_unlocked()
            result, new_data = modifier(data)
            if new_data is not None:
                self._flush_unlocked(new_data)
            return result

    def _is_terminal(self, status: str) -> bool:
        return status in _TERMINAL

    async def _fire_webhook(self, task: Task, event: str) -> None:
        """POST 通知 + push 给 SSE 订阅者。

        用 stdlib http.client 而不是 httpx：httpx 0.28+ 在 Windows + Python 3.14 上
        POST 127.0.0.1 的 HTTP/1.1 server 稳定返回 502（httpx 自身的 bug，跟 server 无关）。
        用 stdlib 跨平台行为稳定。
        """
        body = {
            "event": event,
            "task_id": task.task_id,
            "topic": task.topic,
            "status": task.status,
            "from_model": task.from_model,
            "for_model": task.for_model,
            "claimed_by": task.claimed_by,
            "result": task.result if event in ("task.done", "task.verified") else None,
            "error": task.error if event == "task.failed" else None,
            "verify_history": task.verify_history,
            "at": time.time(),
        }

        # 1) webhook —— stdlib http.client（见 notify.post_webhook 注释）
        if task.webhook and urlparse(task.webhook).scheme in ("http", "https"):
            try:
                from ..notify import post_webhook

                status = await post_webhook(task.webhook, body)
                task.notify_history.append(
                    {"event": event, "url": task.webhook, "status_code": status, "at": time.time()}
                )
            except Exception as e:  # noqa: BLE001
                self._log.warning("webhook POST failed: %s", e)
                task.notify_history.append(
                    {"event": event, "url": task.webhook, "error": str(e), "at": time.time()}
                )

        # 2) SSE 订阅者
        dropped = 0
        for q in self._subscribers.get(task.task_id, []):
            try:
                q.put_nowait(body)
            except asyncio.QueueFull:  # noqa: PERF203
                dropped += 1
        if dropped > 0:
            self._log.warning(
                "task %s 事件队列满，丢弃 %d 个（subscriber 没及时消费）",
                task.task_id, dropped,
            )

    # ---- 订阅（SSE 用）----

    def subscribe(self, task_id: str) -> asyncio.Queue:
        """订阅某个 task 的事件流，返回 asyncio.Queue。

        加上限：单 task 最多 MAX_SUBSCRIBERS_PER_TASK 个 subscriber，整个 store
        最多 MAX_TOTAL_SUBSCRIBERS。超过时丢弃最早加的（让客户端收不到 → 知道要重连）。
        """
        # 总数检查
        total = sum(len(qs) for qs in self._subscribers.values())
        if total >= self.MAX_TOTAL_SUBSCRIBERS:
            self._log.warning(
                "subscriber 总数已达上限 (%d)，拒绝新订阅 task=%s",
                total, task_id,
            )
            return None  # type: ignore[return-value]
        # 单 task 检查：超过上限时丢最早的
        subs = self._subscribers.setdefault(task_id, [])
        if len(subs) >= self.MAX_SUBSCRIBERS_PER_TASK:
            self._log.warning(
                "task %s 的 subscriber 已达上限 (%d)，丢弃最早的",
                task_id, len(subs),
            )
            try:
                old = subs.pop(0)
                # 给老 subscriber 发个关闭信号（None）
                try:
                    old.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            except IndexError:
                pass
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        subs.append(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(task_id)
        if subs and q in subs:
            subs.remove(q)
            # 清理空 list（避免 dict 越攒越大）
            if not subs:
                self._subscribers.pop(task_id, None)

    # ---- API ----

    async def set_sub_task_ids(self, task_id: str, sub_task_ids: list[str]) -> Task | None:
        """设置主任务的 sub-task 列表（sub-agent 派发关系）。

        写入到 metadata["sub_task_ids"]。dashboard 用这个画 sub-agent 树。
        """
        async with self._lock:
            def _modifier(data):
                for t in data["tasks"]:
                    if t["task_id"] == task_id:
                        t.setdefault("metadata", {})["sub_task_ids"] = list(sub_task_ids)
                        break
                return None, data
            self._read_modify_write(_modifier)
        return await self.status(task_id)

    async def get_sub_task_results(self, sub_task_ids: list[str]) -> dict[str, dict]:
        """批量拿 sub-task 的状态 + result。

        返回 {task_id: {status, result, error}}
        """
        async with self._lock:
            data = self._load()
        wanted = set(sub_task_ids)
        out = {}
        for t in data["tasks"]:
            if t["task_id"] in wanted:
                out[t["task_id"]] = {
                    "task_id": t["task_id"],
                    "status": t["status"],
                    "result": t.get("result") or "",
                    "error": t.get("error") or "",
                }
        return out

    # ---- API ----

    async def publish(
        self,
        topic: str,
        payload: str,
        from_model: str = "unknown",
        for_model: str | None = None,
        metadata: dict[str, Any] | None = None,
        max_retries: int = 3,
        acceptance: dict[str, Any] | None = None,
        webhook: str = "",
    ) -> Task:
        task = Task(
            task_id=uuid.uuid4().hex[:12],
            topic=topic,
            payload=payload,
            from_model=from_model,
            for_model=for_model,
            metadata=metadata or {},
            max_retries=max_retries,
            acceptance=acceptance or {},
            webhook=webhook,
        )
        async with self._lock:
            def _modifier(data):
                data["tasks"].append(task.to_dict())
                return None, data
            self._read_modify_write(_modifier)
        await self._fire_webhook(task, "task.published")
        return task

    async def claim(
        self,
        topic: str,
        worker: str,
        for_model: str | None = None,
    ) -> Task | None:
        async with self._lock:
            now = time.time()

            def _modifier(data):
                # 1) 重置超时 claimed
                for t in data["tasks"]:
                    if (
                        t["status"] == TASK_STATUS_CLAIMED
                        and t["claimed_at"] is not None
                        and now - t["claimed_at"] > self.claim_timeout_sec
                    ):
                        t["status"] = TASK_STATUS_PENDING
                        t["claimed_by"] = None
                        t["claimed_at"] = None
                        t["retries"] += 1

                # 2) 找可认领的
                for t in data["tasks"]:
                    if t["status"] != TASK_STATUS_PENDING:
                        continue
                    if t["topic"] != topic:
                        continue
                    if t["retries"] >= t["max_retries"]:
                        t["status"] = TASK_STATUS_FAILED
                        t["error"] = "max retries exceeded"
                        continue
                    if for_model and t["for_model"] and t["for_model"] != for_model:
                        continue
                    t["status"] = TASK_STATUS_CLAIMED
                    t["claimed_by"] = worker
                    t["claimed_at"] = now
                    return Task.from_dict(t), data
                return None, data

            return self._read_modify_write(_modifier)

    async def complete(
        self,
        task_id: str,
        worker: str,
        result: str,
        error: str | None = None,
    ) -> tuple[Task | None, str]:
        """完成任务。如果有 acceptance，状态变成 verifying；否则直接 done/failed。

        返回 (task, next_action)
            next_action: "done" | "verifying" | "failed" | "retry" | "unknown"
        """
        async with self._lock:
            def _modifier(data):
                for t in data["tasks"]:
                    if t["task_id"] != task_id:
                        continue
                    if t["claimed_by"] != worker:
                        return (None, "unauthorized"), None
                    if error:
                        t["status"] = TASK_STATUS_FAILED
                        t["error"] = error
                    else:
                        t["result"] = result
                        # 有 acceptance？进入 verifying
                        if t.get("acceptance") and t["acceptance"].get("criteria"):
                            t["status"] = TASK_STATUS_VERIFYING
                        else:
                            t["status"] = TASK_STATUS_DONE
                    t["completed_at"] = time.time()
                    return (Task.from_dict(t), t["status"]), data
                return (None, "unknown"), None

            task, outcome = self._read_modify_write(_modifier)

        if task is None:
            return None, outcome

        # 通知（写盘成功后才发，不在文件锁内做网络 IO）
        if outcome == TASK_STATUS_VERIFYING:
            await self._fire_webhook(task, "task.completed_awaiting_verify")
            return task, "verifying"
        elif outcome == TASK_STATUS_DONE:
            await self._fire_webhook(task, "task.done")
            return task, "done"
        else:
            await self._fire_webhook(task, "task.failed")
            return task, "failed"

    # ---- v2: 验收 ----

    async def verify(
        self,
        task_id: str,
        verifier: str,
        passed: bool,
        score: float = 0.0,
        issues: str = "",
    ) -> tuple[Task | None, str]:
        """写入 verifier 的验收结果。

        返回 (task, next_action)
            next_action: "verified" | "retry" | "failed" | "waiting" | "unknown"
        """
        async with self._lock:
            def _modifier(data):
                for t in data["tasks"]:
                    if t["task_id"] != task_id:
                        continue
                    if t["status"] != TASK_STATUS_VERIFYING:
                        return (None, "not_in_verifying"), None

                    # 记一笔历史
                    t["verify_history"].append(
                        {"verifier": verifier, "passed": passed, "score": score, "issues": issues, "at": time.time()}
                    )

                    if passed:
                        t["status"] = TASK_STATUS_DONE
                        action = "verified"
                    else:
                        # 失败：看要不要自动重试
                        accept = t.get("acceptance") or {}
                        auto = accept.get("auto_retry", True)
                        max_iter = accept.get("max_iterations", 2)
                        if auto and t["retries"] < max_iter:
                            # 重置为 pending，让它再被认领
                            t["status"] = TASK_STATUS_PENDING
                            t["claimed_by"] = None
                            t["claimed_at"] = None
                            t["retries"] += 1
                            # 把 verifier 的 issues 写进 metadata，给 worker 看
                            t.setdefault("metadata", {})["last_verify_issues"] = issues
                            action = "retry"
                        else:
                            t["status"] = TASK_STATUS_FAILED
                            t["error"] = f"verifier reject: {issues[:200]}"
                            action = "failed"

                    return (Task.from_dict(t), action), data
                return (None, "unknown"), None

            task, action = self._read_modify_write(_modifier)

        if task is None:
            return None, action

        # 通知（写盘成功后才发，不在文件锁内做网络 IO）
        if task.status == TASK_STATUS_DONE:
            await self._fire_webhook(task, "task.verified")
        elif task.status == TASK_STATUS_PENDING:
            await self._fire_webhook(task, "task.verify_failed_will_retry")
        else:
            await self._fire_webhook(task, "task.failed")

        return task, action

    # ---- 查询 ----

    async def status(self, task_id: str) -> Task | None:
        data = self._load()
        for t in data["tasks"]:
            if t["task_id"] == task_id:
                return Task.from_dict(t)
        return None

    async def stats(self) -> dict[str, Any]:
        data = self._load()
        by_topic: dict[str, dict[str, int]] = {}
        by_status: dict[str, int] = {}
        for t in data["tasks"]:
            by_status[t["status"]] = by_status.get(t["status"], 0) + 1
            tp = t["topic"]
            by_topic.setdefault(tp, {"pending": 0, "claimed": 0, "done": 0, "failed": 0, "verifying": 0})
            by_topic[tp][t["status"]] = by_topic[tp].get(t["status"], 0) + 1
        return {
            "total": len(data["tasks"]),
            "by_status": by_status,
            "by_topic": by_topic,
        }

    async def list_topics(self) -> list[str]:
        data = self._load()
        return sorted({t["topic"] for t in data["tasks"]})

    async def peek(self, topic: str, limit: int = 5) -> list[Task]:
        data = self._load()
        out: list[Task] = []
        for t in data["tasks"]:
            if t["topic"] == topic and t["status"] == TASK_STATUS_PENDING:
                out.append(Task.from_dict(t))
                if len(out) >= limit:
                    break
        return out

    async def list_claimed_or_done(
        self,
        topic: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 100,
    ) -> list[Task]:
        """列 claimed / verifying / done / failed 任务（已认领过的）。

        dashboard 拿 subagent 列表用：worker 认领过（claimed_by 非空）的任务。
        默认 status in {claimed, verifying, done, failed}，按 created_at 倒序。
        """
        if statuses is None:
            statuses = [
                TASK_STATUS_CLAIMED,
                TASK_STATUS_VERIFYING,
                TASK_STATUS_DONE,
                TASK_STATUS_FAILED,
            ]
        status_set = set(statuses)
        data = self._load()
        out: list[Task] = []
        # 倒序遍历（最新的先）
        for t in reversed(data["tasks"]):
            if t.get("claimed_by") is None:
                continue
            if t["status"] not in status_set:
                continue
            if topic and t["topic"] != topic:
                continue
            out.append(Task.from_dict(t))
            if len(out) >= limit:
                break
        return out
