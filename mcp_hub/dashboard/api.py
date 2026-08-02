"""Dashboard API endpoints —— 只读，从 mcp-hub 的状态文件 + 模块拿数据。

数据源：
    - TaskStore: 任务历史（data/tasks.json）
    - runtimes 模块: detect_all() 拿 runtime 列表
    - <workdir>/.mcp-hub/subagents/*.log: 子 agent 执行日志
    - .env 配置: cluster 配置 + queue 路径
    - data/dashboard_state.json: dashboard 自己存的状态（pinned models 等）
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from mcp_hub.config import load_settings
from mcp_hub.queue import TaskStore
from mcp_hub.runtimes import detect_all
from mcp_hub.runtimes.base import read_transcript
from mcp_hub.tools import detect_all as detect_tools


class DashboardState:
    """dashboard 状态 holder —— 每次请求都从底层拉新数据（短轮询友好）。"""

    # 持久化文件路径（dashboard 自己的状态，跟 mcp-hub 核心解耦）
    STATE_FILE = Path("./data/dashboard_state.json")

    # runtime/tool 探测缓存：detect 要跑子进程（--version 等），一次好几秒；
    # 前端 2s 轮询不缓存会让 /api/overview 卡到十几秒、请求堆积。
    DETECT_TTL = 60.0

    def __init__(self):
        self._settings = None
        self._store: TaskStore | None = None
        self._state: dict = {}  # dashboard 自己存的状态（pinned_models 等）
        self._runtimes_cache: tuple[float, dict] | None = None
        self._tools_cache: tuple[float, dict] | None = None
        self._info_cache: tuple[float, list] | None = None
        self._models_all_cache: tuple[float, dict] | None = None

    def _detect_runtimes(self) -> dict:
        now = time.time()
        if self._runtimes_cache and now - self._runtimes_cache[0] < self.DETECT_TTL:
            return self._runtimes_cache[1]
        runtimes = detect_all()
        self._runtimes_cache = (now, runtimes)
        return runtimes

    def _detect_tools(self) -> dict:
        now = time.time()
        if self._tools_cache and now - self._tools_cache[0] < self.DETECT_TTL:
            return self._tools_cache[1]
        tools = detect_tools()
        self._tools_cache = (now, tools)
        return tools

    def _runtimes_info(self) -> list:
        """每个 runtime 的 info()（内部会跑 is_available/list_models 子进程），同样缓存。"""
        now = time.time()
        if self._info_cache and now - self._info_cache[0] < self.DETECT_TTL:
            return self._info_cache[1]
        items = [r.info() for r in self._detect_runtimes().values()]
        self._info_cache = (now, items)
        return items

    def _ensure(self) -> None:
        if self._settings is None:
            self._settings = load_settings()
        if self._store is None:
            self._store = TaskStore(self._settings.hub_queue_path)
        if not self._state:
            self._load_state()

    def _load_state(self) -> None:
        """从 data/dashboard_state.json 加载 dashboard 自己的状态。"""
        if self.STATE_FILE.exists():
            try:
                self._state = json.loads(self.STATE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._state = {}

    def _save_state(self) -> None:
        """把 dashboard 状态写到 data/dashboard_state.json。"""
        self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.STATE_FILE.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------- Pinned models (dashboard 自己存，不走 .env) ----------

    def get_pinned_models(self) -> dict[str, Any]:
        """获取当前白名单。如果 .env 也配了，合并（去重），优先用 dashboard 存的。"""
        # 1. dashboard 自己存的
        dashboard_pinned = self._state.get("pinned_models", [])
        # 2. .env 里的
        env_pinned_raw = self._settings.hub_dashboard_pinned_models
        env_pinned: list[str] = []
        if env_pinned_raw:
            try:
                env_pinned = json.loads(env_pinned_raw)
            except json.JSONDecodeError:
                pass
        # 合并去重
        combined = list(dict.fromkeys(env_pinned + dashboard_pinned))
        return {
            "ok": True,
            "pinned_models": combined,
            "from_env": env_pinned,
            "from_dashboard": dashboard_pinned,
        }

    def set_pinned_models(self, models: list[str]) -> dict[str, Any]:
        """设置白名单（写 dashboard_state.json，覆盖之前的）。"""
        if not isinstance(models, list):
            return {"ok": False, "error": "pinned_models 必须是数组"}
        # 清洗：去空 / 去重
        cleaned = []
        for m in models:
            if isinstance(m, str) and m.strip():
                if m.strip() not in cleaned:
                    cleaned.append(m.strip())
        self._state["pinned_models"] = cleaned
        self._save_state()
        self._models_all_cache = None  # 白名单变了，模型墙缓存立刻作废
        return {
            "ok": True,
            "pinned_models": cleaned,
            "saved_to": str(self.STATE_FILE),
        }

    # ---------- Overview ----------

    def overview(self) -> dict[str, Any]:
        self._ensure()
        runtimes = self._detect_runtimes()
        tools = self._detect_tools()

        # 队列统计
        loop = asyncio.new_event_loop()
        try:
            stats = loop.run_until_complete(self._store.stats())
            topics = loop.run_until_complete(self._store.list_topics())
        finally:
            loop.close()

        # 收集 subagent 状态（从 workdir/.mcp-hub/subagents/ 读 log 文件）
        subagent_logs = self._list_subagent_logs()

        return {
            "ok": True,
            "ts": time.time(),
            "runtimes": {
                "count": len(runtimes),
                "items": self._runtimes_info(),
            },
            "tools": {
                "count": len(tools),
                "items": [t.info() for t in tools.values()],
            },
            "queue": {
                "stats": stats,
                "topics": topics,
            },
            "subagent_logs": subagent_logs,
            "config": {
                "queue_path": self._settings.hub_queue_path,
                "cluster_enabled": self._settings.hub_cluster_enabled,
                "cluster_size": self._settings.hub_cluster_size,
                "cluster_model": self._settings.cluster_model_resolved(),
                "cluster_topic": self._settings.hub_cluster_topic,
                "max_concurrent_subagents": self._settings.hub_max_concurrent_subagents,
            },
        }

    # ---------- Runtimes ----------

    def runtimes(self) -> dict[str, Any]:
        return {
            "ok": True,
            "ts": time.time(),
            "items": self._runtimes_info(),
        }

    def models_all(self) -> dict[str, Any]:
        """所有 runtime + model 列表（去重），按 runtime 分组。

        用于"模型卡片墙"——opencode/claude/kimi/codex 每个 model 一张卡。
        白名单来源：dashboard_state.json（web 设的）+ .env 里的 HUB_DASHBOARD_PINNED_MODELS，合并去重。
        """
        self._ensure()
        now = time.time()
        if self._models_all_cache and now - self._models_all_cache[0] < self.DETECT_TTL:
            return self._models_all_cache[1]
        runtimes = self._detect_runtimes()
        groups: dict[str, list[dict[str, Any]]] = {}
        all_models: list[dict[str, Any]] = []
        for rname, r in runtimes.items():
            models = r.list_models()
            avail = r.is_available()  # 每个 runtime 探一次就够，别按模型数重复探
            groups[rname] = [{
                "name": m,
                "runtime": rname,
                "available": avail,
                "binary": r.binary,
            } for m in models]
            for m in models:
                all_models.append({
                    "name": m,
                    "runtime": rname,
                    "available": avail,
                })

        # 应用白名单（合并 .env + dashboard 存的）
        pinned_info = self.get_pinned_models()
        pinned_list = pinned_info["pinned_models"]

        if pinned_list:
            def _matches(m):
                full = f"{m['runtime']}/{m['name']}"
                return m["name"] in pinned_list or full in pinned_list
            all_models = [m for m in all_models if _matches(m)]
            groups = {}
            for m in all_models:
                groups.setdefault(m["runtime"], []).append(m)

        result = {
            "ok": True,
            "ts": time.time(),
            "by_runtime": groups,
            "all": all_models,
            "runtimes": self._runtimes_info(),
            "pinned": bool(pinned_list),
            "pinned_models": pinned_list,
        }
        self._models_all_cache = (time.time(), result)
        return result

    # ---------- Subagents ----------

    def _list_subagent_logs(self) -> list[dict[str, Any]]:
        """扫描所有候选 workdir 下的 .mcp-hub/subagents/*.log，返回最近修改的列表。

        不同 subagent 可能用不同 workdir，所以扫多个位置：
        - 配置里的 hub_cluster_workdir
        - 当前 cwd
        - ./data/ 下面的子目录（测试/临时 workdir）
        """
        self._ensure()
        # 候选根目录
        candidates = set()
        cfg_wd = Path(self._settings.hub_cluster_workdir or ".").resolve()
        candidates.add(cfg_wd)
        candidates.add(Path.cwd().resolve())
        data_dir = Path("./data").resolve()
        if data_dir.exists():
            candidates.add(data_dir)
            for sub in data_dir.iterdir():
                if sub.is_dir():
                    candidates.add(sub.resolve())

        items = []
        seen: set[str] = set()
        for wd in candidates:
            log_dir = wd / ".mcp-hub" / "subagents"
            if not log_dir.exists():
                continue
            for f in log_dir.glob("*.log"):
                if f.stem in seen or f.name.endswith(".err.log"):
                    continue
                seen.add(f.stem)
                st = f.stat()
                items.append({
                    "task_id": f.stem,
                    "path": str(f),
                    "workdir": str(wd),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                })
        # 再合并 subagents_registry.json —— 管道根治版起，spawn 都会登记绝对路径的
        # log_file，workdir 不再局限于候选目录，以 registry 为准
        registry_path = Path(self._settings.hub_queue_path).with_name("subagents_registry.json")
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            registry = {}
        for tid, entry in (registry.get("subagents") or {}).items():
            if tid in seen:
                continue
            log_file = entry.get("log_file") or ""
            if not log_file:
                continue
            f = Path(log_file)
            try:
                st = f.stat()
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = 0, entry.get("finished_at") or entry.get("started_at") or 0
            seen.add(tid)
            items.append({
                "task_id": tid,
                "path": log_file,
                "workdir": entry.get("workdir") or "",
                "size": size,
                "mtime": mtime,
            })
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items[:500]

    # 超过这个时间（秒）的任务视为已封存，默认不显示在主列表
    ARCHIVE_THRESHOLD_SEC = 3600

    def _is_archived(self, t: dict[str, Any]) -> bool:
        """判断一个任务是否已封存（创建时间超过阈值）。"""
        created_at = t.get("created_at") or 0
        return (time.time() - created_at) > self.ARCHIVE_THRESHOLD_SEC

    def subagents(self, include_archived: bool = False) -> dict[str, Any]:
        """从队列的 claimed_by + subagent_logs 推算当前子 agent。

        用 list_claimed_or_done 拿所有 worker 认领过的任务（claimed/verifying/done/failed），
        不只是 pending。dashboard 要看历史。

        默认隐藏超过 ARCHIVE_THRESHOLD_SEC 的会话，减少界面混乱；
        include_archived=true 时返回全部。
        """
        self._ensure()
        loop = asyncio.new_event_loop()
        try:
            # 拿所有 claimed + verifying + done 任务（按 created_at 倒序，limit 1000）
            tasks = loop.run_until_complete(
                self._store.list_claimed_or_done(limit=1000)
            )
            claimed_tasks = [t.to_dict() for t in tasks]
        finally:
            loop.close()

        # 跟 log 文件合并（log 文件可能有但 task 已经 done）
        logs = self._list_subagent_logs()
        log_by_id = {l["task_id"]: l for l in logs}

        # spawn_subagent 的任务不进队列，registry 里有 runtime/model/caller/状态
        registry_path = Path(self._settings.hub_queue_path).with_name("subagents_registry.json")
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            registry = {}
        reg_by_id: dict[str, Any] = registry.get("subagents") or {}

        now = time.time()
        # 运行中但日志 5 分钟没更新，视为疑似卡死
        STUCK_THRESHOLD_SEC = 300

        all_subagents = []
        for t in claimed_tasks:
            tid = t["task_id"]
            log = log_by_id.get(tid, {})
            status = t.get("status") or ""
            last_activity = log.get("mtime") or t.get("claimed_at") or t.get("created_at")
            is_running = status in ("running", "claimed", "pending")
            stuck = is_running and last_activity and (now - last_activity) > STUCK_THRESHOLD_SEC
            all_subagents.append({
                "task_id": tid,
                "topic": t.get("topic"),
                "status": status,
                "claimed_by": t.get("claimed_by"),
                "from_model": t.get("from_model"),
                "for_model": t.get("for_model"),
                "created_at": t.get("created_at"),
                "claimed_at": t.get("claimed_at"),
                "completed_at": t.get("completed_at"),
                "result_preview": (t.get("result") or "")[:300],
                "error": t.get("error"),
                "has_log": bool(log),
                "log_path": log.get("path"),
                "log_size": log.get("size", 0),
                "log_mtime": log.get("mtime"),
                "last_activity": last_activity,
                "possibly_stuck": stuck,
                "duration_sec": (
                    (t.get("completed_at") or now) - t.get("claimed_at")
                    if t.get("claimed_at") else 0
                ),
            })

        # spawn_subagent 派生的任务（不进队列）：以 registry/日志为准单独补进来
        claimed_ids = {t["task_id"] for t in claimed_tasks}
        for l in logs:
            tid = l["task_id"]
            if tid in claimed_ids:
                continue
            reg = reg_by_id.get(tid) or {}
            started = reg.get("started_at") or l["mtime"]
            finished = reg.get("finished_at")
            status = reg.get("status") or "unknown"
            last_activity = l["mtime"] or started
            is_running = status in ("running", "claimed", "pending")
            stuck = is_running and last_activity and (now - last_activity) > STUCK_THRESHOLD_SEC
            all_subagents.append({
                "task_id": tid,
                "topic": None,
                "status": status,
                "claimed_by": reg.get("runtime"),
                "from_model": reg.get("caller"),
                "for_model": reg.get("model"),
                "reasoning_effort": reg.get("reasoning_effort") or "",
                "created_at": started,
                "claimed_at": started,
                "completed_at": finished,
                "result_preview": "",
                "error": None,
                "has_log": True,
                "log_path": l["path"],
                "log_size": l["size"],
                "log_mtime": l["mtime"],
                "last_activity": last_activity,
                "possibly_stuck": stuck,
                "duration_sec": (finished or now) - started if started else 0,
            })

        all_subagents.sort(key=lambda x: x.get("claimed_at") or 0, reverse=True)

        archived = [s for s in all_subagents if self._is_archived(s)]
        visible = all_subagents if include_archived else [
            s for s in all_subagents if not self._is_archived(s)
        ]

        return {
            "ok": True,
            "ts": time.time(),
            "count": len(visible),
            "subagents": visible,
            "archived_count": len(archived),
            "total_count": len(all_subagents),
            "include_archived": include_archived,
        }

    def subagent_log(self, task_id: str, tail_kb: int = 64) -> dict[str, Any]:
        """读子 agent 日志（tail 最后 tail_kb KB），在所有候选 workdir 下找。"""
        self._ensure()
        # 先看 _list_subagent_logs 拿所有位置
        for item in self._list_subagent_logs():
            if item["task_id"] == task_id:
                log_file = Path(item["path"])
                size = log_file.stat().st_size
                with log_file.open("rb") as f:
                    f.seek(max(0, size - tail_kb * 1024))
                    content = f.read().decode("utf-8", errors="replace")
                return {
                    "ok": True,
                    "task_id": task_id,
                    "size": size,
                    "tail_kb": tail_kb,
                    "content": content,
                    "path": str(log_file),
                    "workdir": item.get("workdir"),
                }
        return {"ok": False, "error": f"no log for {task_id}", "task_id": task_id}

    def subagent_transcript(self, task_id: str) -> dict[str, Any]:
        """读子 agent 的结构化 transcript（chat-like 事件流）。

        transcript 来自 {log_path}.transcript.jsonl，由 runtime adapter 在 wait() 末尾写。
        每行是一个 event（type: prompt/turn/tool_call/tool_result/file_change/final/error）。
        """
        self._ensure()
        for item in self._list_subagent_logs():
            if item["task_id"] == task_id:
                log_file = Path(item["path"])
                transcript_file = log_file.with_suffix(".transcript.jsonl")
                if not transcript_file.exists():
                    # transcript 要等进程结束才写盘；运行中就从裸 .log 实时解析一份预览
                    live = self._parse_live_log(task_id, log_file)
                    if live is not None:
                        return live
                    return {
                        "ok": False,
                        "error": "no transcript file (older run, before v3)",
                        "task_id": task_id,
                        "transcript_path_expected": str(transcript_file),
                    }
                events = read_transcript(transcript_file)
                size = transcript_file.stat().st_size
                return {
                    "ok": True,
                    "task_id": task_id,
                    "event_count": len(events),
                    "events": events,
                    "path": str(transcript_file),
                    "size": size,
                }
        return {"ok": False, "error": f"no log for {task_id}", "task_id": task_id}

    # 实时预览最多读日志末尾多少字节（防止大日志每次全量解析）
    LIVE_LOG_MAX_BYTES = 2 * 1024 * 1024

    def _parse_live_log(self, task_id: str, log_file: Path) -> dict[str, Any] | None:
        """运行中的子 agent：从裸 .log 实时解析出 transcript 预览。

        各 runtime 的 JSONL 解析器本就是无状态的逐行解析，直接吃日志内容即可。
        runtime 从 registry 查；查不到或不支持的 runtime 返回原始日志尾巴作为一个
        兜底事件，保证用户总能看到点东西。解析失败返回 None（走"无 transcript"提示）。
        """
        try:
            size = log_file.stat().st_size
            with log_file.open("rb") as f:
                if size > self.LIVE_LOG_MAX_BYTES:
                    f.seek(size - self.LIVE_LOG_MAX_BYTES)
                raw = f.read().decode("utf-8", errors="replace")
        except OSError:
            return None

        runtime = ""
        registry_path = Path(self._settings.hub_queue_path).with_name("subagents_registry.json")
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            runtime = ((registry.get("subagents") or {}).get(task_id) or {}).get("runtime") or ""
        except Exception:  # noqa: BLE001
            pass

        events: list[dict[str, Any]] | None = None
        try:
            if runtime == "opencode":
                from mcp_hub.runtimes.opencode import _clean_ansi, _parse_opencode_jsonl
                events, _, _ = _parse_opencode_jsonl(_clean_ansi(raw))
            elif runtime == "codex":
                from mcp_hub.runtimes.codex import _clean_ansi, _parse_codex_jsonl
                events = _parse_codex_jsonl(_clean_ansi(raw))
            elif runtime == "claude":
                from mcp_hub.runtimes.claude import _clean_ansi, _parse_claude_stream_json
                events = _parse_claude_stream_json(_clean_ansi(raw))
            elif runtime == "kimi":
                from mcp_hub.runtimes.kimi import _clean_ansi, _parse_kimi_stream_json
                events = _parse_kimi_stream_json(_clean_ansi(raw))
            elif runtime == "antigravity":
                from mcp_hub.runtimes.antigravity import parse_live_log
                events = parse_live_log(raw, "")
            elif runtime == "qoder":
                from mcp_hub.runtimes.qoder import parse_live_log
                events = parse_live_log(raw, "")
        except Exception:  # noqa: BLE001
            events = None

        # 从 registry 拿 prompt / started_at / status，让前端知道任务是不是还在跑
        prompt = ""
        started_at = time.time()
        status = "running"
        registry_path = Path(self._settings.hub_queue_path).with_name("subagents_registry.json")
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            entry = (registry.get("subagents") or {}).get(task_id) or {}
            prompt = entry.get("prompt") or ""
            started_at = entry.get("started_at") or started_at
            status = entry.get("status") or status
        except Exception:  # noqa: BLE001
            pass

        # 补 ts：事件里没有 ts 的用 started_at（用户要看时间判断是否卡死）
        for ev in events:
            ev.setdefault("ts", started_at)

        if prompt:
            events = [{"type": "prompt", "ts": started_at, "content": prompt}] + events

        if not events:
            # 不支持的 runtime 或解析为空：给原始日志尾巴兜底
            tail = raw[-4000:]
            events = [{"type": "turn", "role": "assistant",
                       "content": f"（实时原始日志，runtime={runtime or 'unknown'} 暂不支持结构化解析）\n\n{tail}"}]
            events[0]["ts"] = started_at

        return {
            "ok": True,
            "task_id": task_id,
            "event_count": len(events),
            "events": events,
            "path": str(log_file),
            "size": size,
            "live": True,
            "started_at": started_at,
            "status": status,
        }

    # ---------- 用户手动消息干涉 ----------

    def _user_messages_path(self, task_id: str) -> Path | None:
        """用户消息文件：{log_path}.user_messages.jsonl。"""
        for item in self._list_subagent_logs():
            if item["task_id"] == task_id:
                return Path(item["path"]).with_suffix(".user_messages.jsonl")
        return None

    def save_user_message(self, task_id: str, message: str, from_user: str = "用户") -> dict[str, Any]:
        """把用户手动干预消息追加保存到任务旁边的 jsonl 文件。"""
        if not message or not message.strip():
            return {"ok": False, "error": "消息不能为空"}
        path = self._user_messages_path(task_id)
        if path is None:
            return {"ok": False, "error": f"任务 {task_id} 没有日志文件，无法保存消息"}
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.time(),
            "from": from_user,
            "message": message.strip(),
        }
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            return {"ok": True, "task_id": task_id, "path": str(path), "record": record}
        except OSError as e:
            return {"ok": False, "error": f"写消息文件失败: {e}"}

    def get_user_messages(self, task_id: str) -> dict[str, Any]:
        """读用户手动消息列表。"""
        path = self._user_messages_path(task_id)
        if path is None or not path.exists():
            return {"ok": True, "task_id": task_id, "messages": []}
        messages: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            return {"ok": False, "error": f"读消息文件失败: {e}", "messages": messages}
        return {"ok": True, "task_id": task_id, "messages": messages, "path": str(path)}

    # ---------- 任务续跑（dashboard 直接 spawn） ----------

    def _registry_path(self) -> Path:
        return Path(self._settings.hub_queue_path).with_name("subagents_registry.json")

    def _registry_load(self) -> dict[str, Any]:
        path = self._registry_path()
        if not path.exists():
            return {"subagents": {}}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"subagents": {}}

    def _registry_save(self, data: dict[str, Any]) -> None:
        path = self._registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def continue_subagent(self, task_id: str, message: str = "", from_user: str = "用户") -> dict[str, Any]:
        """基于原任务 spawn 一个续跑子 agent。

        场景：原任务超时 / 报错 / 被用户中断，用户手动发一条消息（如"继续"或"改一下 XX"），
        dashboard 把原 prompt + 原任务输出摘要 + 用户消息拼成新 prompt，用相同 runtime/model/workdir
        重新 spawn 一个子 agent。返回新的 task_id。
        """
        self._ensure()
        registry = self._registry_load()
        original = (registry.get("subagents") or {}).get(task_id)
        if not original:
            return {"ok": False, "error": f"registry 里找不到任务 {task_id}"}

        runtime_name = original.get("runtime") or ""
        model = original.get("model") or ""
        workdir = original.get("workdir") or "."
        original_prompt = original.get("prompt") or ""
        if not runtime_name or not model:
            return {"ok": False, "error": "原任务缺少 runtime / model 信息，无法续跑"}

        # 读原任务日志摘要（给续跑 agent 上下文）
        log_summary = ""
        for item in self._list_subagent_logs():
            if item["task_id"] == task_id:
                try:
                    p = Path(item["path"])
                    raw = p.read_text(encoding="utf-8", errors="replace")
                    # 去掉 header，取最后 2000 字符
                    import re
                    raw = re.sub(r"^=== 实时日志 \([^)]*\) ===\n?", "", raw)
                    log_summary = raw[-2000:].strip()
                except Exception:  # noqa: BLE001
                    pass
                break

        # 读所有用户消息（包含本次新消息）
        msgs = self.get_user_messages(task_id).get("messages", [])
        if message and message.strip():
            msgs.append({"ts": time.time(), "from": from_user, "message": message.strip()})
        user_msgs_text = "\n".join(f"- [{m.get('from', '用户')}]: {m.get('message', '')}" for m in msgs[-5:])

        continuation_prompt = (
            f"这是一个续跑任务。原任务 ID：{task_id}。\n"
            f"原任务指令：\n{original_prompt}\n\n"
            f"原任务最近输出摘要：\n{log_summary or '(无可用输出)'}\n\n"
            f"用户补充指令：\n{user_msgs_text or '(无)'}\n\n"
            f"请基于以上上下文继续完成任务。如果原任务已经部分完成，请从中断处继续；"
            f"如果用户指令要求修改方向，请按最新指令执行。"
        )

        runtimes = detect_all()
        adapter = runtimes.get(runtime_name)
        if adapter is None or not adapter.is_available():
            return {"ok": False, "error": f"runtime '{runtime_name}' 当前不可用"}

        import uuid
        new_task_id = uuid.uuid4().hex[:12]

        # 优先用原生 session resume（opencode / codex / qoder），模型能识别原会话；
        # 不支持 resume 的 runtime 退化为"新 spawn + 文本上下文"。
        use_resume = False
        session_id = None
        if getattr(adapter, "supports_resume", False):
            log_path = original.get("log_file") or ""
            if log_path:
                try:
                    from mcp_hub.runtimes.base import SubagentHandle
                    fake_handle = SubagentHandle(
                        pid=None,
                        runtime=runtime_name,
                        model=model,
                        task_id=task_id,
                        workdir=workdir,
                        started_at=original.get("started_at") or time.time(),
                        output_file=Path(log_path),
                    )
                    session_id = adapter.extract_session_id(fake_handle)
                    use_resume = bool(session_id)
                except Exception:  # noqa: BLE001
                    use_resume = False

        # spawn 是 async，在当前 loop 里跑
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            if use_resume and session_id:
                # resume 时 task 只放用户最新指令，原会话上下文 CLI 自己带
                resume_task = message.strip() or "请继续完成未完成的任务。"
                handle = loop.run_until_complete(
                    adapter.resume_spawn(
                        session_id=session_id,
                        task_id=new_task_id,
                        model=model,
                        task=resume_task,
                        workdir=workdir,
                        timeout_sec=1800,
                    )
                )
            else:
                handle = loop.run_until_complete(
                    adapter.spawn(
                        task_id=new_task_id,
                        model=model,
                        task=continuation_prompt,
                        workdir=workdir,
                        timeout_sec=1800,
                    )
                )
        except Exception as e:  # noqa: BLE001
            loop.close()
            return {"ok": False, "error": f"spawn 失败: {e}"}

        # 写 registry 让 dashboard 能跟踪状态
        registry = self._registry_load()
        registry["subagents"][new_task_id] = {
            "runtime": runtime_name,
            "model": model,
            "pid": handle.pid,
            "workdir": workdir,
            "log_file": str(handle.output_file) if handle.output_file else "",
            "started_at": handle.started_at,
            "caller": from_user,
            "status": "running",
            "prompt": continuation_prompt,
            "continued_from": task_id,
            "resume_used": use_resume,
            "session_id": session_id,
        }
        self._registry_save(registry)

        # 后台轮询等它自然退出，避免 dashboard 阻塞
        async def _watch() -> None:
            try:
                await adapter.wait(handle, timeout_sec=1800)
                registry = self._registry_load()
                entry = registry["subagents"].get(new_task_id)
                if entry:
                    entry["status"] = "done"
                    entry["finished_at"] = time.time()
                    self._registry_save(registry)
            except Exception:  # noqa: BLE001
                registry = self._registry_load()
                entry = registry["subagents"].get(new_task_id)
                if entry:
                    entry["status"] = "failed"
                    entry["finished_at"] = time.time()
                    self._registry_save(registry)

        try:
            loop.run_until_complete(_watch())
        finally:
            loop.close()

        return {
            "ok": True,
            "new_task_id": new_task_id,
            "continued_from": task_id,
            "runtime": runtime_name,
            "model": model,
            "workdir": workdir,
            "resume_used": use_resume,
            "session_id": session_id,
            "note": (
                f"runtime={runtime_name} 支持原生 session resume，模型能识别原会话"
                if use_resume else
                f"runtime={runtime_name} 不支持 session resume，已用新进程+文本上下文续跑"
            ),
        }

    def subagent_stream(self, task_id: str):
        """SSE 实时日志流：tail 子 agent 的 .log 文件。

        返回一个生成器，yield 格式化的 SSE data 行。
        前端用 EventSource 连接，可实时看到子 agent stdout/stderr。
        """
        self._ensure()

        # 等 log 文件出现（子进程可能刚启动还没创建）
        log_file: Path | None = None
        for _ in range(60):
            for item in self._list_subagent_logs():
                if item["task_id"] == task_id:
                    log_file = Path(item["path"])
                    break
            if log_file and log_file.exists():
                break
            time.sleep(0.5)
            yield f"data: {json.dumps({'type': 'heartbeat', 'note': 'waiting for log file'}, ensure_ascii=False)}\n\n"

        if not log_file or not log_file.exists():
            yield f"data: {json.dumps({'type': 'error', 'message': f'no log file for {task_id}'}, ensure_ascii=False)}\n\n"
            return

        yield f"data: {json.dumps({'type': 'meta', 'path': str(log_file)}, ensure_ascii=False)}\n\n"

        # tail 模式：先读完已有内容，再持续 poll 新行
        try:
            with log_file.open("r", encoding="utf-8", errors="replace") as f:
                # 先推送已有内容
                existing = f.read()
                if existing:
                    yield f"data: {json.dumps({'type': 'log', 'content': existing}, ensure_ascii=False)}\n\n"

                # 持续 tail
                idle_count = 0
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {json.dumps({'type': 'log', 'content': line}, ensure_ascii=False)}\n\n"
                        idle_count = 0
                    else:
                        time.sleep(0.3)
                        idle_count += 1
                        # 每 ~3 秒发一个心跳，保持连接
                        if idle_count >= 10:
                            yield f"data: {json.dumps({'type': 'heartbeat'}, ensure_ascii=False)}\n\n"
                            idle_count = 0
        except OSError as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    def cluster_submit(self, payload: str, topic: str = "cluster.work",
                      from_model: str = "用户",
                      runtime: str = "",
                      model: str = "",
                      acceptance_json: str = "",
                      acceptance_criteria: list = None,
                      acceptance_verifier: str = "",
                      acceptance_max_iterations: int = 2,
                      acceptance_auto_retry: bool = True,
                      timeout_sec: int = 0,
                      webhook: str = "") -> dict[str, Any]:
        """从 dashboard 派一个任务到 topic（默认 cluster.work）。

        from_model 默认 "用户" —— 在 web 派活时统一署名。
        cluster worker（mcp-hub server 进程里）会 claim 这个任务，调 subagent 跑。
        dashboard 拿到 task_id 后可以轮询 cluster_task(task_id) 看结果。

        新增 runtime/model 参数：派发时指定目标 runtime + model，task 的
        for_model 字段会带上，worker 认领时按这个匹配。

        验收：
          - acceptance_criteria: 验收标准列表（每条是字符串）
          - acceptance_verifier: 用哪个 model 验（alias 或 model id）
          - acceptance_max_iterations: 验不过最大重做次数
          - acceptance_auto_retry: 验不过是否自动重发
          - acceptance_json: 直接传 JSON 字符串覆盖（高级用法）
        """
        self._ensure()
        if not payload or not payload.strip():
            return {"ok": False, "error": "payload 不能为空"}

        # 拼 acceptance 配置
        acceptance = None
        if acceptance_json:
            try:
                acceptance = json.loads(acceptance_json)
            except json.JSONDecodeError as e:
                return {"ok": False, "error": f"acceptance_json 解析失败：{e}"}
        elif acceptance_criteria:
            acceptance = {
                "criteria": [c for c in acceptance_criteria if isinstance(c, str) and c.strip()],
                "verifier": acceptance_verifier.strip() or "claude",
                "max_iterations": int(acceptance_max_iterations or 0),
                "auto_retry": bool(acceptance_auto_retry),
            }
            # 清洗 criteria：去空
            acceptance["criteria"] = [c.strip() for c in acceptance["criteria"]]

        # 如果指定了 model 但没指定 runtime，从所有 runtime 里猜一个能跑这个 model 的
        if model and not runtime:
            runtimes = detect_all()
            for rname, r in runtimes.items():
                if r.is_available() and model in r.list_models():
                    runtime = rname
                    break
            if not runtime:
                return {
                    "ok": False,
                    "error": f"model '{model}' 在所有 runtime 都不可用",
                    "hint": "试试 list_models 看哪些 model 在线",
                }

        # for_model 拼成 "runtime/model" 格式（worker claim 时能匹配）
        for_model = f"{runtime}/{model}" if runtime and model else ""

        # 按 for_model 自动选 topic（多 pool 路由）：
        #   - 优先用用户传的 topic
        #   - 否则按 for_model 找匹配 pool 的 topic
        #   - 都没有就用第一个 pool 的 topic
        if topic == "cluster.work" and for_model:
            # 用户没显式改 topic，按 for_model 路由
            pool_specs = self._settings.cluster_pool_specs()
            matched = None
            for ps in pool_specs:
                if f"{ps['runtime']}/{ps['model']}" == for_model:
                    matched = ps
                    break
            if matched:
                topic = matched["topic"]
            elif pool_specs:
                # 退化到第一个 enabled 的 pool
                for ps in pool_specs:
                    if ps.get("enabled", True):
                        topic = ps["topic"]
                        break

        metadata: dict[str, Any] = {}
        if timeout_sec and timeout_sec > 0:
            metadata["timeout_sec"] = timeout_sec

        loop = asyncio.new_event_loop()
        try:
            task = loop.run_until_complete(
                self._store.publish(
                    topic=topic,
                    payload=payload,
                    from_model=from_model,
                    for_model=for_model or None,
                    metadata=metadata or None,
                    acceptance=acceptance,
                    webhook=webhook,
                )
            )
        finally:
            loop.close()
        return {
            "ok": True,
            "task_id": task.task_id,
            "topic": task.topic,
            "from_model": from_model,
            "for_model": for_model,
            "runtime": runtime,
            "model": model,
            "has_acceptance": bool(acceptance and acceptance.get("criteria")),
            "created_at": task.created_at,
        }

    def cluster_task(self, task_id: str) -> dict[str, Any]:
        """轮询 cluster 任务结果（dashboard 派活后用这个查）。"""
        self._ensure()
        loop = asyncio.new_event_loop()
        try:
            task = loop.run_until_complete(self._store.status(task_id))
        finally:
            loop.close()
        if task is None:
            return {"ok": False, "error": "not found", "task_id": task_id}
        return {"ok": True, "task": task.to_dict()}

    def subagent_history(self, task_id: str, limit: int = 5) -> dict[str, Any]:
        """看 task 的相关历史 —— 同一 worker / 同一 model / 同一 from 派过的其他任务。

        用来回答"这个 task 是谁派的，谁接的，历史上还干过什么"。
        """
        self._ensure()
        loop = asyncio.new_event_loop()
        try:
            task = loop.run_until_complete(self._store.status(task_id))
            if task is None:
                return {"ok": False, "error": "task not found", "task_id": task_id}

            # 拿所有 topic 的任务（limit 大点，按时间过滤）
            topics = loop.run_until_complete(self._store.list_topics())
            all_tasks = []
            for t in topics:
                all_tasks.extend(loop.run_until_complete(self._store.peek(t, limit=200)))
        finally:
            loop.close()

        all_tasks.sort(key=lambda x: x.created_at, reverse=True)

        def _filter(pred):
            return [
                t.to_dict() for t in all_tasks
                if t.task_id != task_id and pred(t)
            ][:limit]

        return {
            "ok": True,
            "task_id": task_id,
            "history": {
                "by_worker": _filter(lambda t: t.claimed_by == task.claimed_by and t.claimed_by),
                "by_for_model": _filter(lambda t: t.for_model == task.for_model and t.for_model),
                "by_from_model": _filter(lambda t: t.from_model == task.from_model and t.from_model),
                "by_topic": _filter(lambda t: t.topic == task.topic and t.topic),
            },
        }

    # ---------- Cluster ----------

    def cluster(self) -> dict[str, Any]:
        """cluster 状态：所有 pool 的 config + 队列堆积。"""
        self._ensure()
        s = self._settings
        if not s.hub_cluster_enabled:
            return {
                "ok": True,
                "enabled": False,
                "note": "cluster 未启用",
            }

        # 解析多 pool 配置（不实际启动，只看 config + 队列）
        pool_specs = s.cluster_pool_specs()
        if not pool_specs:
            return {
                "ok": True,
                "enabled": True,
                "pool_count": 0,
                "note": "cluster enabled 但没有任何 pool 配置",
            }

        loop = asyncio.new_event_loop()
        try:
            pool_summaries = []
            total_pending = 0
            total_claimed = 0
            total_done = 0
            total_failed = 0
            for p in pool_specs:
                topic = p["topic"]
                tasks = loop.run_until_complete(self._store.peek(topic, limit=200))
                pending = sum(1 for t in tasks if t.status == "pending")
                claimed = sum(1 for t in tasks if t.status == "claimed")
                done = sum(1 for t in tasks if t.status == "done")
                failed = sum(1 for t in tasks if t.status == "failed")
                total_pending += pending
                total_claimed += claimed
                total_done += done
                total_failed += failed
                pool_summaries.append({
                    "name": p["name"],
                    "enabled": p.get("enabled", True),
                    "size": p.get("size", 1),
                    "runtime": p["runtime"],
                    "model": p["model"],
                    "topic": topic,
                    "workdir": p.get("workdir", "."),
                    "concurrency_per_worker": p.get("concurrency_per_worker", 1),
                    "task_timeout_sec": p.get("task_timeout_sec", 600),
                    "queue": {
                        "pending": pending,
                        "claimed": claimed,
                        "done": done,
                        "failed": failed,
                    },
                })
        finally:
            loop.close()

        return {
            "ok": True,
            "enabled": True,
            "pool_count": len(pool_summaries),
            "pools": pool_summaries,
            "totals": {
                "pending": total_pending,
                "claimed": total_claimed,
                "done": total_done,
                "failed": total_failed,
            },
            "note": (
                "dashboard 独立进程不重复跑 cluster；"
                "worker 实时状态以 mcp-hub server 进程为准（看 list_workers 工具）。"
            ),
        }

    # ---------- Tasks ----------

    def tasks(self, topic: str = "", limit: int = 100,
              include_archived: bool = False) -> dict[str, Any]:
        """列所有任务（不限 pending），用于 task 树 / 历史查看。

        旧实现只用 peek() 导致只返回 pending 任务，dashboard task 列表长期为空。
        现在直接读全量 tasks.json 并可选按 topic 过滤。

        默认隐藏超过 1 小时的已封存任务；include_archived=true 显示全部。
        """
        self._ensure()
        data = self._store._load()
        task_list = [t for t in data.get("tasks", [])]
        if topic:
            task_list = [t for t in task_list if t.get("topic") == topic]
        task_list.sort(key=lambda x: x.get("created_at", 0), reverse=True)

        archived = [t for t in task_list if self._is_archived(t)]
        visible = task_list if include_archived else [
            t for t in task_list if not self._is_archived(t)
        ]

        return {
            "ok": True,
            "ts": time.time(),
            "count": len(visible[:limit]),
            "tasks": visible[:limit],
            "archived_count": len(archived),
            "total_count": len(task_list),
            "include_archived": include_archived,
        }

    def task(self, task_id: str) -> dict[str, Any]:
        self._ensure()
        loop = asyncio.new_event_loop()
        try:
            task = loop.run_until_complete(self._store.status(task_id))
        finally:
            loop.close()
        if task is None:
            return {"ok": False, "error": "not found", "task_id": task_id}
        return {"ok": True, "task": task.to_dict()}

    def verify_task(self, task_id: str, verifier: str, passed: bool,
                   score: float = 0.0, issues: str = "") -> dict[str, Any]:
        """在 dashboard 写一个 verify 记录到 task。

        这会更新 task 的 verify_history，task 状态会变 done / failed（根据
        acceptance.auto_retry 和 max_iterations）。
        """
        self._ensure()
        loop = asyncio.new_event_loop()
        try:
            task, action = loop.run_until_complete(
                self._store.verify(
                    task_id=task_id,
                    verifier=verifier,
                    passed=passed,
                    score=score,
                    issues=issues,
                )
            )
        finally:
            loop.close()
        if task is None:
            return {"ok": False, "error": f"task '{task_id}' 不在 verifying 状态", "task_id": task_id}
        return {
            "ok": True,
            "task_id": task_id,
            "next_action": action,
            "task": task.to_dict(),
        }

    def _sub_tasks(self, task_id: str) -> list[dict[str, Any]]:
        """拿某个 task 的 sub-task 列表（含状态、结果预览、log/transcript 是否存在）。"""
        self._ensure()
        loop = asyncio.new_event_loop()
        try:
            task = loop.run_until_complete(self._store.status(task_id))
            sub_ids = (task.metadata or {}).get("sub_task_ids", []) if task else []
            if not sub_ids:
                return []
            sub_results = loop.run_until_complete(self._store.get_sub_task_results(sub_ids))
        finally:
            loop.close()

        logs = {l["task_id"]: l for l in self._list_subagent_logs()}
        out = []
        for tid in sub_ids:
            info = sub_results.get(tid, {})
            log = logs.get(tid, {})
            transcript_file = None
            if log:
                p = Path(log["path"])
                tp = p.with_suffix(".transcript.jsonl")
                if tp.exists():
                    transcript_file = str(tp)
            out.append({
                "task_id": tid,
                "status": info.get("status", "unknown"),
                "result_preview": (info.get("result") or "")[:200],
                "error": info.get("error"),
                "claimed_by": info.get("claimed_by"),
                "has_log": bool(log),
                "log_path": log.get("path"),
                "has_transcript": bool(transcript_file),
                "transcript_path": transcript_file,
            })
        return out

    def task_details(self, task_id: str, history_limit: int = 5) -> dict[str, Any]:
        """任务的完整详情 + 相关历史 + sub-task 树（点开条目看这个）。

        返回：
          - task: 完整 task 数据（含 payload / result / error / verify_history）
          - log: 子 agent log 文件 tail（如果有）
          - transcript: 结构化会话事件（如果有）
          - sub_tasks: 该任务派生的 sub-task 列表
          - history: 同 model / 同 from / 同 topic / 同 worker 的其他任务
        """
        self._ensure()
        loop = asyncio.new_event_loop()
        try:
            task = loop.run_until_complete(self._store.status(task_id))
            if task is None:
                return {"ok": False, "error": "not found", "task_id": task_id}

            # 拿所有 topic 任务做历史关联
            topics = loop.run_until_complete(self._store.list_topics())
            all_tasks = []
            for t in topics:
                all_tasks.extend(loop.run_until_complete(self._store.peek(t, limit=300)))
        finally:
            loop.close()

        all_tasks.sort(key=lambda x: x.created_at, reverse=True)

        def _filter(pred):
            return [
                t.to_dict() for t in all_tasks
                if t.task_id != task_id and pred(t)
            ][:history_limit]

        history = {
            "by_for_model": _filter(lambda t: t.for_model and t.for_model == task.for_model),
            "by_from_model": _filter(lambda t: t.from_model == task.from_model and t.from_model),
            "by_worker": _filter(lambda t: t.claimed_by == task.claimed_by and t.claimed_by),
            "by_topic": _filter(lambda t: t.topic == task.topic and t.topic),
        }

        # 找 log（subagent 写过日志的）
        log_info = None
        transcript_info = None
        for item in self._list_subagent_logs():
            if item["task_id"] == task_id:
                log_file = Path(item["path"])
                size = log_file.stat().st_size
                with log_file.open("rb") as f:
                    f.seek(max(0, size - 32 * 1024))  # tail 32KB
                    content = f.read().decode("utf-8", errors="replace")
                log_info = {
                    "size": size,
                    "path": str(log_file),
                    "content": content,
                }
                # transcript
                transcript_file = log_file.with_suffix(".transcript.jsonl")
                if transcript_file.exists():
                    events = read_transcript(transcript_file)
                    transcript_info = {
                        "size": transcript_file.stat().st_size,
                        "path": str(transcript_file),
                        "event_count": len(events),
                        "events": events,
                    }
                break

        sub_tasks = self._sub_tasks(task_id)

        return {
            "ok": True,
            "task": task.to_dict(),
            "log": log_info,
            "transcript": transcript_info,
            "sub_tasks": sub_tasks,
            "history": history,
        }
