"""Cluster —— 多 worker 集群。

设计：
  - ClusterWorker：一个 worker = 一个 runtime 适配器实例 + worker_id + 状态
  - ClusterPool：管理 N 个 worker 协程，从 TaskStore claim 任务，spawn runtime 跑
  - ClusterManager：对外接口，start/stop/scale/submit

每个 worker 是常驻协程，循环里 claim → spawn → wait → complete。
N 个 worker 并行抢同一个 topic，TaskStore 的 claim 原子性保证不重复。
"""

from __future__ import annotations

from .manager import ClusterManager, PoolSpec
from .pool import ClusterPool
from .worker import ClusterWorker, WorkerStats, WorkerStatus

__all__ = [
    "ClusterManager",
    "ClusterPool",
    "ClusterWorker",
    "PoolSpec",
    "WorkerStats",
    "WorkerStatus",
]
