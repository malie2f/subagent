"""子 agent 失败回退策略 —— 容量不足/超时类失败判定与回退模型选择。"""

from __future__ import annotations

from .runtimes.base import SubagentResult

# ---------- Antigravity Claude 容量不足自动回退 ----------

# Antigravity 后端对 Claude 模型容量不足时的典型提示/错误码
_ANTIGRAVITY_CAPACITY_SIGNALS = (
    "high traffic",
    "no capacity available",
    "unavailable (code 503)",
    "capacity",
)

# 回退目标：优先 Gemini 3.6 flash medium，其次 3.6 flash high
_ANTIGRAVITY_FALLBACK_MODELS = (
    "gemini-3.6-flash-medium",
    "gemini-3.6-flash-high",
    "gemini-3.5-flash-medium",
)


def _is_antigravity_capacity_error(result: SubagentResult) -> bool:
    """判断 Antigravity 子 agent 失败是否因为 Claude 模型服务端容量不足或超时。"""
    if result.exit_code == 0:
        return False
    # 显式超时也触发回退（Claude 模型高负载时常见）
    if result.error == "timeout":
        return True
    combined = f"{result.stderr or ''}\n{result.error or ''}\n{result.summary or ''}".lower()
    return any(sig in combined for sig in _ANTIGRAVITY_CAPACITY_SIGNALS)


def _pick_antigravity_fallback(requested_model: str) -> str | None:
    """为 Antigravity Claude 模型选一个 Gemini 回退模型。"""
    # 如果用户本来就选的 Gemini，不需要再回退
    if requested_model.startswith("gemini-"):
        return None
    return _ANTIGRAVITY_FALLBACK_MODELS[0]


# ---------- 通用兜底：失败时回退到 GPT 5.6 Sol ----------

_SOL_FALLBACK_SIGNALS = (
    "timeout",
    "rate limit",
    "too many requests",
    "unavailable",
    "capacity",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "503",
    "502",
    "500",
    "high traffic",
    "no capacity available",
)


def _is_fallback_to_sol_error(result: SubagentResult) -> bool:
    """判断子 agent 失败是否适合用 GPT 5.6 Sol 兜底重试。"""
    if result.exit_code == 0:
        return False
    if result.error == "timeout":
        return True
    combined = f"{result.stderr or ''}\n{result.error or ''}\n{result.summary or ''}".lower()
    return any(sig in combined for sig in _SOL_FALLBACK_SIGNALS)


# botcf-claude 主通道超时后的稳定通道映射。
# stable 通道没有 claude-fable-5，所以 fable-5 只在主通道重试，不切稳定通道。
# grok-4.5 无保底（用户明确要求：超时就超时）。
_BOTCF_STABLE_MAP = {
    "botcf-claude/claude-opus-5": "botcf-claude-stable/claude-opus-5",
    "botcf-claude/claude-opus-4-6": "botcf-claude-stable/claude-opus-4-6",
}


def _is_timeout_error(result: SubagentResult) -> bool:
    """判断是否超时类失败（用于 botcf-claude 重试决策）。"""
    if result.exit_code == 0:
        return False
    if result.error == "timeout":
        return True
    combined = f"{result.stderr or ''}\n{result.error or ''}".lower()
    return "timeout" in combined or "timed out" in combined
