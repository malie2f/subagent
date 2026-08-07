"""P0 修复测试 —— 子 agent 死必报 / 死可查 / 死可续 + 猝死诊断。

覆盖：
  - registry 终态增厚（exit_code / exit_code_source / 现场字段 round-trip）
  - _extract_peak_tokens（step-finish 两种拼写，取最大）
  - _pid_exit_code（活进程 STILL_ACTIVE → None；刚死的进程尽力拿真码）
  - _poll_orphan_subagent E2E（真子进程退出 3 → registry 落 dead + 现场）
  - notify.post_webhook / valid_webhook_url
  - _detect_caller 读 clientInfo.name（含 "mcp" 无信息 fall-through）
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_hub.config import HubSettings
from mcp_hub import registry as reg
from mcp_hub.notify import post_webhook, valid_webhook_url
from mcp_hub.runtimes.base import SubagentHandle, SubagentResult


# ---------- fixtures ----------

@pytest.fixture
def hub_reg(tmp_path: Path) -> Path:
    """用临时 data 目录初始化 registry（每个用例独立 registry 文件）。"""
    settings = HubSettings(hub_queue_path=str(tmp_path / "tasks.json"))  # type: ignore[call-arg]
    logger = logging.getLogger("test-registry")
    subagents: dict[str, SubagentHandle] = {}
    results: dict[str, SubagentResult] = {}
    reg.init_registry(
        settings=settings,
        logger=logger,
        subagents=subagents,
        subagent_results=results,
        runtimes={},
    )
    return tmp_path


def _make_handle(
    task_id: str,
    pid: int = 0,
    output_file: Path | None = None,
    prompt: str = "测试任务",
) -> SubagentHandle:
    return SubagentHandle(
        pid=pid or None,
        runtime="opencode",
        model="zen-v4f/deepseek-v4-flash-free",
        task_id=task_id,
        workdir=".",
        started_at=time.time() - 1,
        process=None,
        output_file=output_file,
        prompt=prompt,
    )


def _load_registry(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "subagents_registry.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------- registry 终态增厚 ----------

def test_registry_mark_roundtrip(hub_reg: Path) -> None:
    h = _make_handle("t1")
    reg._registry_register(h, "kimicode", "high", webhook="http://127.0.0.1:9/hook")
    res = SubagentResult(
        runtime=h.runtime, model=h.model, task_id="t1",
        exit_code=3, stdout="", stderr="boom\n" * 500, duration_sec=12.34,
        summary="挂了" * 600, error=None, prompt=h.prompt,
        session_id="conv-xyz-123",
    )
    reg._registry_mark("t1", "dead", result=res, stats={"peak_rss_mb": 812.345, "peak_tokens": 200684})

    entry = _load_registry(hub_reg)["subagents"]["t1"]
    assert entry["status"] == "dead"
    assert entry["exit_code"] == 3
    assert entry["exit_code_source"] == "real"
    assert entry["duration_sec"] == 12.3
    assert entry["peak_rss_mb"] == 812.3
    assert entry["peak_tokens"] == 200684
    assert entry["session_id"] == "conv-xyz-123"
    assert entry["webhook"] == "http://127.0.0.1:9/hook"
    assert entry["caller"] == "kimicode"
    # 截断：summary 留尾 1000 字符、stderr 留尾 2000 字符
    assert len(entry["summary"]) == 1000
    assert len(entry["stderr_tail"]) == 2000
    assert entry["finished_at"] > 0


def test_registry_mark_exit_code_none_is_unknown(hub_reg: Path) -> None:
    h = _make_handle("t2")
    reg._registry_register(h, "unknown")
    res = SubagentResult(
        runtime=h.runtime, model=h.model, task_id="t2",
        exit_code=None, stdout="", stderr="", duration_sec=1.0,
        summary="", error=None, prompt="",
    )
    reg._registry_mark("t2", "dead", result=res)
    entry = _load_registry(hub_reg)["subagents"]["t2"]
    assert entry["status"] == "dead"
    assert entry["exit_code"] is None
    assert entry["exit_code_source"] == "unknown"


def test_registry_register_resumed_from(hub_reg: Path) -> None:
    h = _make_handle("t3")
    reg._registry_register(h, "claude", resumed_from="deadbeef01")
    entry = _load_registry(hub_reg)["subagents"]["t3"]
    assert entry["resumed_from"] == "deadbeef01"


# ---------- _extract_peak_tokens ----------

def test_extract_peak_tokens_picks_max() -> None:
    text = (
        '{"type":"step-finish","part":{"tokens":{"total":150000,"input":1}}}\n'
        '{"type":"step-finish","tokens":{"total":200684,"input":2,"output":3}}\n'
        '{"type":"step_finish","tokens":{"total":180000}}\n'
    )
    assert reg._extract_peak_tokens(text) == 200684


def test_extract_peak_tokens_none_when_absent() -> None:
    assert reg._extract_peak_tokens('{"type":"step-start"}\nhello') is None
    assert reg._extract_peak_tokens("") is None


# ---------- _pid_exit_code ----------

def test_pid_exit_code_live_process_is_none() -> None:
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert reg._pid_exit_code(p.pid) is None  # STILL_ACTIVE 不当退出码
    finally:
        p.kill()
        p.wait()


def test_pid_exit_code_dead_process_best_effort() -> None:
    p = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(3)"])
    p.wait()
    # Popen 对象还活着 → 内核对象未回收，Windows 上应拿到 3；拿不到也不谎报
    code = reg._pid_exit_code(p.pid)
    assert code in (3, None)


# ---------- 孤儿轮询 E2E（死必报核心路径）----------

async def test_poll_orphan_records_death_scene(hub_reg: Path) -> None:
    log = hub_reg / "orphan.log"
    log.write_text('{"type":"step-finish","tokens":{"total":12345}}\n{"type":"error","msg":"boom"}\n', encoding="utf-8")
    p = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(3)"])
    p.wait()  # 先死透，轮询第一轮就收口（不等 5s）

    h = _make_handle("orphan1", pid=p.pid, output_file=log)
    h.started_at = time.time() - 2
    reg._subagents[h.task_id] = h
    reg._registry_register(h, "kimicode")
    await reg._poll_orphan_subagent(h)

    entry = _load_registry(hub_reg)["subagents"]["orphan1"]
    assert entry["status"] == "dead"
    assert entry["exit_code"] in (3, None)  # 尽力拿真码，拿不到标 unknown
    assert entry["exit_code_source"] == ("real" if entry["exit_code"] is not None else "unknown")
    assert entry["peak_tokens"] == 12345
    assert "boom" in entry["summary"]
    assert entry["duration_sec"] >= 0
    # 结果也进了共享内存表
    assert reg._subagent_results["orphan1"].exit_code == entry["exit_code"]


# ---------- notify ----------

def test_valid_webhook_url() -> None:
    assert valid_webhook_url("http://127.0.0.1:8765/hook")
    assert valid_webhook_url("https://example.com/hook")
    assert not valid_webhook_url("ftp://x")
    assert not valid_webhook_url("")
    assert not valid_webhook_url("notaurl")


async def test_post_webhook_delivers_json() -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import socket
    import threading

    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(length) or b"{}"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *a, **k):  # noqa: ANN002, ANN003
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = HTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        status = await post_webhook(f"http://127.0.0.1:{port}/hook", {"event": "subagent.failed", "task_id": "x"})
        assert status == 200
        assert received and received[0]["event"] == "subagent.failed"
    finally:
        srv.shutdown()
        srv.server_close()


# ---------- _detect_caller 读 clientInfo ----------

def _fake_ctx(client_name: str | None) -> Any:
    client_info = SimpleNamespace(name=client_name) if client_name is not None else None
    client_params = SimpleNamespace(clientInfo=client_info) if client_info is not None else None
    session = SimpleNamespace(client_params=client_params)
    req_ctx = SimpleNamespace(session=session, request=None)
    return SimpleNamespace(request_context=req_ctx)


def test_detect_caller_client_info_hit() -> None:
    import mcp_hub.server as hub_server

    assert hub_server._detect_caller(_fake_ctx("kimi-code"), "unknown") == "kimicode"
    assert hub_server._detect_caller(_fake_ctx("claude-code"), "unknown") == "claude"
    assert hub_server._detect_caller(_fake_ctx("opencode"), "unknown") == "opencode"


def test_detect_caller_client_info_unknown_name_passthrough() -> None:
    import mcp_hub.server as hub_server

    # 未识别的非空名字原样返回（在 UA/父进程探测之前就收口，结果确定）
    assert hub_server._detect_caller(_fake_ctx("SomeNewClient"), "unknown") == "SomeNewClient"


def test_detect_caller_client_info_mcp_is_fallthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_hub.server as hub_server

    # "mcp" 是 python SDK 默认名，无信息，继续往后探。
    # 把父进程探测掐掉（psutil=None），避免测试机的进程命令行污染结果。
    monkeypatch.setattr(hub_server, "psutil", None)
    assert hub_server._detect_caller(_fake_ctx("mcp"), "unknown") == "unknown"


def test_detect_caller_client_info_stateless_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_hub.server as hub_server

    # stateless 会话 client_params=None：不炸，继续 fall-through
    monkeypatch.setattr(hub_server, "psutil", None)
    assert hub_server._detect_caller(_fake_ctx(None), "unknown") == "unknown"


def test_detect_caller_explicit_fallback_wins() -> None:
    import mcp_hub.server as hub_server

    assert hub_server._detect_caller(_fake_ctx("kimi-code"), "codex") == "codex"
