"""ClusterWorker —— 一个 worker 实例的数据 + 统计。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# worker 状态
WORKER_STATUS_STARTING = "starting"
WORKER_STATUS_IDLE = "idle"
WORKER_STATUS_BUSY = "busy"
WORKER_STATUS_OFFLINE = "offline"

WorkerStatus = str  # 类型别名


@dataclass
class WorkerStats:
    """单个 worker 的运行统计。"""

    processed: int = 0
    failed: int = 0
    total_duration_sec: float = 0.0
    last_task_id: str = ""
    last_task_at: float = 0.0
    last_error: str = ""

    @property
    def avg_duration_sec(self) -> float:
        if self.processed == 0:
            return 0.0
        return self.total_duration_sec / self.processed

    @property
    def success_rate(self) -> float:
        total = self.processed + self.failed
        if total == 0:
            return 0.0
        return self.processed / total

    def record_success(self, duration_sec: float, task_id: str) -> None:
        self.processed += 1
        self.total_duration_sec += duration_sec
        self.last_task_id = task_id
        self.last_task_at = time.time()
        self.last_error = ""

    def record_failure(self, task_id: str, error: str = "") -> None:
        self.failed += 1
        self.last_task_id = task_id
        self.last_task_at = time.time()
        if error:
            self.last_error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "failed": self.failed,
            "avg_duration_sec": round(self.avg_duration_sec, 2),
            "success_rate": round(self.success_rate, 3),
            "last_task_id": self.last_task_id,
            "last_task_at": self.last_task_at,
            "last_error": self.last_error,
        }


@dataclass
class ClusterWorker:
    """单个 worker 的元信息 + 运行时状态。"""

    worker_id: str                       # 全局唯一（如 opencode-1 / opencode-2 ...）
    runtime_name: str                    # "opencode" / "claude" / "kimi"
    model: str                           # "opencode-go/deepseek-v4-flash"
    concurrency: int = 1                 # 同时跑几个任务
    status: WorkerStatus = WORKER_STATUS_STARTING
    current_tasks: list[str] = field(default_factory=list)  # 正在跑的 task_id
    stats: WorkerStats = field(default_factory=WorkerStats)
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "runtime": self.runtime_name,
            "model": self.model,
            "concurrency": self.concurrency,
            "status": self.status,
            "current_tasks": list(self.current_tasks),
            "current_load": f"{len(self.current_tasks)}/{self.concurrency}",
            "started_at": self.started_at,
            "stats": self.stats.to_dict(),
        }

    def has_idle_slot(self) -> bool:
        return len(self.current_tasks) < self.concurrency
