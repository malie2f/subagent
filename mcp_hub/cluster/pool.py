"""ClusterPool —— worker 池 + worker 协程循环。"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import TYPE_CHECKING

from ..queue.store import TaskStore
from ..runtimes.base import (
    RuntimeAdapter,
    SUBAGENT_PROTOCOL_HINT,
    parse_subagent_block,
    strip_subagent_block,
)
from .worker import (
    ClusterWorker,
    WORKER_STATUS_BUSY,
    WORKER_STATUS_IDLE,
    WORKER_STATUS_OFFLINE,
    WORKER_STATUS_STARTING,
)

if TYPE_CHECKING:
    from .manager import ClusterManager

_log = logging.getLogger("mcp-hub.cluster")


class ClusterPool:
    """worker 池：N 个 worker 协程共享一个 TaskStore。

    每个 worker 跑 worker_loop：
      while running:
        if 没空闲 slot → sleep
        task = store.claim(topic, worker=worker_id)
        if 没任务 → sleep poll_interval
        else:
          handle = runtime.spawn(...)
          result = runtime.wait(...)
          store.complete(...)
    """

    def __init__(self, manager: "ClusterManager", spec):
        self.manager = manager
        self.spec = spec
        self.workers: list[ClusterWorker] = []
        self._worker_tasks: list[asyncio.Task] = []
        self._stopping = False

    def size(self) -> int:
        return len(self.workers)

    async def start(self) -> None:
        if not self.spec.enabled:
            _log.info("pool '%s' 未启用，跳过启动", self.spec.name)
            return
        if self._worker_tasks:
            _log.warning("pool '%s' 已经在跑了", self.spec.name)
            return

        # 验证 runtime 可用
        if self.spec.runtime not in self.manager.runtimes:
            raise RuntimeError(
                f"pool '{self.spec.name}' runtime '{self.spec.runtime}' 不可用；可选：{list(self.manager.runtimes.keys())}"
            )

        _log.info(
            "启动 pool '%s'：%d workers，runtime=%s，model=%s，topic=%s",
            self.spec.name, self.spec.size, self.spec.runtime,
            self.spec.model, self.spec.topic,
        )

        for i in range(self.spec.size):
            worker_id = f"{self.spec.name}-{i + 1}"
            w = ClusterWorker(
                worker_id=worker_id,
                runtime_name=self.spec.runtime,
                model=self.spec.model,
                concurrency=self.spec.concurrency_per_worker,
            )
            self.workers.append(w)
            t = asyncio.create_task(self._worker_loop(w), name=f"cluster-pool-{self.spec.name}-worker-{worker_id}")
            self._worker_tasks.append(t)

    async def stop(self, timeout_sec: float = 10.0) -> None:
        if self._stopping:
            return
        self._stopping = True
        _log.info("停止 cluster（%d workers）", len(self._worker_tasks))
        for t in self._worker_tasks:
            t.cancel()
        # 等所有 worker 协程退出
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        # 标记 offline
        for w in self.workers:
            w.status = WORKER_STATUS_OFFLINE
        self._worker_tasks = []

    async def scale(self, new_size: int) -> tuple[int, int]:
        """扩缩 worker 数。返回 (old_size, new_size)。"""
        old_size = len(self.workers)
        if new_size == old_size:
            return (old_size, new_size)

        if new_size < old_size:
            # 缩：把多余的 worker 标记 offline
            to_remove = self.workers[new_size:]
            for w in to_remove:
                w.status = WORKER_STATUS_OFFLINE
            self.workers = self.workers[:new_size]
            # 取消对应的协程
            extra_tasks = self._worker_tasks[new_size:]
            for t in extra_tasks:
                t.cancel()
            await asyncio.gather(*extra_tasks, return_exceptions=True)
            self._worker_tasks = self._worker_tasks[:new_size]
            _log.info("cluster 缩容 %d → %d", old_size, new_size)
            return (old_size, new_size)

        # 扩：加 worker
        for i in range(old_size, new_size):
            worker_id = f"{self.spec.name}-{i + 1}"
            w = ClusterWorker(
                worker_id=worker_id,
                runtime_name=self.spec.runtime,
                model=self.spec.model,
                concurrency=self.spec.concurrency_per_worker,
            )
            self.workers.append(w)
            t = asyncio.create_task(self._worker_loop(w), name=f"cluster-pool-{self.spec.name}-worker-{worker_id}")
            self._worker_tasks.append(t)
        _log.info("cluster 扩容 %d → %d", old_size, new_size)
        return (old_size, new_size)

    # ---------- worker loop ----------

    async def _worker_loop(self, worker: ClusterWorker) -> None:
        """单个 worker 的主循环。"""
        store = self.manager.store
        runtime: RuntimeAdapter = self.manager.runtimes[worker.runtime_name]

        worker.status = WORKER_STATUS_IDLE
        _log.info("worker %s 上线", worker.worker_id)

        try:
            while not self._stopping:
                # 没有空闲 slot 就等
                if not worker.has_idle_slot():
                    await asyncio.sleep(0.5)
                    continue

                # claim 一个任务（短超时，避免长时间 block 关闭）
                try:
                    task = await asyncio.wait_for(
                        store.claim(
                            topic=self.spec.topic,
                            worker=worker.worker_id,
                            # for_model 拼成 runtime/model 跟 dashboard 派活时一致
                            # （否则 worker 永远 claim 不到任务）
                            for_model=f"{worker.runtime_name}/{worker.model}",
                        ),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    _log.exception("worker %s claim 失败：%s", worker.worker_id, e)
                    await asyncio.sleep(self.spec.poll_interval_sec)
                    continue

                if task is None:
                    # 没任务，歇一会
                    await asyncio.sleep(self.spec.poll_interval_sec)
                    continue

                from mcp_hub.config import load_settings
                from mcp_hub.connections import runtime_connected
                if load_settings().hub_require_runtime_connection and not runtime_connected(worker.runtime_name):
                    err = (
                        f"runtime '{worker.runtime_name}' 未在仪表盘连接，"
                        "cluster worker 拒绝执行。打开 http://127.0.0.1:8766 「连接」页。"
                    )
                    _log.warning("worker %s 跳过 task=%s：%s", worker.worker_id, task.task_id, err)
                    try:
                        await store.complete(
                            task_id=task.task_id,
                            worker=worker.worker_id,
                            result="",
                            error=err,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    await asyncio.sleep(self.spec.poll_interval_sec)
                    continue

                # 跑这个任务
                await self._run_one(worker, runtime, task)
        except asyncio.CancelledError:
            _log.info("worker %s 被取消", worker.worker_id)
            worker.status = WORKER_STATUS_OFFLINE
            raise
        except Exception:  # noqa: BLE001
            _log.exception("worker %s 协程异常退出", worker.worker_id)
            worker.status = WORKER_STATUS_OFFLINE

    async def _run_one(self, worker: ClusterWorker, runtime: RuntimeAdapter, task) -> None:
        """跑一个任务：spawn → wait → complete。

        v3.1 升级：支持 sub-agent 协议
          1. 主 agent 第一轮返回时扫 <<<SUBAGENT>>> block
          2. 如果有 sub-agents：派 N 个 sub-tasks 到 queue，等所有完成
          3. 用 sub-tasks 的结果做 context 跑第二轮"汇总" prompt
          4. 第二轮返回的 stdout 作为最终 result 写回主任务
        """
        store = self.manager.store
        worker.current_tasks.append(task.task_id)
        worker.status = WORKER_STATUS_BUSY
        started = time.time()

        try:
            # 任务 metadata 里可带 timeout_sec（长任务模式），优先于 pool 默认
            task_timeout = int(
                (task.metadata or {}).get("timeout_sec") or self.spec.task_timeout_sec
            )
            # 第一轮：原 prompt + sub-agent 协议说明
            first_prompt = task.payload + SUBAGENT_PROTOCOL_HINT
            handle = await runtime.spawn(
                task_id=task.task_id,
                model=worker.model,
                task=first_prompt,
                workdir=self.spec.workdir,
                timeout_sec=task_timeout,
            )
            _log.info(
                "worker=%s task=%s 第一轮 spawned pid=%s",
                worker.worker_id, task.task_id, handle.pid,
            )

            result = await runtime.wait(handle, task_timeout)
            if result.exit_code != 0:
                # 第一轮失败，不走 sub-agent 流程
                err = result.error or f"exit_code={result.exit_code} stderr={(result.stderr or '')[:500]}"
                await store.complete(
                    task_id=task.task_id,
                    worker=worker.worker_id,
                    result=result.stdout[-2000:] if result.stdout else "",
                    error=err,
                )
                worker.stats.record_failure(task.task_id, err[:200])
                return

            # 扫 <<<SUBAGENT>>> block
            sub_tasks = parse_subagent_block(result.stdout or "")
            if not sub_tasks:
                # 没要 sub-agent：原流程
                await self._finish_task(
                    store, worker, task, result.stdout or "",
                    started, raw_output=result.stdout or "",
                )
                return

            # 有 sub-agent：派 + 等 + 汇总
            _log.info(
                "worker=%s task=%s LLM 要派 %d 个 sub-agents: %s",
                worker.worker_id, task.task_id, len(sub_tasks),
                [t[:50] for t in sub_tasks],
            )
            sub_task_ids = []
            for sub_desc in sub_tasks:
                sub = await store.publish(
                    topic=task.topic,
                    payload=sub_desc,
                    from_model=f"{task.task_id}:subagent",
                    for_model=f"{worker.runtime_name}/{worker.model}",
                )
                sub_task_ids.append(sub.task_id)
            # 写 sub_task_ids 到主任务 metadata
            await store.set_sub_task_ids(task.task_id, sub_task_ids)
            _log.info(
                "worker=%s task=%s 已派 %d 个 sub-tasks: %s",
                worker.worker_id, task.task_id, len(sub_task_ids), sub_task_ids,
            )

            # 轮询等所有 sub-tasks 完成
            poll_interval = min(2.0, self.spec.poll_interval_sec)
            max_wait = task_timeout * 3  # sub-tasks 总时间可以更长
            t0 = time.time()
            while time.time() - t0 < max_wait:
                results = await store.get_sub_task_results(sub_task_ids)
                if len(results) == len(sub_task_ids):
                    # 所有都 done 或 failed
                    break
                await asyncio.sleep(poll_interval)
            sub_results = await store.get_sub_task_results(sub_task_ids)
            _log.info(
                "worker=%s task=%s sub-tasks 全部完成 (%d 个)",
                worker.worker_id, task.task_id, len(sub_results),
            )

            # 第二轮：汇总 prompt（用 sub-agent 结果做 context）
            summary_prompt = self._build_summary_prompt(
                original_payload=task.payload,
                first_output=result.stdout or "",
                sub_results=sub_results,
            )
            _log.info(
                "worker=%s task=%s 第二轮 spawned（汇总 %d 个 sub-agent 结果）",
                worker.worker_id, task.task_id, len(sub_results),
            )
            handle2 = await runtime.spawn(
                task_id=task.task_id + "-round2",
                model=worker.model,
                task=summary_prompt,
                workdir=self.spec.workdir,
                timeout_sec=task_timeout,
            )
            result2 = await runtime.wait(handle2, task_timeout)
            if result2.exit_code == 0:
                await self._finish_task(
                    store, worker, task, result2.stdout or "",
                    started, raw_output=result2.stdout or "",
                    sub_results=sub_results,
                )
            else:
                # 第二轮失败：fallback 到第一轮结果（去掉 SUBAGENT block）
                cleaned = strip_subagent_block(result.stdout or "")
                err = f"汇总轮失败: exit_code={result2.exit_code} stderr={(result2.stderr or '')[:300]}"
                await store.complete(
                    task_id=task.task_id,
                    worker=worker.worker_id,
                    result=cleaned[-4000:] + f"\n\n[sub-agent 汇总失败：{err}]",
                )
                worker.stats.record_failure(task.task_id, err[:200])
        except asyncio.CancelledError:
            try:
                await store.complete(
                    task_id=task.task_id,
                    worker=worker.worker_id,
                    result="",
                    error="cluster worker cancelled",
                )
            except Exception:  # noqa: BLE001
                pass
            raise
        except Exception as e:  # noqa: BLE001
            err = f"worker 异常：{e}\n{traceback.format_exc()[:500]}"
            _log.exception("worker=%s task=%s 异常", worker.worker_id, task.task_id)
            try:
                await store.complete(
                    task_id=task.task_id,
                    worker=worker.worker_id,
                    result="",
                    error=err,
                )
            except Exception:  # noqa: BLE001
                pass
            worker.stats.record_failure(task.task_id, err[:200])
        finally:
            if task.task_id in worker.current_tasks:
                worker.current_tasks.remove(task.task_id)
            worker.status = WORKER_STATUS_IDLE if worker.has_idle_slot() else WORKER_STATUS_BUSY

    async def _finish_task(
        self,
        store: TaskStore,
        worker: ClusterWorker,
        task,
        final_output: str,
        started: float,
        raw_output: str = "",
        sub_results: dict[str, dict] | None = None,
    ) -> None:
        """完成一个任务：把最终结果（含 sub-agent 元数据）写回。"""
        cleaned = strip_subagent_block(final_output or "")
        truncated = (cleaned or "")[-4000:]
        summary = (cleaned or "")[:300]
        # 把 sub-agent 结果拼到 result 末尾（如果存在）
        result_with_subs = truncated
        if sub_results:
            sub_summary = "\n\n[Sub-agents 已完成 {} 个]".format(len(sub_results))
            result_with_subs = truncated + sub_summary
        await store.complete(
            task_id=task.task_id,
            worker=worker.worker_id,
            result=result_with_subs,
        )
        worker.stats.record_success(time.time() - started, task.task_id)
        _log.info(
            "worker=%s task=%s OK duration=%.1fs sub_agents=%d",
            worker.worker_id, task.task_id, time.time() - started,
            len(sub_results) if sub_results else 0,
        )

    def _build_summary_prompt(
        self,
        original_payload: str,
        first_output: str,
        sub_results: dict[str, dict],
    ) -> str:
        """构造第二轮"汇总" prompt：原任务 + 第一轮答案 + sub-agent 结果 → 让 LLM 综合。"""
        parts = [
            f"# 原任务\n{original_payload}",
            "",
            f"# 你第一轮的回答（已经派了 sub-agents）\n{strip_subagent_block(first_output)}",
            "",
            f"# Sub-agents 完成结果（共 {len(sub_results)} 个）",
        ]
        for i, (sid, info) in enumerate(sub_results.items(), 1):
            status = info.get("status", "?")
            content = (info.get("result") or info.get("error") or "").strip()
            if len(content) > 2000:
                content = content[:2000] + "\n... (truncated)"
            parts.append(f"\n## Sub-agent #{i} ({sid[:8]}, status={status})\n{content}")
        parts.append("")
        parts.append(
            "# 请基于以上 sub-agent 的结果，汇总成最终答案给用户。"
            "不要再次派 sub-agents（这一轮是最终汇总），直接给完整答案。"
        )
        return "\n".join(parts)

    # ---------- 状态查询 ----------

    def info(self) -> dict:
        """导出 pool 状态（用于 list_workers / cluster_stats MCP tool）。"""
        return {
            "name": self.spec.name,
            "enabled": self.spec.enabled,
            "size": len(self.workers),
            "runtime": self.spec.runtime,
            "model": self.spec.model,
            "topic": self.spec.topic,
            "workdir": self.spec.workdir,
            "concurrency_per_worker": self.spec.concurrency_per_worker,
            "task_timeout_sec": self.spec.task_timeout_sec,
            "running": not self._stopping,
            "workers": [w.to_dict() for w in self.workers],
        }
