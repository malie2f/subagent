"""Dashboard —— mcp-hub 的可观测性面板。

两种使用方式：

1. **Web dashboard**（主推）：
       python -m mcp_hub.dashboard
   起 Flask 服务在 http://127.0.0.1:8766
   一个页面 4 个 tab：Overview / Subagents / Cluster / Tasks
   短轮询 2s 拉数据，不依赖 SSE

2. **CLI**（辅助）：
       python -m mcp_hub.dashboard status      # 一屏概览
       python -m mcp_hub.dashboard subagent <id>   # 单个 subagent 详情
       python -m mcp_hub.dashboard watch cluster    # 实时看 cluster
       python -m mcp_hub.dashboard log <id>         # tail 子 agent 日志

设计原则：
- **完全独立进程**，不跟 mcp-hub server 共享内存
- 只读不改：读 .env / 读 TaskStore JSON / 读子 agent log
- 不动 mcp-hub 本身代码（确保 mcp-hub server 跑着时 dashboard 也能独立启）
"""

from __future__ import annotations

from .server import create_app, main

__all__ = ["create_app", "main"]
