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
