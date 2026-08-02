"""模型分工路由 —— 根据任务类型和优先级推荐合适的 runtime + model（关键词规则部分）。"""

from __future__ import annotations

from typing import Any

# ---------- 模型分工路由表 ----------
# 根据任务类型和优先级（fast / balanced / quality）推荐合适的 runtime + model。
# 这些推荐基于当前 hub 里实际可用的 runtime 和模型能力特点。
_MODEL_ROUTES: dict[str, Any] = {
    "scenarios": {
        "chinese_long_context": {
            "name": "中文长文本 / 总结 / 翻译 / 润色",
            "reason": "Kimi 中文和长上下文强；Gemini Flash 便宜且快",
            "models": {
                "quality": ("kimi", "kimi-code/kimi-for-coding"),
                "balanced": ("kimi", "kimi-code/kimi-for-coding"),
                "fast": ("antigravity", "gemini-3.6-flash-low"),
            },
        },
        "coding": {
            "name": "代码生成 / 重构 / 调试 / Code Review",
            "reason": "Gemini Flash 速度快且擅长多模态 coding；GPT 5.6 Sol 负责复杂代码/兜底",
            "models": {
                "quality": ("codex", "gpt-5.6-sol"),
                "balanced": ("antigravity", "gemini-3.6-flash-medium"),
                "fast": ("antigravity", "gemini-3.6-flash-low"),
            },
        },
        "complex_reasoning": {
            "name": "复杂推理 / 数学 / 算法 / 深度分析",
            "reason": "GPT 5.6 Sol 推理深度极强，适合复杂任务和兜底；Go 套餐 DeepSeek Flash 作为快档",
            "models": {
                "quality": ("codex", "gpt-5.6-sol"),
                "balanced": ("codex", "gpt-5.6-sol"),
                "fast": ("opencode", "opencode-go/deepseek-v4-flash"),
            },
        },
        "vision": {
            "name": "图像 / 视频 / 截图 / 多模态理解",
            "reason": "Gemini 原生多模态能力强，三档速度可选",
            "models": {
                "quality": ("antigravity", "gemini-3.6-flash-high"),
                "balanced": ("antigravity", "gemini-3.6-flash-medium"),
                "fast": ("antigravity", "gemini-3.6-flash-low"),
            },
        },
        "quick_chat": {
            "name": "快速闲聊 / 简单问答 / 一句话任务",
            "reason": "轻量模型响应快、成本低",
            "models": {
                "quality": ("antigravity", "gemini-3.6-flash-medium"),
                "balanced": ("antigravity", "gemini-3.6-flash-low"),
                "fast": ("codex", "gpt-5.6-luna"),
            },
        },
        "deepseek": {
            "name": "DeepSeek 通用任务（优先 Go 套餐）",
            "reason": "Go 套餐的 deepseek-v4-pro/flash 能力强；free 只在极小任务用",
            "models": {
                "quality": ("opencode", "opencode-go/deepseek-v4-pro"),
                "balanced": ("opencode", "opencode-go/deepseek-v4-flash"),
                "fast": ("opencode", "opencode/deepseek-v4-flash-free"),
            },
        },
        "batch_cheap": {
            "name": "批量 / 低成本 / 兜底任务",
            "reason": "OpenCode 免费模型量大管饱，只做特小任务",
            "models": {
                "quality": ("opencode", "opencode/deepseek-v4-flash-free"),
                "balanced": ("opencode", "opencode/deepseek-v4-flash-free"),
                "fast": ("opencode", "opencode/deepseek-v4-flash-free"),
            },
        },
        "general": {
            "name": "通用任务",
            "reason": "默认选均衡的 Gemini Flash Medium",
            "models": {
                "quality": ("antigravity", "gemini-3.6-flash-high"),
                "balanced": ("antigravity", "gemini-3.6-flash-medium"),
                "fast": ("antigravity", "gemini-3.6-flash-low"),
            },
        },
    },
    "keywords": [
        # 用户显式指定模型/系列时优先遵循
        (["deepseek", "ds", "深度求索"], "deepseek"),
        (["总结", "摘要", "长文", "文档", "翻译", "润色", "中文", "概括"], "chinese_long_context"),
        # 编程类放最前，避免“快速排序”被“快速”误抢到 quick_chat
        (["代码", "编程", "重构", "debug", "调试", "函数", "类", "bug", "写个", "实现", "review", "cr", "commit", "排序", "leetcode", "数据结构", "接口", "api"], "coding"),
        # 视觉类优先于通用“分析”
        (["图片", "图像", "视频", "看图", "vision", "截图", "照片", "屏幕"], "vision"),
        (["数学", "算法", "推理", "证明", "复杂", "分析", "推导", "逻辑"], "complex_reasoning"),
        (["快", "简单", "一句话", "简要", "简短"], "quick_chat"),
        (["批量", "便宜", "免费", "兜底", "大量"], "batch_cheap"),
    ],
}


