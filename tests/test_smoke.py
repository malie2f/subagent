"""v2 测试 —— 队列 + 验收 + 通知。"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from mcp_hub.queue import TaskStore


# ---------- 工具：本地 HTTP 接收器（测 webhook 用）----------

class _WebhookCapture:
    def __init__(self):
        self.received: list[dict[str, Any]] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    def start(self) -> str:
        capture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    body = {"raw": raw.decode("utf-8", errors="replace")}
                capture.received.append(body)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *args, **kwargs):  # noqa: ANN002, ANN003
                pass

        # 找空闲端口
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self.port}/hook"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def tmp_store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks.json")


@pytest.fixture
def webhook() -> Any:
    cap = _WebhookCapture()
    cap.url = cap.start()  # 把 start 返回的 url 挂到 cap 上
    yield cap
    cap.stop()


# ---------- 基础队列（v1 兼容）----------

async def test_publish_and_claim(tmp_store: TaskStore) -> None:
    t = await tmp_store.publish(topic="demo", payload="hello", from_model="t")
    assert t.status == "pending"
    c = await tmp_store.claim(topic="demo", worker="w")
    assert c is not None
    assert c.task_id == t.task_id


async def test_complete_done(tmp_store: TaskStore) -> None:
    t = await tmp_store.publish(topic="d", payload="p", from_model="t")
    c = await tmp_store.claim(topic="d", worker="w")
    assert c is not None
    task, action = await tmp_store.complete(task_id=t.task_id, worker="w", result="ok")
    assert task is not None
    assert task.status == "done"
    assert action == "done"


async def test_complete_with_error(tmp_store: TaskStore) -> None:
    t = await tmp_store.publish(topic="d", payload="p", from_model="t")
    await tmp_store.claim(topic="d", worker="w")
    task, action = await tmp_store.complete(task_id=t.task_id, worker="w", result="", error="boom")
    assert task is not None
    assert task.status == "failed"
    assert action == "failed"


# ---------- v2: acceptance + verifying 流程 ----------

async def test_complete_with_acceptance_enters_verifying(tmp_store: TaskStore) -> None:
    t = await tmp_store.publish(
        topic="d",
        payload="p",
        from_model="t",
        acceptance={"criteria": ["must pass tests"], "verifier": "claude"},
    )
    await tmp_store.claim(topic="d", worker="w")
    task, action = await tmp_store.complete(task_id=t.task_id, worker="w", result="did the thing")
    assert task is not None
    assert task.status == "verifying"
    assert action == "verifying"
    # result 已经写了
    assert task.result == "did the thing"


async def test_verify_passed_marks_done(tmp_store: TaskStore) -> None:
    t = await tmp_store.publish(
        topic="d", payload="p", from_model="t",
        acceptance={"criteria": ["ok"], "verifier": "claude"},
    )
    await tmp_store.claim(topic="d", worker="w")
    await tmp_store.complete(task_id=t.task_id, worker="w", result="done")
    task, action = await tmp_store.verify(
        task_id=t.task_id, verifier="claude", passed=True, score=0.95, issues=""
    )
    assert task is not None
    assert task.status == "done"
    assert action == "verified"
    assert len(task.verify_history) == 1
    assert task.verify_history[0]["passed"] is True


async def test_verify_failed_with_retry_resets_to_pending(tmp_store: TaskStore) -> None:
    t = await tmp_store.publish(
        topic="d", payload="p", from_model="t",
        acceptance={
            "criteria": ["ok"],
            "verifier": "claude",
            "max_iterations": 2,
            "auto_retry": True,
        },
    )
    await tmp_store.claim(topic="d", worker="w")
    await tmp_store.complete(task_id=t.task_id, worker="w", result="bad result")
    task, action = await tmp_store.verify(
        task_id=t.task_id, verifier="claude", passed=False, score=0.2,
        issues="tests failing",
    )
    assert task is not None
    assert task.status == "pending"  # 重置回 pending 重新认领
    assert action == "retry"
    assert task.retries == 1
    assert task.metadata.get("last_verify_issues") == "tests failing"


async def test_verify_failed_no_retry_marks_failed(tmp_store: TaskStore) -> None:
    t = await tmp_store.publish(
        topic="d", payload="p", from_model="t",
        acceptance={
            "criteria": ["ok"],
            "verifier": "claude",
            "max_iterations": 1,
            "auto_retry": False,  # 关键：关掉自动重试
        },
    )
    await tmp_store.claim(topic="d", worker="w")
    await tmp_store.complete(task_id=t.task_id, worker="w", result="bad")
    task, action = await tmp_store.verify(
        task_id=t.task_id, verifier="claude", passed=False, issues="no good",
    )
    assert task is not None
    assert task.status == "failed"
    assert action == "failed"
    assert "no good" in (task.error or "")


async def test_verify_reaches_max_iterations_then_failed(tmp_store: TaskStore) -> None:
    t = await tmp_store.publish(
        topic="d", payload="p", from_model="t",
        acceptance={"criteria": ["ok"], "max_iterations": 1, "auto_retry": True},
    )

    # 第 1 轮
    await tmp_store.claim(topic="d", worker="w1")
    await tmp_store.complete(task_id=t.task_id, worker="w1", result="r1")
    task, _ = await tmp_store.verify(task_id=t.task_id, verifier="v", passed=False, issues="i1")
    assert task.status == "pending"  # 回到 pending (retries=1)
    assert task.retries == 1

    # 第 2 轮——但 max_iterations=1 已经超过了，应该直接 failed
    await tmp_store.claim(topic="d", worker="w2")
    await tmp_store.complete(task_id=t.task_id, worker="w2", result="r2")
    task, action = await tmp_store.verify(task_id=t.task_id, verifier="v", passed=False, issues="i2")
    assert task.status == "failed"
    assert action == "failed"


async def test_verify_on_non_verifying_returns_error(tmp_store: TaskStore) -> None:
    t = await tmp_store.publish(topic="d", payload="p", from_model="t")
    await tmp_store.claim(topic="d", worker="w")
    await tmp_store.complete(task_id=t.task_id, worker="w", result="ok")
    # task 是 done 状态（没 acceptance），verify 应该拒绝
    task, action = await tmp_store.verify(task_id=t.task_id, verifier="v", passed=True)
    assert task is None
    assert action == "not_in_verifying"


# ---------- v2: webhook 通知 ----------

async def test_webhook_fired_on_done(tmp_store: TaskStore, webhook: _WebhookCapture) -> None:
    t = await tmp_store.publish(topic="d", payload="p", from_model="t", webhook=webhook.url)
    await asyncio.sleep(0.2)
    await tmp_store.claim(topic="d", worker="w")
    await tmp_store.complete(task_id=t.task_id, worker="w", result="ok")
    await asyncio.sleep(0.3)
    events = [r.get("event") for r in webhook.received]
    assert "task.published" in events
    assert "task.done" in events


async def test_webhook_fired_on_verified(tmp_store: TaskStore, webhook: _WebhookCapture) -> None:
    t = await tmp_store.publish(
        topic="d", payload="p", from_model="t",
        acceptance={"criteria": ["ok"], "verifier": "v"},
        webhook=webhook.url,
    )
    await asyncio.sleep(0.2)
    await tmp_store.claim(topic="d", worker="w")
    await tmp_store.complete(task_id=t.task_id, worker="w", result="r")
    await tmp_store.verify(task_id=t.task_id, verifier="v", passed=True)
    await asyncio.sleep(0.3)
    events = [r.get("event") for r in webhook.received]
    assert "task.published" in events
    assert "task.completed_awaiting_verify" in events
    assert "task.verified" in events


async def test_webhook_fired_on_retry(tmp_store: TaskStore, webhook: _WebhookCapture) -> None:
    t = await tmp_store.publish(
        topic="d", payload="p", from_model="t",
        acceptance={"criteria": ["ok"], "verifier": "v", "auto_retry": True, "max_iterations": 2},
        webhook=webhook.url,
    )
    await asyncio.sleep(0.2)
    await tmp_store.claim(topic="d", worker="w")
    await tmp_store.complete(task_id=t.task_id, worker="w", result="r")
    await tmp_store.verify(task_id=t.task_id, verifier="v", passed=False, issues="nope")
    await asyncio.sleep(0.3)
    events = [r.get("event") for r in webhook.received]
    assert "task.verify_failed_will_retry" in events


# ---------- v2: SSE 订阅 ----------

async def test_sse_subscribe_receives_events(tmp_store: TaskStore) -> None:
    t = await tmp_store.publish(topic="d", payload="p", from_model="t")
    await asyncio.sleep(0.05)
    q = tmp_store.subscribe(t.task_id)

    # 触发一个事件
    await tmp_store.claim(topic="d", worker="w")
    await tmp_store.complete(task_id=t.task_id, worker="w", result="ok")
    await asyncio.sleep(0.05)

    # 拿一个事件
    event = await asyncio.wait_for(q.get(), timeout=1)
    assert event["event"] == "task.done"
    assert event["task_id"] == t.task_id
    tmp_store.unsubscribe(t.task_id, q)


# ---------- 配置加载测试 ----------

def test_settings_default() -> None:
    from mcp_hub.config import HubSettings
    s = HubSettings()  # type: ignore[call-arg]
    assert s.hub_log_level == "INFO"
    assert s.hub_max_concurrent_subagents == 0  # 默认不限


def test_model_aliases_default() -> None:
    from mcp_hub.config import HubSettings
    s = HubSettings()  # type: ignore[call-arg]
    aliases = s.model_aliases()
    names = {a.alias for a in aliases}

    # 如果用户通过 .env 注入了 HUB_MODEL_ALIASES_JSON，只校验解析成功且非空；
    # 没有注入时才校验内置默认别名集合，避免 .env 自定义列表导致误失败。
    if s.hub_model_aliases_json:
        assert aliases
        assert all(a.alias and a.candidates for a in aliases)
        return

    assert {"deepseek", "minimax", "claude", "kimi", "gpt"} <= names
    # minimax 第一个 candidate 应该是 opencode-go/minimax-m2.7 或更高级
    m = next(a for a in aliases if a.alias == "minimax")
    assert "opencode-go/minimax-m2.7" in m.candidates or "opencode-go/minimax-m3" in m.candidates
