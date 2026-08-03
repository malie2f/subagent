"""MCP Hub —— 让多个 AI 编程工具互相调用、互相派活。

设计目标：
  - 一个 FastMCP server，对外暴露 5 类工具：
      1) call_model        直接同步调用任意一个模型
      2) publish_task      发布异步任务到队列
      3) claim_task        认领一个待处理任务
      4) complete_task     上报任务完成结果
      5) status            查询队列和模型状态

  - 一个文件队列做轻量持久化，无需 Redis / Postgres
  - 每个模型一个 adapter，统一暴露 chat() 接口
  - 任何 MCP 客户端（Claude Code / Codex / Kimi Code / OpenCode / Mavis）
    都可以接进来，把别的模型当工具用
"""

__version__ = "0.1.0"

# --- Windows: 所有子进程默认不弹控制台窗口 ---------------------------------
# hub/dashboard 以无控制台方式常驻（开机 vbs 隐藏启动 + cli 用
# DETACHED_PROCESS|CREATE_NO_WINDOW 拉起）。父进程没有控制台时，无论用
# asyncio.create_subprocess_exec 还是 subprocess.run/Popen 拉起 console
# 子系统的子进程（opencode.exe / codex / claude --help / netstat …），
# Windows 都会给它新建一个可见的空控制台窗口——detect_all 一次探测就是
# 十几路，浏览器开着 dashboard 时每 60s TTL 到期又来一波，直接刷屏。
# 这里在包导入时统一给两条入口包一层，注入 CREATE_NO_WINDOW
# （调用方显式传了 creationflags 则尊重调用方，如 cli._spawn_detached）。
import sys as _sys

if _sys.platform == "win32":
    import asyncio as _asyncio
    import subprocess as _subprocess

    _orig_create_subprocess_exec = _asyncio.create_subprocess_exec

    async def _create_subprocess_exec_no_window(*args, **kwargs):
        kwargs.setdefault("creationflags", _subprocess.CREATE_NO_WINDOW)
        return await _orig_create_subprocess_exec(*args, **kwargs)

    _asyncio.create_subprocess_exec = _create_subprocess_exec_no_window

    # 同步侧：subprocess.run / call / check_output 最终都走 Popen.__init__，
    # 在这一个收口注入即可全覆盖。
    _orig_popen_init = _subprocess.Popen.__init__

    def _popen_init_no_window(self, *args, **kwargs):
        kwargs.setdefault("creationflags", _subprocess.CREATE_NO_WINDOW)
        _orig_popen_init(self, *args, **kwargs)

    _subprocess.Popen.__init__ = _popen_init_no_window
# ---------------------------------------------------------------------------