def _classify_task(task: str) -> str:
    """根据任务描述的关键词匹配到分工场景。"""
    task_lower = task.lower()
    for keywords, scenario in _MODEL_ROUTES["keywords"]:
        if any(kw in task_lower for kw in keywords):
            return scenario
    return "general"


_ROUTER_SYSTEM = "你是 MCP Hub 的模型路由专家。你的唯一职责是根据用户任务，从给定列表中选择最合适的 runtime + model，并返回严格格式的 JSON。不要解释、不要生成任务内容、不要添加任何额外文字。"

_ROUTER_USER = """请从以下 runtime 中为任务选择最合适的 runtime + model：

- antigravity: gemini-3.6-flash-high / gemini-3.6-flash-medium / gemini-3.6-flash-low （Google Gemini，速度快，原生多模态，适合 coding/看图/快速问答）
- codex: gpt-5.6-sol / gpt-5.6-terra / gpt-5.6-luna （OpenAI Codex，gpt-5.6-sol 推理极强，适合复杂算法/架构/兜底）
- claude: opus / sonnet / haiku / botcf-claude/claude-opus-5 / botcf-claude/claude-opus-4-6 / botcf-claude/claude-fable-5 （Claude Code；botcf-claude 需 `😡Claude-Max` 分组 key，baseURL `https://botcf.com`）
- opencode: opencode-go/deepseek-v4-pro / opencode-go/deepseek-v4-flash / opencode/deepseek-v4-flash-free / qwen/qwen3.7-plus （OpenCode 平台；Go 套餐付费模型优先，free 只用于明确的“免费/批量/兜底”任务）
- qoder: Qwen3.8-Max-Preview / Qwen3.7-Max / Qwen3.7-Plus / DeepSeek-V4-Pro / DeepSeek-V4-Flash / GLM-5.2 / Kimi-K2.7-Code / MiniMax-M2.7 （Qoder CN CLI）
- kimi: kimi-code/kimi-for-coding / kimi-code/kimi-for-coding-highspeed （Kimi Code，中文和长文本强）

选择规则：
1. coding 任务优先 Gemini Flash（antigravity）。
2. 复杂推理/复杂代码/兜底优先 gpt-5.6-sol（codex）。
3. 提到 deepseek/ds/深度求索 的任务必须用 opencode-go/deepseek-v4-*，不要用 free，除非任务明确说“免费/批量/兜底”。
4. 中文长文本/总结/翻译优先 kimi。
5. 图像/视频/截图优先 Gemini。
6. fast 档也在高质量模型里选，不要用 free 凑数。

任务：{task}
优先级：{priority}

必须只返回如下格式的一个 JSON 对象（不要 markdown、不要思考过程、不要多余文字）：
{{"runtime": "antigravity", "model": "gemini-3.6-flash-medium", "reason": "简短理由"}}
"""


def _fallback_recommend_model(task: str, priority: str = "balanced") -> dict[str, Any]:
    """关键词匹配兜底：LLM 路由失败时用。"""
    scenario_key = _classify_task(task)
    scenario = _MODEL_ROUTES["scenarios"].get(scenario_key, _MODEL_ROUTES["scenarios"]["general"])
    priority = priority if priority in ("fast", "balanced", "quality") else "balanced"
    runtime, model = scenario["models"][priority]
    return {
        "scenario": scenario_key,
        "scenario_name": scenario["name"],
        "priority": priority,
        "runtime": runtime,
        "model": model,
        "reason": f"[兜底规则] {scenario.get('reason', '')}",
    }
