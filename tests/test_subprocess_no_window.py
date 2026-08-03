"""Windows 子进程 CREATE_NO_WINDOW 注入补丁的测试。

背景：hub/dashboard 以无控制台方式常驻，子进程（探测命令、子代理 CLI）
默认会被 Windows 新建可见空控制台窗口。__init__.py 在 win32 下给
asyncio.create_subprocess_exec 和 subprocess.Popen.__init__ 统一注入
CREATE_NO_WINDOW（显式传 creationflags 的调用方不受影响）。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

import mcp_hub

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 有此补丁")


def test_popen_init_injects_no_window(monkeypatch):
    calls: list[dict] = []

    def fake_init(self, *args, **kwargs):
        self._child_created = False  # 避免半初始化的 Popen 在 __del__ 里炸
        calls.append(kwargs)

    monkeypatch.setattr(mcp_hub, "_orig_popen_init", fake_init)
    subprocess.Popen(["whatever", "--version"])
    assert calls[0]["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_popen_init_respects_explicit_flags(monkeypatch):
    calls: list[dict] = []

    def fake_init(self, *args, **kwargs):
        self._child_created = False
        calls.append(kwargs)

    monkeypatch.setattr(mcp_hub, "_orig_popen_init", fake_init)
    # cli._spawn_detached 会显式传 DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW
    explicit = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(["whatever"], creationflags=explicit)
    assert calls[0]["creationflags"] == explicit


def test_real_subprocess_run_still_works():
    # 补丁不能让真实 spawn 挂掉（CREATE_NO_WINDOW 不影响管道捕获）
    r = subprocess.run(
        [sys.executable, "-c", "print('ok')"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "ok"


def test_asyncio_exec_wrapped():
    # asyncio 入口也被包过（函数对象已被替换）
    assert asyncio.create_subprocess_exec is not mcp_hub._orig_create_subprocess_exec


def test_asyncio_real_exec_still_works():
    async def run():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "print('ok')",
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return proc.returncode, out

    code, out = asyncio.run(run())
    assert code == 0
    assert out.strip() == b"ok"
