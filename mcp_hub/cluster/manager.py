"""ClusterManager —— 集群管理器，对外接口（v3.2 多 pool 版）。

支持一个 manager 持有多个 pool，每个 pool 监听一个 topic + 用一个 runtime + 一个 model。
这样 dashboard 派活时按 `for_model` 自动落到对应 pool。

例：
  pools = [
    PoolSpec(name="deepseek", runtime="opencode", model="opencode-go/deepseek-v4-flash", topic="cluster.work", size=3),
    PoolSpec(name="codex",    runtime="codex",    model="codex/gpt-5.6-terra",          topic="cluster.work.codex", size=3),
  ]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from ..queue.store import TaskStore
from .pool import ClusterPool

if TYPE_CHECKING:
    from ..runtimes.base import RuntimeAdapter

_log = logging.getLogger("mcp-hub.cluster")


@dataclass
class PoolSpec:
    """单个 pool 的配置 + 运行时状态容器。

    字段：
      - name: pool 标识（dashboard 显示用）
      - enabled: 是否启用（False = 跳过这个 pool）
      - size: worker 数
      - runtime: 用哪个 runtime（opencode / claude / codex / kimi ...）
      - model: LLM model 名（runtime 内部用）
      - topic: 这个 pool 监听哪个 queue topic
      - workdir: worker fork 子 agent 时的工作目录
      - concurrency_per_worker: 每个 worker 同时跑几个任务
      - task_timeout_sec: 单任务超时
      - poll_interval_sec: 没任务时 sleep 多久再 poll

    兼容：构造时如果 dict 里有 'runtime_name' 也会被当作 'runtime' 收。
    """

    name: str
    enabled: bool
    size: int
    runtime: str
    model: str
    topic: str
    workdir: str
    concurrency_per_worker: int
    task_timeout_sec: int
    poll_interval_sec: float = 2.0
    pool: "ClusterPool | None" = field(default=None, init=False, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "PoolSpec":
        """从 dict 构造。兼容 runtime_name 别名。"""
        kw = dict(d)
        # 接受 runtime 或 runtime_name
        if "runtime" not in kw and "runtime_name" in kw:
            kw["runtime"] = kw["runtime_name"]
        # 删掉 PoolSpec 不认识的字段
        for k in ("runtime_name",):
            kw.pop(k, None)
        return cls(**kw)


class ClusterManager:
    """集群总入口（多 pool 版）。

    跟 runtimes/queue 完全解耦：复用现有 TaskStore 做任务分发，
    复用现有 RuntimeAdapter 做实际 agent 进程启动。
    """

    def __init__(
        self,
        *,
        specs: list[PoolSpec],
        store: TaskStore,
        runtimes: dict[str, "RuntimeAdapter"],
    ):
        self.specs: list[PoolSpec] = specs
        self.store = store
        self.runtimes = runtimes
        self.pools: list[ClusterPool] = []

    # ---------- 启停 ----------

    async def start(self) -> None:
        """启动所有 enabled pool。"""
        if not self.specs:
            _log.info("cluster 没有 pool 配置，跳过启动")
            return

        for spec in self.specs:
            if not spec.enabled:
                _log.info("pool '%s' 显式 enabled=false，跳过", spec.name)
                continue
            if spec.size <= 0:
                _log.info("pool '%s' size=0，跳过", spec.name)
                continue
            if spec.runtime not in self.runtimes:
                _log.warning(
                    "pool '%s' runtime '%s' 不可用（可用：%s），跳过",
                    spec.name, spec.runtime, list(self.runtimes.keys()),
                )
                continue
            pool = ClusterPool(self, spec)
            spec.pool = pool
            self.pools.append(pool)
            await pool.start()

        if not self.pools:
            _log.warning("cluster 没有任何可用 pool")

    async def stop(self) -> None:
        """停所有 pool。"""
        for p in self.pools:
            try:
                await p.stop()
            except Exception:  # noqa: BLE001
                _log.exception("pool '%s' 停止失败", p.spec.name)

    async def scale(self, name: str, n: int) -> tuple[int, int] | None:
        """按 name 缩放某个 pool。返回 (old, new) 或 None（没找到）。"""
        for spec in self.specs:
            if spec.name == name and spec.pool is not None:
                return await spec.pool.scale(n)
        return None

    # ---------- 路由查询 ----------

    def find_pool_by_for_model(self, for_model: str | None) -> "ClusterPool | None":
        """根据 for_model="runtime/model" 找对应 pool。"""
        if not for_model:
            return None
        for p in self.pools:
            if f"{p.spec.runtime}/{p.spec.model}" == for_model:
                return p
        # 再用 model 名字试一下（兼容只传 model 不带 runtime）
        for p in self.pools:
            if p.spec.model == for_model:
                return p
        return None

    def find_pool_by_topic(self, topic: str) -> "ClusterPool | None":
        """按 topic 找 pool。"""
        for p in self.pools:
            if p.spec.topic == topic:
                return p
        return None

    def default_pool(self) -> "ClusterPool | None":
        """默认 pool（dashboard 派活没指定 model 时用）。"""
        return self.pools[0] if self.pools else None

    # ---------- 兼容老 API：单个 cluster 的属性代理到第一个 pool（dashboard 旧调用用）----------

    @property
    def enabled(self) -> bool:
        """cluster 是否"配置上启用了"（有至少一个 enabled spec）—— 不依赖 start()。

        用法：
          - _run_cluster_only() 启动前用这个判断是否要跑
          - dashboard cluster() 显示 enabled 状态
          - cluster_stats / list_workers 也用它
        """
        return any(s.enabled for s in self.specs)

    @property
    def is_running(self) -> bool:
        """cluster 是不是已经 start 过了（至少一个 pool 起来了）。"""
        return len(self.pools) > 0

    @property
    def size(self) -> int:
        return sum(p.size() for p in self.pools)

    @property
    def runtime_name(self) -> str:
        if not self.pools:
            return ""
        return self.pools[0].spec.runtime

    @property
    def model(self) -> str:
        if not self.pools:
            return ""
        return self.pools[0].spec.model

    @property
    def topic(self) -> str:
        if not self.pools:
            return ""
        return self.pools[0].spec.topic

    @property
    def workdir(self) -> str:
        if not self.pools:
            return ""
        return self.pools[0].spec.workdir

    @property
    def concurrency_per_worker(self) -> int:
        if not self.pools:
            return 1
        return self.pools[0].spec.concurrency_per_worker

    @property
    def task_timeout_sec(self) -> int:
        if not self.pools:
            return 600
        return self.pools[0].spec.task_timeout_sec

    @property
    def poll_interval_sec(self) -> float:
        if not self.pools:
            return 2.0
        return self.pools[0].spec.poll_interval_sec

    # ---------- 状态导出 ----------

    def info(self) -> dict:
        """导出多 pool 状态。"""
        return {
            "enabled": self.enabled,
            "pool_count": len(self.pools),
            "pools": [p.info() for p in self.pools],
            "all_workers": [w.to_dict() for p in self.pools for w in p.workers],
        }

    def all_pools(self) -> Iterable[PoolSpec]:
        return iter(self.specs)
