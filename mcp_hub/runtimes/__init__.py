"""Runtimes —— 真实可执行的 AI 编程 CLI 适配器。

每个 adapter 负责：
  1) 检测是否安装
  2) spawn 一个子进程跑任务
  3) 捕获输出 + 退出码
  4) 可以 kill / 拿状态

已实现：
  - opencode     ✅  OpenCode 1.18.x
  - codebuddy    ✅  CodeBuddy Code
  - claude       ✅  Claude Code 2.1.x
  - kimi         ✅  Kimi Code 0.23.x
  - antigravity  ✅  Antigravity CLI 1.1.x（agy / antigravity）
  - grok         ✅  Grok Build 0.2.x（grok-4.5，美国服务需代理）
  - dsh          ✅  DeepSeek Harness 0.1.x（dsh --profile headless，官方 v4-pro）
  - zcode        🟡  ZCode 0.15.x（需先完成 zcode 登录/模型配置）
  - minimax      🟡  STUB（MiniMax Code 桌面 app 还没修好 CLI 入口）
"""

from __future__ import annotations

from .antigravity import AntigravityAdapter
from .base import RuntimeAdapter, SubagentHandle, SubagentResult
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .dsh import DshAdapter
from .grok import GrokAdapter
from .kimi import KimiAdapter
from .mavis import MavisAdapter
from .minimax import MinimaxAdapter
from .opencode import OpencodeAdapter
from .qoder import QoderAdapter
from .codebuddy import CodebuddyAdapter
from .zcode import ZcodeAdapter

REGISTRY = {
    "antigravity": AntigravityAdapter,
    "opencode": OpencodeAdapter,
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "dsh": DshAdapter,
    "grok": GrokAdapter,
    "codebuddy": CodebuddyAdapter,
    "kimi": KimiAdapter,
    "mavis": MavisAdapter,
    "minimax": MinimaxAdapter,
    "qoder": QoderAdapter,
    "zcode": ZcodeAdapter,
}


def detect_all() -> dict[str, RuntimeAdapter]:
    """检测所有可用的 runtime，返回 name -> adapter 字典。

    只返回 is_available() 为 True 的（minimax 是 stub 但 is_available 会探测 wrapper，
    当前 wrapper 存在但命令跑不通 —— 所以也返回，但跑起来会失败提示修复）。
    如果要严格只列"完全能用"的，可改成只收 wait() 探针成功的。
    """
    out: dict[str, RuntimeAdapter] = {}
    for name, cls in REGISTRY.items():
        try:
            a = cls()
            if a.is_available():
                out[name] = a
        except Exception:  # noqa: BLE001
            pass
    return out


__all__ = [
    "AntigravityAdapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "DshAdapter",
    "GrokAdapter",
    "KimiAdapter",
    "MavisAdapter",
    "MinimaxAdapter",
    "OpencodeAdapter",
    "QoderAdapter",
    "CodebuddyAdapter",
    "ZcodeAdapter",
    "REGISTRY",
    "RuntimeAdapter",
    "SubagentHandle",
    "SubagentResult",
    "detect_all",
]
