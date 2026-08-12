"""MCP Hub 主服务。

启动方式：
    stdio:    python -m mcp_hub.server
    sse:      python -m mcp_hub.server --transport sse --port 8765

被 Claude Code / Codex / Kimi Code / OpenCode / Mavis 当作 MCP server 接入后，
这些工具就可以：
    - 让 Claude 调用 Kimi（"用 kimi 总结一下这段代码"）
    - 让 Codex 发布任务给 Claude 处理
    - 让任意模型订阅某个 topic，等结果
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import Context
    from mcp import types as _mcp_types
except ImportError as e:  # noqa: BLE001
    print(
        "未安装 mcp 包，请先运行：pip install mcp",
        file=sys.stderr,
    )
    raise SystemExit(1) from e

# ---- 兼容层：有些 MCP 客户端会把 tools/call 的 arguments 传成 JSON 字符串 ----
# 这会导致 pydantic 校验失败，服务端返回 -32602 "Invalid request parameters"。
# 这里在 ClientRequest 解析前把字符串 arguments 预解析成 dict，容忍这类客户端。
_orig_client_request_validate = _mcp_types.ClientRequest.model_validate.__func__


@classmethod  # type: ignore[misc]
def _patched_client_request_validate(cls, obj, *args, **kwargs):
    if isinstance(obj, dict):
        params = obj.get("params")
        if isinstance(params, dict) and isinstance(params.get("arguments"), str):
            try:
                params = dict(params)
                params["arguments"] = json.loads(params["arguments"])
                obj = dict(obj)
                obj["params"] = params
            except json.JSONDecodeError:
                pass
    return _orig_client_request_validate(cls, obj, *args, **kwargs)


_mcp_types.ClientRequest.model_validate = _patched_client_request_validate

# ---- 错误富化：-32602 不再只回 "Invalid request parameters" + 空 data ----
# SDK 的 receive loop（mcp/shared/session.py）把请求校验失败、会话未初始化等
# 所有异常统一报成 -32602 + 固定 message + 空 data，调用方 agent 完全无法自查。
# 这里富化 ErrorData：message 追加真实原因（客户端一般只显示 message），
# data 附细节 + CALLING_SPEC §2 自查清单。借助 sys.exc_info() 拿 except 块里的活动异常。
from mcp.shared import session as _mcp_shared_session
from pydantic import ValidationError

_CALLING_SPEC_HINT = (
    "调用自查（mcp-hub/CALLING_SPEC.md §2）："
    "① 严格按工具 schema 传参，schema 没有的字段不要传；"
    "② 类型必须对（timeout_sec 是整数、wait 是布尔）；"
    "③ *_json 后缀参数（metadata_json/acceptance_json）传 JSON 字符串，不是对象；"
    "④ arguments 必须是对象，不能是 JSON 字符串；"
    "⑤ workdir 用绝对路径。"
)


def _describe_request_error(exc: BaseException) -> str:
    """把 receive loop 吞掉的异常翻成调用方能看懂的说明。"""
    text = str(exc)
    if isinstance(exc, RuntimeError) and "initialization" in text:
        return (
            "MCP 会话尚未完成 initialize 握手——通常是 hub 重启后客户端自动重连了 "
            "SSE 但没有重新 initialize。请重连 MCP 服务（或重启当前会话）后重试。"
        )
    if isinstance(exc, ValidationError):
        errs = exc.errors(include_url=False)
        # ClientRequest 是 union：校验失败时每个成员分支都报一条 method 不匹配，
        # 真正的错误在 method 匹配的那个分支里，优先展示该分支的字段错误。
        by_branch: dict[Any, list[dict[str, Any]]] = {}
        for e in errs:
            loc = e.get("loc", ())
            by_branch.setdefault(loc[0] if loc else "?", []).append(e)
        matched = [
            e
            for branch_errs in by_branch.values()
            if not any(x.get("loc", ())[-1:] == ("method",) for x in branch_errs)
            for e in branch_errs
        ]
        parts = []
        for err in (matched or errs)[:4]:
            loc = ".".join(str(x) for x in err.get("loc", ()))
            msg = err.get("msg")
            parts.append(f"{loc}: {msg}" if loc else str(msg))
        return f"请求结构校验失败（{'; '.join(parts)}）。"
    return f"{type(exc).__name__}: {text}"


_OrigErrorData = _mcp_types.ErrorData


class _EnrichedErrorData(_OrigErrorData):
    """只在 receive loop 的 -32602 兜底分支里富化 message/data，其余场景原样透传。"""

    def __init__(self, **kwargs: Any) -> None:
        if (
            kwargs.get("code") == _mcp_types.INVALID_PARAMS
            and kwargs.get("message") == "Invalid request parameters"
            and not kwargs.get("data")
        ):
            exc = sys.exc_info()[1]
            if exc is not None:
                reason = _describe_request_error(exc)
                kwargs["message"] = f"Invalid request parameters: {reason}"
                kwargs["data"] = f"{reason}\n{_CALLING_SPEC_HINT}"
        super().__init__(**kwargs)


_mcp_shared_session.ErrorData = _EnrichedErrorData

# ---- 容错：hub 重启后，已连接客户端重连 SSE 时经常不重新 initialize ----
# 会话卡在 NotInitialized，之后所有请求都被报成 -32602（上面的富化让原因可见）。
# hub 是本地工具代理，不依赖 per-session 能力协商（SDK 里 client_params 的访问
# 都有 None 保护），强制 stateless 让新会话创建即 Initialized，重连后直接用。
from mcp.server.session import ServerSession as _ServerSession

_orig_server_session_init = _ServerSession.__init__


def _patched_server_session_init(self, *args: Any, **kwargs: Any) -> None:
    if len(args) >= 4:
        args = (*args[:3], True, *args[4:])
    else:
        kwargs["stateless"] = True
    _orig_server_session_init(self, *args, **kwargs)


_ServerSession.__init__ = _patched_server_session_init  # type: ignore[method-assign]

# ---- 错误富化②：工具参数校验失败也带自查清单 ----
# 上面那条只管信封层 -32602；更常见的是参数到了工具层才被 pydantic 拒
# （类型不对、_json 参数传了对象）。FastMCP 的参数校验统一收口在
# FuncMetadata.call_fn_with_arg_validation，拦下 ValidationError，
# 把 CALLING_SPEC §2 自查清单附进错误文本，让调用方直接自愈。
from mcp.server.fastmcp.exceptions import ToolError as _ToolError
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata as _FuncMetadata

_orig_call_fn_with_validation = _FuncMetadata.call_fn_with_arg_validation


async def _patched_call_fn_with_validation(
    self, fn, fn_is_async, arguments_to_validate, arguments_to_pass_directly
):
    try:
        return await _orig_call_fn_with_validation(
            self, fn, fn_is_async, arguments_to_validate, arguments_to_pass_directly
        )
    except ValidationError as e:
        raise _ToolError(f"{e}\n\n{_CALLING_SPEC_HINT}") from e


_FuncMetadata.call_fn_with_arg_validation = _patched_call_fn_with_validation

try:
    import psutil
except ImportError:  # noqa: BLE001
    psutil = None  # type: ignore[assignment]

from pathlib import Path

from .config import HubSettings, ensure_queue_dir, load_settings
from .models import build_adapters
from .models.base import ChatRequest, Message
from .queue import TaskStore
from .runtimes import detect_all
from .runtimes.base import SubagentHandle, SubagentResult
from .tools import detect_all as detect_tools
from .tools.base import ToolResult
from .cluster import ClusterManager, PoolSpec
from .routing import _fallback_recommend_model, _ROUTER_SYSTEM, _ROUTER_USER
from .fallbacks import (
    _BOTCF_STABLE_MAP,
    _is_antigravity_capacity_error,
    _is_fallback_to_sol_error,
    _is_timeout_error,
    _pick_antigravity_fallback,
)
from .registry import (
    init_registry,
    _ensure_orphan_pollers,
    _fire_subagent_webhook,
    _handle_alive,
    _kill_pid,
    _pid_alive,
    _recover_orphan_subagents,
    _registry_load_unlocked,
    _registry_lock,
    _registry_mark,
    _registry_register,
    _sample_process_stats,
)


async def _recommend_model(task: str, priority: str = "balanced") -> dict[str, Any]:
    """让 LLM 根据任务描述推荐 runtime + model；LLM 失败时回退到关键词规则。"""
    _init()
    priority = priority if priority in ("fast", "balanced", "quality") else "balanced"

    # 优先用 minimax 直连模型做路由决策（当前唯一稳定有 key 的直连模型）
    adapter = _adapters.get("minimax")
    if adapter is None:
        return _fallback_recommend_model(task, priority)

    try:
        user_prompt = _ROUTER_USER.format(task=task, priority=priority)
        resp = await adapter.chat(
            ChatRequest(
                system=_ROUTER_SYSTEM,
                messages=[Message(role="user", content=user_prompt)],
                max_tokens=512,
                temperature=0.1,
            )
        )
        text = resp.text.strip()
        # 去掉可能的 <think>...</think>
        if "<think>" in text and "</think>" in text:
            text = text.split("</think>", 1)[-1].strip()
        # 尝试提取 JSON
        if "```json" in text:
            text = text.split("```json")[-1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[-1].split("```")[0].strip()
        # 只取第一个 { 到对应 } 的内容（简单匹配）
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"LLM 返回中找不到 JSON: {text[:200]}")
        text = text[start : end + 1]
        rec = json.loads(text)
        if not isinstance(rec, dict):
            raise ValueError("LLM 返回的不是 dict")
        runtime = rec.get("runtime")
        model = rec.get("model")
        reason = rec.get("reason", "")
        if not runtime or not model:
            raise ValueError("LLM 返回缺 runtime/model")
        # 校验 runtime 是否存在
        if runtime not in _runtimes:
            raise ValueError(f"LLM 返回未知 runtime: {runtime}")

        # 后处理：DeepSeek 任务不应被丢到 free 模型
        task_lower = task.lower()
        is_deepseek_task = any(k in task_lower for k in ("deepseek", "ds", "深度求索"))
        is_free_intent = any(k in task_lower for k in ("免费", "批量", "兜底", "free"))
        if is_deepseek_task and model == "opencode/deepseek-v4-flash-free" and not is_free_intent:
            model = "opencode-go/deepseek-v4-flash"
            reason = f"[强制修正] DeepSeek 任务优先使用 Go 套餐: {reason}"

        return {
            "scenario": "llm_routed",
            "scenario_name": "LLM 智能路由",
            "priority": priority,
            "runtime": runtime,
            "model": model,
            "reason": reason,
        }
    except Exception as e:  # noqa: BLE001
        if _logger is not None:
            _logger.warning("LLM 路由失败，回退到规则路由: %s", e)
        return _fallback_recommend_model(task, priority)


# ---------- 全局单例 ----------

_settings: HubSettings | None = None
_adapters: dict[str, Any] = {}
_store: TaskStore | None = None
_logger: logging.Logger | None = None
_runtimes: dict[str, Any] = {}
_tools: dict[str, Any] = {}
_aliases: list[Any] = []
_subagents: dict[str, SubagentHandle] = {}  # task_id -> handle
_subagent_results: dict[str, SubagentResult] = {}  # task_id -> result
_subagent_sem: asyncio.Semaphore | None = None  # 并发控制（None = 不限）
_cluster: ClusterManager | None = None


def _setup_logging(level: str) -> logging.Logger:
    log = logging.getLogger("mcp-hub")
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not log.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        log.addHandler(h)
    return log


def _init() -> None:
    """延迟初始化：第一次调用工具时执行。"""
    global _settings, _adapters, _store, _logger, _runtimes, _tools, _aliases, _subagent_sem, _cluster
    if _settings is not None:
        return
    _settings = load_settings()
    _logger = _setup_logging(_settings.hub_log_level)
    _store = TaskStore(ensure_queue_dir(_settings.hub_queue_path))
    _adapters = build_adapters(_settings.model_configs())
    _runtimes = detect_all()
    _tools = detect_tools()
    # mmx 不认环境变量 key，单独注入：优先 .env 里的 MMX_API_KEY，否则用 MINIMAX_API_KEY
    from .tools.mmx import MmxAdapter
    if "mmx" in _tools:
        import os
        mmx_key = os.environ.get("MMX_API_KEY") or _settings.minimax_api_key
        if mmx_key:
            _tools["mmx"] = MmxAdapter(api_key=mmx_key)
            _logger.info("mmx 注入 api_key 成功（len=%d）", len(mmx_key))
        else:
            _logger.warning("mmx 没拿到 api_key，调用都会失败（需要 MMX_API_KEY 或 MINIMAX_API_KEY）")
    # hedge-gateway：注入 .env 的 key/base_url（没有 key 网关回 401）
    from .tools.hedge import HedgeAdapter
    if "hedge" in _tools:
        _tools["hedge"] = HedgeAdapter(
            base_url=_settings.hedge_base_url,
            api_key=_settings.hedge_api_key,
        )
        if _settings.hedge_api_key:
            _logger.info("hedge 注入 api_key 成功（len=%d）", len(_settings.hedge_api_key))
        else:
            _logger.warning("hedge 没拿到 api_key（HEDGE_API_KEY），调用会 401")
    _aliases = _settings.model_aliases()

    # 并发信号量：0 = 不限
    if _settings.hub_max_concurrent_subagents > 0:
        _subagent_sem = asyncio.Semaphore(_settings.hub_max_concurrent_subagents)
    else:
        _subagent_sem = None  # 不限

    # Cluster（可选，多 pool 架构）
    pool_spec_dicts = _settings.cluster_pool_specs()
    pool_specs: list[PoolSpec] = [PoolSpec.from_dict(d) for d in pool_spec_dicts]
    _cluster = ClusterManager(
        specs=pool_specs,
        store=_store,
        runtimes=_runtimes,
    )

    _logger.info(
        "mcp-hub 启动：%d 个模型在线，%d 个 runtime 可用，%d 个 tool 可用，cluster=%s，队列=%s，并发=%s",
        len(_adapters),
        len(_runtimes),
        len(_tools),
        f"{len(pool_specs)} pools" if pool_specs else "off",
        _settings.hub_queue_path,
        "unlimited" if _subagent_sem is None else str(_settings.hub_max_concurrent_subagents),
    )
    for n, a in _adapters.items():
        _logger.info("  - adapter %-10s %s @ %s", n, a.config.model, a.config.base_url)
    for n, r in _runtimes.items():
        _logger.info("  - runtime  %-10s binary=%s", n, r.binary)
    for n, t in _tools.items():
        _logger.info("  - tool     %-10s ops=%s", n, t.list_operations())
    for spec in pool_specs:
        _logger.info(
            "  - cluster  pool=%-10s size=%d runtime=%s model=%s topic=%s workdir=%s",
            spec.name, spec.size, spec.runtime, spec.model, spec.topic, spec.workdir,
        )

    # 装配 registry 模块：注入 settings/logger/子 agent 表（dict 为共享引用）
    init_registry(
        settings=_settings,
        logger=_logger,
        subagents=_subagents,
        subagent_results=_subagent_results,
        runtimes=_runtimes,
    )

    # 从 registry 恢复上次 hub 进程留下的孤儿子 agent（pid 还活着的重新盯上）
    _recover_orphan_subagents()


# ---------- 调用方平台识别 ----------

# 各 MCP 客户端常见的 User-Agent / 进程名关键字（小写）
_CALLER_KEYWORDS: dict[str, list[str]] = {
    "codex": ["codex"],
    "kimicode": ["kimi-code", "kimi code", "kimi"],
    "claude": ["claude-code", "claude code", "claude"],
    "opencode": ["opencode"],
    "minimax": ["minimax", "mavis"],
    "zcode": ["zcode"],
    "botcf": ["botcf"],
}


def _detect_caller(ctx: Context | None, fallback: str = "unknown") -> str:
    """根据 MCP 请求上下文或父进程识别调用方平台。

    优先级：
      1. fallback 本身已是可识别平台名（非 unknown / 用户）
      2. MCP 握手 clientInfo.name（最直接；stateless 会话可能拿不到）
      3. SSE/HTTP 请求的 User-Agent 头
      4. stdio 模式下父进程名
      5. 环境变量 hint（KIMI_CODE / CODEX / CLAUDE_CODE / OPENCODE）
      6. 返回 fallback
    """
    # 1. 如果调用方已经显式署名且不是占位符，直接尊重
    if fallback and fallback not in ("unknown", "用户", "user", ""):
        return fallback

    # 2. MCP 初始化握手的 clientInfo.name
    if ctx is not None:
        try:
            req_ctx = ctx.request_context
            session = getattr(req_ctx, "session", None) if req_ctx is not None else None
            client_params = getattr(session, "client_params", None) if session is not None else None
            client_info = getattr(client_params, "clientInfo", None) if client_params is not None else None
            ci_name = (getattr(client_info, "name", "") or "").strip()
            if ci_name and ci_name.lower() != "mcp":  # python SDK 默认名，无信息
                ci_lower = ci_name.lower()
                for platform, keywords in _CALLER_KEYWORDS.items():
                    if any(kw in ci_lower for kw in keywords):
                        return platform
                if _logger is not None:
                    _logger.info("_detect_caller: 未识别的 clientInfo.name=%r，原样采用", ci_name)
                return ci_name
        except Exception:
            pass

    # 3. SSE / StreamableHTTP：从 ASGI request 取 User-Agent
    if ctx is not None:
        try:
            req_ctx = ctx.request_context
            if req_ctx is not None and req_ctx.request is not None:
                request = req_ctx.request
                ua = ""
                if hasattr(request, "headers"):
                    ua = request.headers.get("user-agent", "")
                elif isinstance(request, dict) and "headers" in request:
                    # ASGI scope 直接传进来时是 dict
                    headers = request.get("headers", [])
                    ua = next(
                        (v.decode() for k, v in headers if k.decode().lower() == "user-agent"),
                        "",
                    )
                ua_lower = ua.lower()
                for platform, keywords in _CALLER_KEYWORDS.items():
                    if any(kw in ua_lower for kw in keywords):
                        return platform
        except Exception:
            pass

    # 4. stdio 模式：看父进程名
    try:
        ppid = os.getppid()
        if psutil is not None:
            parent = psutil.Process(ppid)
            exe = (parent.exe() or "").lower()
            name = (parent.name() or "").lower()
            cmdline = " ".join(parent.cmdline() or []).lower()
            for platform, keywords in _CALLER_KEYWORDS.items():
                if any(kw in exe or kw in name or kw in cmdline for kw in keywords):
                    return platform
    except Exception:
        pass

    # 5. 环境变量 hint（部分客户端会注入）
    env_hints = [
        ("KIMI_CODE", "kimicode"),
        ("CODEX", "codex"),
        ("CLAUDE_CODE", "claude"),
        ("OPENCODE", "opencode"),
        ("MAVIS", "minimax"),
        ("ZCODE", "zcode"),
    ]
    for env_key, platform in env_hints:
        if os.environ.get(env_key):
            return platform

    return fallback or "unknown"


# ---------- MCP Server ----------

mcp = FastMCP(
    "mcp-hub",
    instructions=(
        "MCP Hub —— 让当前 AI 把其他模型当工具用。"
        "首次使用建议先看 list_runtimes / list_model_aliases；"
        "简单问答用 call_model，编程/多步任务用 spawn_subagent；"
        "runtime/model 都可传 'auto' 让 hub 自动路由。"
        "核心能力："
        "1) call_model 同步调用任意一个模型（直接 API）；"
        "2) spawn_subagent 开一个真实的 CLI 子 agent 进程（OpenCode/Claude Code/Codex/Kimi/Antigravity/CodeBuddy/Qoder），"
        "   子 agent 有自己的 context、工具、迭代能力；支持 reasoning_effort 控制思考等级（codex/opencode 生效）；"
        "3) recommend_model 根据任务描述推荐最适合的 runtime + model（fast/balanced/quality）；"
        "4) list_runtimes 列出可用的子 agent runtime；"
        "5) list_model_aliases 列出逻辑模型别名（deepseek/minimax/claude/kimi/gpt/gemini/opus）和 fallback 链；"
        "6) publish_task / claim_task / complete_task / verify_task 异步任务队列；"
        "7) queue_status 队列状态；"
        "8) list_tools / mmx_chat / mmx_image_generate / mmx_speech / mmx_music / "
        "mmx_search / mmx_vision / mmx_quota / mmx_voices / mmx_video_generate / mmx_video_get "
        "—— 多模态工具（mmx 文字/图像/语音/音乐/视频/搜索/看图）；"
        "8b) hedge_vision / hedge_image_generate / hedge_video_generate / hedge_models "
        "—— hedge-gateway（qwen 系视觉/生图/生视频，OpenAI 兼容）；"
        "9) list_workers / submit_cluster_task / scale_workers / cluster_stats "
        "—— 多 worker 集群（默认 DeepSeek V4 Flash via OpenCode Go 套餐，N 个 worker 并行）。"
        "完整使用说明见项目根目录 MCP_USAGE.md。"
    ),
)


# ---- 工具：列出模型 ----

@mcp.tool()
async def list_models() -> str:
    """列出所有可调用的模型（直连 API + 各 runtime 的子代理模型）。

    返回 JSON：count / api_count / runtime_count / models，
    每个模型包含 name / source（api 或 runtime）；
    api 模型另含 provider / base_url，runtime 模型 name 形如 "runtime/model"。
    """
    _init()
    assert _adapters is not None and _runtimes is not None
    # 直连 API 模型（call_model 可用）
    api_models = [dict(a.info(), source="api") for a in _adapters.values()]
    # runtime 模型（spawn_subagent 可用），前缀 runtime 名避免重名歧义
    runtime_models: list[dict] = []
    for r in _runtimes.values():
        if not r.is_available():
            continue
        for m in r.list_models():
            runtime_models.append(
                {
                    "name": f"{r.name}/{m}",
                    "runtime": r.name,
                    "model": m,
                    "source": "runtime",
                }
            )
    models = api_models + runtime_models
    return json.dumps(
        {
            "count": len(models),
            "api_count": len(api_models),
            "runtime_count": len(runtime_models),
            "models": models,
        },
        ensure_ascii=False,
        indent=2,
    )


# ---- 工具：列出 model aliases ----

@mcp.tool()
async def list_model_aliases() -> str:
    """列出逻辑模型别名（deepseek / minimax / claude / kimi / gpt）和 fallback 链。

    调用 call_model / spawn_subagent 时，model 字段可以是 alias，
    hub 会按 candidates 顺序找第一个可用的真实 model。
    """
    _init()
    assert _aliases is not None
    return json.dumps(
        {
            "aliases": [
                {"alias": a.alias, "candidates": a.candidates} for a in _aliases
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


# ---- 工具：列出可用 runtime ----

@mcp.tool()
async def list_runtimes() -> str:
    """列出可用的子 agent runtime（opencode / claude / kimi / codex / ...）。

    返回每个 runtime 的：name、binary、是否安装、可用的 model 列表。
    """
    _init()
    assert _runtimes is not None and _logger is not None
    info = []
    for r in _runtimes.values():
        d = r.info()
        _logger.info("runtime %s: %s", d["name"], d.get("models", [])[:5])
        info.append(d)
    return json.dumps(
        {
            "count": len(info),
            "max_concurrent": "unlimited" if _subagent_sem is None else _settings.hub_max_concurrent_subagents,
            "runtimes": info,
        },
        ensure_ascii=False,
        indent=2,
    )


# ---- 工具：根据任务推荐模型 ----

@mcp.tool()
async def recommend_model(task: str, priority: str = "balanced") -> str:
    """根据任务描述推荐最适合的 runtime + model。

    参数:
        task: 任务描述
        priority: fast（快/便宜） / balanced（均衡） / quality（质量优先）

    返回 JSON：{scenario, scenario_name, priority, runtime, model, reason}
    """
    _init()
    rec = await _recommend_model(task, priority)
    return json.dumps(rec, ensure_ascii=False, indent=2)


# ---- 工具：直接调用模型 ----

@mcp.tool()
async def call_model(
    model: str,
    prompt: str,
    system: str = "",
    max_tokens: int = 1024,
    temperature: float = 0.7,
) -> str:
    """同步调用一个模型，返回它的文本回答。

    参数:
        model: 模型别名（claude / gpt / kimi / minimax / deepseek）或具体 model 名
        prompt: 用户提示
        system: 系统提示（可选）
        max_tokens: 最大输出 token
        temperature: 采样温度

    alias 的 fallback 链见 list_model_aliases。
    """
    _init()
    assert _adapters is not None and _aliases is not None and _logger is not None
    # 解析 alias
    alias_map = {a.alias: a for a in _aliases}
    if model in alias_map:
        a = alias_map[model]
        # 直接调：找到 alias 第一个 candidates 里在 _adapters 里的
        adapter = _adapters.get(model)
        if adapter is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"alias '{model}' 没有原生 adapter 接，只能用 spawn_subagent 走 OpenCode",
                    "hint": f"试试: spawn_subagent(runtime='opencode', model='{a.candidates[0]}', ...)",
                    "candidates": a.candidates,
                },
                ensure_ascii=False,
            )
    else:
        adapter = _adapters.get(model)
        if adapter is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"unknown model: {model}",
                    "available": list(_adapters.keys()),
                },
                ensure_ascii=False,
            )

    req = ChatRequest(
        messages=[Message(role="user", content=prompt)],
        system=system or None,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    try:
        resp = await adapter.chat(req)
        _logger.info("call_model %s -> %d chars", model, len(resp.text))
        return json.dumps(
            {
                "ok": True,
                "model": model,
                "provider": resp.provider,
                "text": resp.text,
                "usage": resp.usage,
            },
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001
        _logger.exception("call_model %s failed", model)
        return json.dumps(
            {"ok": False, "model": model, "error": str(e)},
            ensure_ascii=False,
        )


# ---- 工具：发布任务 ----

@mcp.tool()
async def publish_task(
    topic: str,
    payload: str,
    from_model: str = "unknown",
    for_model: str = "",
    metadata_json: str = "{}",
    max_retries: int = 3,
    acceptance_json: str = "",
    webhook: str = "",
    ctx: Context | None = None,
) -> str:
    """发布一个异步任务到队列。

    参数:
        topic: 任务主题（消费者按 topic 拉取）
        payload: 任务内容
        from_model: 发布者（未指定时自动识别调用方平台）
        for_model: 指定消费者（空 = 任意）
        metadata_json: 额外元数据（JSON 字符串）
        max_retries: 失败重试次数
        acceptance_json: 验收配置（JSON 字符串），例：
            {
                "criteria": ["代码通过 pytest", "无 lint 错误"],
                "verifier": "claude",        # 验收方（model alias 或 model id）
                "max_iterations": 2,         # 最多重做几次
                "auto_retry": true           # 验不过是否自动重发
            }
        webhook: 任务状态变更时 POST 通知的 URL（可选）
    """
    _init()
    assert _store is not None and _logger is not None
    caller = _detect_caller(ctx, from_model)
    try:
        metadata = json.loads(metadata_json) if metadata_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"ok": False, "error": f"metadata_json 解析失败：{e}"})
    try:
        acceptance = json.loads(acceptance_json) if acceptance_json else None
    except json.JSONDecodeError as e:
        return json.dumps({"ok": False, "error": f"acceptance_json 解析失败：{e}"})

    task = await _store.publish(
        topic=topic,
        payload=payload,
        from_model=caller,
        for_model=for_model or None,
        metadata=metadata,
        max_retries=max_retries,
        acceptance=acceptance,
        webhook=webhook,
    )
    _logger.info(
        "publish_task topic=%s id=%s from=%s acceptance=%s webhook=%s",
        topic, task.task_id, caller, bool(acceptance), bool(webhook),
    )
    return json.dumps(
        {
            "ok": True,
            "task_id": task.task_id,
            "topic": task.topic,
            "created_at": task.created_at,
            "has_acceptance": bool(acceptance and acceptance.get("criteria")),
            "webhook_set": bool(webhook),
        },
        ensure_ascii=False,
    )


# ---- 工具：验收任务（v2）----

@mcp.tool()
async def verify_task(
    task_id: str,
    verifier: str,
    passed: bool,
    score: float = 0.0,
    issues: str = "",
) -> str:
    """写入 verifier 的验收结果。

    当任务有 acceptance 配置时，complete_task 不会直接 done，
    而是进入 verifying 状态，等 verifier 调这个工具写回结果。

    参数:
        task_id: 任务 ID
        verifier: 验收方（worker 名 / model alias）
        passed: 是否通过
        score: 评分 0-1
        issues: 不通过的原因（通过时为空）

    返回 JSON：
        - ok, task_id, next_action
        - next_action: "verified" | "retry" | "failed" | "not_in_verifying" | "unauthorized"
    """
    _init()
    assert _store is not None and _logger is not None
    task, action = await _store.verify(
        task_id=task_id,
        verifier=verifier,
        passed=passed,
        score=score,
        issues=issues,
    )
    if task is None:
        return json.dumps({"ok": False, "task_id": task_id, "next_action": action}, ensure_ascii=False)
    _logger.info(
        "verify_task id=%s verifier=%s passed=%s -> %s",
        task_id, verifier, passed, action,
    )
    return json.dumps(
        {
            "ok": True,
            "task_id": task_id,
            "next_action": action,
            "task": task.to_dict(),
        },
        ensure_ascii=False,
    )


# ---- 工具：认领任务 ----

@mcp.tool()
async def claim_task(
    topic: str,
    worker: str,
    for_model: str = "",
) -> str:
    """从一个 topic 认领一个待处理任务。

    参数:
        topic: 任务主题
        worker: 当前 worker 标识（一般填当前模型名）
        for_model: 当前 worker 是哪个模型（用于匹配 for_model 限制）

    返回 JSON：成功返回 task 对象；没有 pending 任务返回 ok=false。
    """
    _init()
    assert _store is not None and _logger is not None
    task = await _store.claim(
        topic=topic,
        worker=worker,
        for_model=for_model or None,
    )
    if task is None:
        return json.dumps({"ok": False, "reason": "no pending task"}, ensure_ascii=False)
    _logger.info("claim_task id=%s by=%s", task.task_id, worker)
    return json.dumps(
        {"ok": True, "task": task.to_dict()},
        ensure_ascii=False,
    )


# ---- 工具：完成任务 ----

@mcp.tool()
async def complete_task(
    task_id: str,
    worker: str,
    result: str,
    error: str = "",
) -> str:
    """把任务标记为 done / failed / verifying，并写回结果。

    如果任务有 acceptance 配置，状态会进入 verifying（等 verify_task）。
    否则直接 done / failed，并触发 webhook 通知。

    参数:
        task_id: 任务 ID
        worker: 当前 worker 标识（必须和 claim 时一致）
        result: 任务执行结果
        error: 失败原因（空 = 成功）
    """
    _init()
    assert _store is not None and _logger is not None
    task, action = await _store.complete(
        task_id=task_id,
        worker=worker,
        result=result,
        error=error or None,
    )
    if task is None:
        return json.dumps(
            {"ok": False, "error": "task not found or worker mismatch", "next_action": action},
            ensure_ascii=False,
        )
    _logger.info("complete_task id=%s status=%s next=%s", task_id, task.status, action)
    return json.dumps(
        {
            "ok": action in ("done", "verifying"),
            "task_id": task_id,
            "next_action": action,
            "task": task.to_dict(),
        },
        ensure_ascii=False,
    )


# ---- 工具：队列状态 ----

@mcp.tool()
async def queue_status(task_id: str = "") -> str:
    """查看队列状态，或单个任务的详情。

    参数:
        task_id: 留空 = 查总览；填 ID = 查单个任务
    """
    _init()
    assert _store is not None
    if task_id:
        task = await _store.status(task_id)
        if task is None:
            return json.dumps({"ok": False, "error": "not found"})
        return json.dumps({"ok": True, "task": task.to_dict()}, ensure_ascii=False)

    stats = await _store.stats()
    topics = await _store.list_topics()
    return json.dumps(
        {"ok": True, "stats": stats, "topics": topics},
        ensure_ascii=False,
    )


# ---- 工具：便捷的"派活并等结果"组合 ----

@mcp.tool()
async def dispatch_and_wait(
    topic: str,
    prompt: str,
    worker_model: str,
    from_model: str = "unknown",
    timeout_sec: int = 120,
    poll_interval_sec: float = 2.0,
    acceptance_json: str = "",
    webhook: str = "",
    ctx: Context | None = None,
) -> str:
    """发布任务 → 循环 claim → 用 worker_model 处理 → complete，阻塞直到拿到结果。

    如果提供了 acceptance_json，会进入 verifying 流程（verifier 也要跑）。

    适合"让另一个模型帮我干一件事"这种同步场景，但用队列解耦。
    """
    _init()
    assert _store is not None and _adapters is not None and _logger is not None
    caller = _detect_caller(ctx, from_model)

    try:
        acceptance = json.loads(acceptance_json) if acceptance_json else None
    except json.JSONDecodeError as e:
        return json.dumps({"ok": False, "error": f"acceptance_json 解析失败：{e}"})

    # 1) publish
    task = await _store.publish(
        topic=topic,
        payload=prompt,
        from_model=caller,
        for_model=worker_model,
        acceptance=acceptance,
        webhook=webhook,
    )
    _logger.info(
        "dispatch_and_wait task_id=%s -> %s from=%s acceptance=%s webhook=%s",
        task.task_id, worker_model, caller, bool(acceptance), bool(webhook),
    )

    # 2) 轮询直到完成 / 失败 / 超时
    elapsed = 0.0
    while elapsed < timeout_sec:
        await asyncio.sleep(poll_interval_sec)
        elapsed += poll_interval_sec
        t = await _store.status(task.task_id)
        if t is None:
            return json.dumps({"ok": False, "error": "task vanished"})
        if t.status == "done":
            return json.dumps(
                {
                    "ok": True,
                    "task_id": t.task_id,
                    "result": t.result,
                    "verify_history": t.verify_history,
                    "from_model": caller,
                },
                ensure_ascii=False,
            )
        if t.status == "failed":
            return json.dumps(
                {
                    "ok": False,
                    "task_id": t.task_id,
                    "error": t.error or "failed",
                    "verify_history": t.verify_history,
                },
                ensure_ascii=False,
            )
    return json.dumps(
        {
            "ok": False,
            "error": f"timeout after {timeout_sec}s",
            "task_id": task.task_id,
        },
        ensure_ascii=False,
    )


# ---- 工具：spawn_subagent（核心！开一个真子 agent 进程）----


async def _finalize_subagent(
    task_id: str,
    res: SubagentResult,
    stats: dict[str, Any] | None = None,
) -> None:
    """子 agent 终态收口：存内存结果 + registry 死亡现场落盘 + webhook 推送。

    status 语义：exit_code==0 → done，其余（含 None=退出码不可考）→ dead。
    调用方之后用 subagent_status 能查到 exit_code/现场，不用自己盯进程。
    """
    assert _subagent_results is not None and _logger is not None
    _subagent_results[task_id] = res
    status = "done" if res.exit_code == 0 else "dead"
    _registry_mark(task_id, status, result=res, stats=stats)
    h = _subagents.get(task_id) if _subagents else None
    await _fire_subagent_webhook(task_id, "subagent.done" if status == "done" else "subagent.failed", {
        "status": status,
        "exit_code": res.exit_code,
        "exit_code_source": "real" if res.exit_code is not None else "unknown",
        "duration_sec": round(res.duration_sec, 1),
        "summary": (res.summary or "")[-1000:],
        "stderr_tail": (res.stderr or "")[-2000:],
        "peak_rss_mb": (stats or {}).get("peak_rss_mb"),
        "peak_tokens": (stats or {}).get("peak_tokens"),
        "log_file": str(h.output_file) if h and h.output_file else "",
    })


@mcp.tool()
async def spawn_subagent(
    runtime: str,
    model: str,
    task: str,
    workdir: str = ".",
    timeout_sec: int = 600,
    wait: bool = True,
    from_model: str = "unknown",
    reasoning_effort: str = "",
    webhook: str = "",
    ctx: Context | None = None,
) -> str:
    """开一个真实的 CLI 子 agent 进程跑任务（这是 MCP Hub 的核心能力！）。

    参数:
        runtime: 哪个 runtime（list_runtimes 看），传 "auto" 让 hub 根据 task 自动选择
        model: 模型名，可以是 alias（deepseek/minimax/...）或具体 provider/model，
               传 "auto" / "fast" / "balanced" / "quality" 让 hub 根据 task 自动选择
        task: 给子 agent 的指令
        workdir: 子 agent 的工作目录
        timeout_sec: 超时秒数（超时 hub 会 kill 直接子进程；opencode/codex 会自动续跑最多 2 次）
        wait: True=阻塞等结果；False=立即返回 task_id（hub 后台继续守候，死亡现场落 registry）
        from_model: 调用方标识（未指定时自动识别调用方平台）
        reasoning_effort: 思考等级 none/minimal/low/medium/high/xhigh/max（是否可用取决于模型；仅 codex/opencode 等支持的 runtime 生效，其余忽略）
        webhook: 可选，子 agent 终态时 POST 通知（subagent.done / subagent.failed），
                 含 exit_code/duration/summary/peak_rss_mb/peak_tokens/log_file

    返回 JSON：
        - wait=True:  {ok, task_id, runtime, model, exit_code, summary, stdout, artifacts, duration_sec}
        - wait=False: {ok, task_id, runtime, model, pid, started_at, log_file}
          之后用 subagent_status(task_id) 查终态（exit_code/现场都在 registry 落盘）。
    """
    _init()
    assert _runtimes is not None and _aliases is not None and _logger is not None
    caller = _detect_caller(ctx, from_model)

    alias_map = {a.alias: a for a in _aliases}

    # 如果 model 是 alias，先解析并尝试推断 runtime
    if model in alias_map:
        candidates = alias_map[model].candidates
        resolved_model = candidates[0]
        # 根据 provider 前缀推断 runtime
        # botcf-claude / botcf-claude-stable 走 Claude Code CLI（claude runtime）；
        # opencode-go / opencode / botcf / qwen 走 opencode runtime；
        # qoder 走 qoderclicn runtime。
        inferred_runtime: str | None = None
        if resolved_model.startswith(("botcf-claude/", "botcf-claude-stable/")):
            inferred_runtime = "claude"
        elif resolved_model.startswith("qoder/"):
            inferred_runtime = "qoder"
        elif resolved_model.startswith(("opencode-go/", "opencode/", "botcf/", "qwen/", "zen-v4f/", "zen-free/", "deepseek/", "tokenrhythm/")):
            inferred_runtime = "opencode"
        elif resolved_model.startswith("antigravity/"):
            inferred_runtime = "antigravity"
        elif resolved_model.startswith("moonshot/"):
            inferred_runtime = "kimi"
        elif resolved_model.startswith("anthropic/"):
            inferred_runtime = "claude"
        elif resolved_model.startswith("openai/"):
            inferred_runtime = "codex"

        if runtime == "auto" and inferred_runtime and inferred_runtime in _runtimes:
            runtime = inferred_runtime
            model = resolved_model
            _logger.info(
                "spawn_subagent alias '%s' inferred runtime=%s model=%s",
                model.split("/")[0] if "/" in model else model,
                runtime,
                model,
            )
        else:
            # runtime 已指定，或推断不出：只解析 model
            model = resolved_model
            _logger.info(
                "spawn_subagent alias='%s' -> %s",
                model.split("/")[0] if "/" in model else model,
                model,
            )

    # 自动路由：runtime/model 还是 auto/priority 时，按 task 推荐
    if runtime == "auto" or model in ("auto", "fast", "balanced", "quality"):
        priority = model if model in ("fast", "balanced", "quality") else "balanced"
        rec = await _recommend_model(task, priority)
        if runtime == "auto":
            runtime = rec["runtime"]
        if model in ("auto", "fast", "balanced", "quality"):
            model = rec["model"]
        _logger.info(
            "spawn_subagent auto route -> runtime=%s model=%s (scenario=%s, reason=%s)",
            runtime, model, rec["scenario"], rec["reason"],
        )

    rt = _runtimes.get(runtime)
    if rt is None:
        return json.dumps(
            {
                "ok": False,
                "error": f"runtime '{runtime}' 不可用",
                "available": list(_runtimes.keys()),
            },
            ensure_ascii=False,
        )

    # 允许 runtime 原生模型名（如 antigravity 的 gemini-3.5-flash-medium）
    if "/" not in model:
        try:
            supported = rt.list_models()
        except Exception:  # noqa: BLE001
            supported = []
        if model not in supported:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"model '{model}' 既不是 alias，也不是 runtime '{runtime}' 支持的原生模型",
                    "hint": f"试试 alias：{list(alias_map.keys())}；或 runtime 原生模型：{supported[:10]}",
                },
                ensure_ascii=False,
            )

    # 支持 runtime 前缀的模型名，如 antigravity/gemini-3.5-flash-medium
    # alias 解析后可能得到这种形式，需要把前缀剥掉传给 runtime
    prefix = f"{runtime}/"
    if model.startswith(prefix):
        model_name = model[len(prefix):]
        try:
            supported = rt.list_models()
        except Exception:  # noqa: BLE001
            supported = []
        if model_name in supported:
            model = model_name
            _logger.info("spawn_subagent strip runtime prefix '%s' -> '%s'", runtime, model)

    # 生成 task_id
    import uuid
    task_id = uuid.uuid4().hex[:12]

    # 并发控制
    if _subagent_sem is not None:
        await _subagent_sem.acquire()

    try:
        # 真 spawn
        handle = await rt.spawn(
            task_id=task_id,
            model=model,
            task=task,
            workdir=workdir,
            timeout_sec=timeout_sec,
            reasoning_effort=reasoning_effort,
        )
        _subagents[task_id] = handle
        _registry_register(handle, caller, reasoning_effort, webhook=webhook)
        _logger.info("spawn_subagent id=%s runtime=%s model=%s pid=%s", task_id, runtime, model, handle.pid)

        async def _respawn_and_wait(mdl: str, suffix: str) -> SubagentResult:
            """重试用：以新 task_id 后缀 respawn 并等结果，登记 registry/结果。"""
            tid2 = task_id + suffix
            h2 = await rt.spawn(
                task_id=tid2, model=mdl, task=task, workdir=workdir, timeout_sec=timeout_sec,
                reasoning_effort=reasoning_effort,
            )
            _subagents[tid2] = h2
            _registry_register(h2, caller, reasoning_effort)
            r2 = await rt.wait(h2, timeout_sec)
            _subagent_results[tid2] = r2
            _registry_mark(tid2, "done", result=r2)
            return r2

        async def _resume_retry(res: SubagentResult, handle: SubagentHandle) -> SubagentResult:
            """超时断线续跑：runtime 支持 resume 且日志里能提取到 session id 时，
            用同一 session 续跑（task_id 后缀 rs1/rs2），最多 settings.hub_resume_max_attempts 次。
            每次续跑从最近一次的 handle 提取 session id（同一 session 贯穿）。"""
            max_attempts = getattr(_settings, "hub_resume_max_attempts", 2) if _settings else 2
            if not getattr(rt, "supports_resume", False) or max_attempts <= 0:
                return res
            cur_handle = handle
            for attempt in range(1, max_attempts + 1):
                if not _is_timeout_error(res):
                    break
                session_id = rt.extract_session_id(cur_handle)
                if not session_id:
                    _logger.warning(
                        "spawn_subagent id=%s 超时但未能提取 session id，放弃续跑", task_id,
                    )
                    break
                resume_prompt = (
                    "（系统自动续跑：上次执行因网络/超时中断。请基于当前进度继续完成原任务，"
                    "不要从头开始；已产出的文件和分析直接复用。）\n\n原任务：\n" + task
                )
                tid2 = task_id + f"rs{attempt}"
                _logger.warning(
                    "spawn_subagent id=%s 第%d次超时，用 session=%s 续跑（%s）",
                    task_id, attempt, session_id, tid2,
                )
                h2 = await rt.resume_spawn(
                    session_id=session_id,
                    task_id=tid2,
                    model=model,
                    task=resume_prompt,
                    workdir=workdir,
                    timeout_sec=timeout_sec,
                )
                _subagents[tid2] = h2
                _registry_register(h2, caller, reasoning_effort)
                res = await rt.wait(h2, timeout_sec)
                _subagent_results[tid2] = res
                _registry_mark(tid2, "done", result=res)
                note = f"[续跑] 第{attempt}次超时后自动续跑（session={session_id}）"
                res.stderr = f"{note}\n{res.stderr or ''}".strip()
                cur_handle = h2
            return res

        async def _botcf_retry(res: SubagentResult) -> SubagentResult:
            """botcf-claude 超时策略：主通道重试一次；仍超时且有稳定通道映射则切
            botcf-claude-stable 再试一次。grok / 其他通道不触发（无保底）。"""
            if not model.startswith("botcf-claude/") or not _is_timeout_error(res):
                return res
            _logger.warning(
                "spawn_subagent id=%s model=%s 超时，主通道重试一次", task_id, model,
            )
            res = await _respawn_and_wait(model, "r1")
            if not _is_timeout_error(res):
                return res
            stable = _BOTCF_STABLE_MAP.get(model)
            if not stable:
                _logger.warning(
                    "spawn_subagent id=%s model=%s 二次超时且无稳定通道映射，放弃", task_id, model,
                )
                return res
            _logger.warning(
                "spawn_subagent id=%s model=%s 二次超时，切稳定通道 %s", task_id, model, stable,
            )
            res = await _respawn_and_wait(stable, "st")
            note = f"[回退] '{model}' 主通道两次超时，已切到稳定通道 '{stable}'。"
            res.stderr = f"{note}\n{res.stderr or ''}".strip()
            res.model = f"{stable} (fallback from {model})"
            return res

        if not wait:
            async def _bg_wait() -> None:
                # wait=False 也在后台等结果。输出已由 spawn 直接重定向到
                # .log 文件（不再走 PIPE），所以不存在"没人 drain 管道导致
                # 子进程堵死"的问题；这里等进程退出、采样 RSS/tokens 峰值、
                # 解析日志、死亡现场落 registry、按需推 webhook。
                if _subagent_sem is not None:
                    await _subagent_sem.acquire()
                stats: dict[str, Any] = {}
                sampler = asyncio.create_task(
                    _sample_process_stats(handle.pid, handle.output_file, stats)
                )
                try:
                    res = await rt.wait(handle, timeout_sec)
                    res = await _resume_retry(res, handle)
                    res = await _botcf_retry(res)
                    await _finalize_subagent(task_id, res, stats)
                    _logger.info(
                        "spawn_subagent id=%s (background) done exit=%s duration=%.1fs",
                        task_id, res.exit_code, res.duration_sec,
                    )
                    if res.exit_code != 0:
                        _logger.warning(
                            "spawn_subagent id=%s (background) failed exit=%s stderr=%.200s",
                            task_id, res.exit_code, res.stderr or "",
                        )
                except Exception:  # noqa: BLE001
                    _logger.exception("spawn_subagent id=%s background wait failed", task_id)
                finally:
                    sampler.cancel()
                    try:
                        await sampler
                    except asyncio.CancelledError:
                        pass
                    if _subagent_sem is not None:
                        _subagent_sem.release()

            asyncio.create_task(_bg_wait())
            return json.dumps(
                {
                    "ok": True,
                    "task_id": task_id,
                    "runtime": runtime,
                    "model": model,
                    "pid": handle.pid,
                    "started_at": handle.started_at,
                    "log_file": str(handle.output_file) if handle.output_file else "",
                    "from_model": caller,
                },
                ensure_ascii=False,
            )

        # 阻塞等（采样器同步盯 RSS/tokens 峰值，结束后落 registry）
        stats: dict[str, Any] = {}
        sampler = asyncio.create_task(
            _sample_process_stats(handle.pid, handle.output_file, stats)
        )
        try:
            result = await rt.wait(handle, timeout_sec)
            result = await _resume_retry(result, handle)
            result = await _botcf_retry(result)
        finally:
            sampler.cancel()
            try:
                await sampler
            except asyncio.CancelledError:
                pass

        # Antigravity Claude 模型容量不足时，自动回退到 Gemini
        if runtime == "antigravity" and _is_antigravity_capacity_error(result):
            fallback_model = _pick_antigravity_fallback(model)
            if fallback_model:
                fb_task_id = task_id + "fb"
                _logger.warning(
                    "spawn_subagent id=%s model=%s capacity error, falling back to %s",
                    task_id, model, fallback_model,
                )
                try:
                    fb_handle = await rt.spawn(
                        task_id=fb_task_id,
                        model=fallback_model,
                        task=task,
                        workdir=workdir,
                        timeout_sec=timeout_sec,
                        reasoning_effort=reasoning_effort,
                    )
                    _subagents[fb_task_id] = fb_handle
                    _registry_register(fb_handle, caller, reasoning_effort)
                    fb_result = await rt.wait(fb_handle, timeout_sec)
                    _subagent_results[fb_task_id] = fb_result
                    _registry_mark(fb_task_id, "done", result=fb_result)
                    # 把回退信息注入 summary / stderr，让调用方知道发生过回退
                    warning = (
                        f"[警告] 请求的 Antigravity 模型 '{model}' 因服务端容量不足或超时（high traffic / 503 / timeout），"
                        f"已自动回退到 '{fallback_model}'。"
                    )
                    fb_result.summary = f"{warning}\n\n{fb_result.summary or ''}".strip()
                    if fb_result.stderr:
                        fb_result.stderr = f"{warning}\n{fb_result.stderr}"
                    else:
                        fb_result.stderr = warning
                    fb_result.model = f"{fallback_model} (fallback from {model})"
                    _logger.info(
                        "spawn_subagent fallback id=%s done exit=%d duration=%.1fs",
                        fb_task_id, fb_result.exit_code, fb_result.duration_sec,
                    )
                    # 原 task_id 也要收口（否则 registry 永远 running，下次重启变孤儿）
                    await _finalize_subagent(task_id, fb_result, stats)
                    return json.dumps(
                        {
                            "ok": fb_result.exit_code == 0,
                            "task_id": fb_task_id,
                            "result": fb_result.to_dict(),
                            "from_model": caller,
                            "fallback": True,
                            "original_model": model,
                            "fallback_model": fallback_model,
                        },
                        ensure_ascii=False,
                    )
                except Exception as e:  # noqa: BLE001
                    _logger.exception("spawn_subagent fallback failed")
                    # 回退也失败，返回原始失败结果，但附加回退失败说明
                    result.stderr = (
                        f"{result.stderr or ''}\n[回退失败] 尝试回退到 {fallback_model} 时出错: {e}"
                    ).strip()

        # 通用兜底：遇到超时/容量/服务端错误时，回退到 GPT 5.6 Sol
        if (
            result.exit_code != 0
            and model != "gpt-5.6-sol"
            and _is_fallback_to_sol_error(result)
            and "codex" in _runtimes
        ):
            sol_runtime = _runtimes["codex"]
            try:
                sol_supported = sol_runtime.list_models()
            except Exception:  # noqa: BLE001
                sol_supported = []
            if "gpt-5.6-sol" in sol_supported:
                sol_task_id = task_id + "sol"
                _logger.warning(
                    "spawn_subagent id=%s runtime=%s model=%s failed, falling back to codex/gpt-5.6-sol",
                    task_id, runtime, model,
                )
                try:
                    sol_handle = await sol_runtime.spawn(
                        task_id=sol_task_id,
                        model="gpt-5.6-sol",
                        task=task,
                        workdir=workdir,
                        timeout_sec=timeout_sec,
                        reasoning_effort=reasoning_effort,
                    )
                    _subagents[sol_task_id] = sol_handle
                    _registry_register(sol_handle, caller, reasoning_effort)
                    sol_result = await sol_runtime.wait(sol_handle, timeout_sec)
                    _subagent_results[sol_task_id] = sol_result
                    _registry_mark(sol_task_id, "done", result=sol_result)
                    warning = (
                        f"[兜底] 原模型 '{model}'（runtime={runtime}）因超时或服务端错误失败，"
                        f"已自动回退到 codex/gpt-5.6-sol。"
                    )
                    sol_result.summary = f"{warning}\n\n{sol_result.summary or ''}".strip()
                    if sol_result.stderr:
                        sol_result.stderr = f"{warning}\n{sol_result.stderr}"
                    else:
                        sol_result.stderr = warning
                    sol_result.model = f"gpt-5.6-sol (fallback from {runtime}/{model})"
                    _logger.info(
                        "spawn_subagent sol fallback id=%s done exit=%d duration=%.1fs",
                        sol_task_id, sol_result.exit_code, sol_result.duration_sec,
                    )
                    # 原 task_id 也要收口（否则 registry 永远 running，下次重启变孤儿）
                    await _finalize_subagent(task_id, sol_result, stats)
                    return json.dumps(
                        {
                            "ok": sol_result.exit_code == 0,
                            "task_id": sol_task_id,
                            "result": sol_result.to_dict(),
                            "from_model": caller,
                            "fallback": True,
                            "original_runtime": runtime,
                            "original_model": model,
                            "fallback_runtime": "codex",
                            "fallback_model": "gpt-5.6-sol",
                        },
                        ensure_ascii=False,
                    )
                except Exception as e:  # noqa: BLE001
                    _logger.exception("spawn_subagent sol fallback failed")
                    result.stderr = (
                        f"{result.stderr or ''}\n[兜底失败] 尝试回退到 gpt-5.6-sol 时出错: {e}"
                    ).strip()

        await _finalize_subagent(task_id, result, stats)
        _logger.info(
            "spawn_subagent id=%s done exit=%d duration=%.1fs",
            task_id, result.exit_code, result.duration_sec,
        )
        return json.dumps(
            {"ok": result.exit_code == 0, "task_id": task_id, "result": result.to_dict(), "from_model": caller, "reasoning_effort": reasoning_effort},
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001
        _logger.exception("spawn_subagent failed")
        return json.dumps({"ok": False, "task_id": task_id, "error": str(e)}, ensure_ascii=False)
    finally:
        if _subagent_sem is not None:
            _subagent_sem.release()


# ---- 工具：看子 agent 状态 ----

@mcp.tool()
async def subagent_status(task_id: str = "") -> str:
    """看子 agent 状态（以 registry 落盘记录为准，死后/重启后也可查）。

    参数:
        task_id: 留空 = 列出所有子 agent（新→旧，最多 50 条）；填 ID = 看单个详情

    单个详情字段：status(running/done/dead) / exit_code / exit_code_source
    （real=真实退出码，unknown=进程死透不可考，不再谎报 0）/ duration_sec /
    summary / stderr_tail / peak_rss_mb / peak_tokens / log_file / caller /
    webhook / resumed_from / is_alive / result（内存里有完整结果时带上）。
    """
    _init()
    _ensure_orphan_pollers()
    assert _subagents is not None
    with _registry_lock():
        reg = _registry_load_unlocked()
    entries = reg.get("subagents", {})

    if task_id:
        entry = entries.get(task_id)
        h = _subagents.get(task_id)
        r = _subagent_results.get(task_id)
        if entry is None and h is None:
            return json.dumps({"ok": False, "error": "not found"}, ensure_ascii=False)
        info: dict[str, Any] = {"task_id": task_id}
        if entry is not None:
            info.update({
                "runtime": entry.get("runtime"),
                "model": entry.get("model"),
                "pid": entry.get("pid"),
                "status": entry.get("status"),
                "started_at": entry.get("started_at"),
                "finished_at": entry.get("finished_at"),
                "duration_sec": entry.get("duration_sec"),
                "exit_code": entry.get("exit_code"),
                "exit_code_source": entry.get("exit_code_source"),
                "summary": entry.get("summary"),
                "stderr_tail": entry.get("stderr_tail"),
                "error": entry.get("error"),
                "peak_rss_mb": entry.get("peak_rss_mb"),
                "peak_tokens": entry.get("peak_tokens"),
                "session_id": entry.get("session_id"),
                "log_file": entry.get("log_file"),
                "workdir": entry.get("workdir"),
                "caller": entry.get("caller"),
                "webhook": entry.get("webhook"),
                "resumed_from": entry.get("resumed_from"),
            })
        else:
            # registry 里没有（极老的内存残留），退化为内存视图
            info.update({
                "runtime": h.runtime,
                "model": h.model,
                "pid": h.pid,
                "started_at": h.started_at,
                "status": "running" if r is None else ("done" if r.exit_code == 0 else "dead"),
            })
        if info.get("status") == "running":
            if h is not None:
                info["is_alive"] = _handle_alive(h)
            elif info.get("pid"):
                info["is_alive"] = _pid_alive(info["pid"])
            else:
                info["is_alive"] = False
        else:
            info["is_alive"] = False
        if r is not None:
            info["result"] = r.to_dict()
        return json.dumps({"ok": True, "info": info}, ensure_ascii=False)

    def _sort_key(item: tuple[str, Any]) -> float:
        return item[1].get("started_at") or 0

    out = []
    for tid, entry in sorted(entries.items(), key=_sort_key, reverse=True)[:50]:
        status = entry.get("status", "running")
        if status == "running":
            h = _subagents.get(tid)
            if h is not None:
                alive = _handle_alive(h)
            elif entry.get("pid"):
                alive = _pid_alive(entry["pid"])
            else:
                alive = False
        else:
            alive = False
        r = _subagent_results.get(tid)
        exit_code = entry.get("exit_code")
        if exit_code is None and r is not None:
            exit_code = r.exit_code
        out.append(
            {
                "task_id": tid,
                "runtime": entry.get("runtime"),
                "model": entry.get("model"),
                "pid": entry.get("pid"),
                "started_at": entry.get("started_at"),
                "status": status,
                "is_alive": alive,
                "finished": status != "running",
                "exit_code": exit_code,
                "duration_sec": entry.get("duration_sec"),
                "caller": entry.get("caller"),
                "peak_rss_mb": entry.get("peak_rss_mb"),
                "peak_tokens": entry.get("peak_tokens"),
                "log_file": entry.get("log_file"),
            }
        )
    return json.dumps(
        {"ok": True, "total": len(entries), "count": len(out), "subagents": out},
        ensure_ascii=False,
    )


# ---- 工具：杀掉子 agent ----

@mcp.tool()
async def cancel_subagent(task_id: str) -> str:
    """杀掉还在跑的子 agent 进程。

    参数:
        task_id: 任务 ID
    """
    _init()
    assert _runtimes is not None and _logger is not None
    h = _subagents.get(task_id)
    if h is None:
        return json.dumps({"ok": False, "error": "not found"})
    rt = _runtimes.get(h.runtime)
    if rt is None:
        return json.dumps({"ok": False, "error": "runtime gone"})
    ok = await rt.cancel(h)
    if not ok and h.process is None and h.pid and _pid_alive(h.pid):
        # 恢复出来的孤儿句柄没有 process 对象，直接按 pid 杀
        ok = _kill_pid(h.pid)
    _logger.info("cancel_subagent id=%s ok=%s", task_id, ok)
    return json.dumps({"ok": ok, "task_id": task_id}, ensure_ascii=False)


# ---- 工具：续跑已死/中断的子 agent（死可续）----

@mcp.tool()
async def resume_subagent(
    task_id: str,
    task: str = "",
    timeout_sec: int = 600,
    wait: bool = True,
    ctx: Context | None = None,
) -> str:
    """基于已死/中断任务的 session 续跑（runtime 需支持 resume）。

    从 registry 拿原任务的 runtime/model/workdir/log_file，从日志提取
    session id 后用同一 session 拉起新进程——复用原会话上下文，不用从头来。

    参数:
        task_id: 原任务 ID（registry 里要有记录，log_file 得还在）
        task: 追加的续跑指令；留空 = 自动拼"基于当前进度继续完成原任务"
        timeout_sec: 超时秒数
        wait: True=阻塞等结果；False=立即返回新 task_id（后台守候，现场落 registry）

    返回 JSON：{ok, task_id(新), resumed_from, session_id, ...}
    """
    _init()
    assert _runtimes is not None and _logger is not None
    with _registry_lock():
        reg = _registry_load_unlocked()
    entry = reg.get("subagents", {}).get(task_id)
    if entry is None:
        return json.dumps(
            {"ok": False, "error": f"registry 里查不到任务 {task_id}（只有经本 hub spawn 的任务可续跑）"},
            ensure_ascii=False,
        )
    rt = _runtimes.get(entry.get("runtime") or "")
    if rt is None:
        return json.dumps(
            {"ok": False, "error": f"runtime '{entry.get('runtime')}' 当前不可用"},
            ensure_ascii=False,
        )
    if not getattr(rt, "supports_resume", False):
        return json.dumps(
            {"ok": False, "error": f"runtime '{rt.name}' 不支持续跑（supports_resume=False）"},
            ensure_ascii=False,
        )
    # 优先用 registry 里落盘的 session_id（终态时已从结果映射进来）；
    # 没有再从原任务日志提取（日志还在的话）
    session_id = entry.get("session_id") or ""
    if not session_id:
        log_file = entry.get("log_file") or ""
        if not log_file or not Path(log_file).exists():
            return json.dumps(
                {"ok": False, "error": f"registry 无 session_id 且日志文件不存在（{log_file or '未记录'}），无法续跑"},
                ensure_ascii=False,
            )
        probe = SubagentHandle(
            pid=entry.get("pid"),
            runtime=entry.get("runtime") or "",
            model=entry.get("model") or "",
            task_id=task_id,
            workdir=entry.get("workdir") or ".",
            started_at=entry.get("started_at") or 0.0,
            output_file=Path(log_file),
        )
        session_id = rt.extract_session_id(probe) or ""
    if not session_id:
        return json.dumps(
            {"ok": False, "error": "拿不到 session id（任务可能没跑起来就死了）"},
            ensure_ascii=False,
        )

    import uuid
    new_id = uuid.uuid4().hex[:12]
    model = entry.get("model") or ""
    workdir = entry.get("workdir") or "."
    orig_prompt = entry.get("prompt") or ""
    if task:
        prompt = task
    else:
        prompt = (
            "（系统自动续跑：上次执行中断。请基于当前进度继续完成原任务，"
            "不要从头开始；已产出的文件和分析直接复用。）\n\n原任务：\n" + orig_prompt
        )
    # 原 caller 是占位符（unknown/空）时重新探测，否则继承
    caller = _detect_caller(ctx, entry.get("caller") or "unknown")

    if _subagent_sem is not None:
        await _subagent_sem.acquire()
    try:
        h2 = await rt.resume_spawn(
            session_id=session_id,
            task_id=new_id,
            model=model,
            task=prompt,
            workdir=workdir,
            timeout_sec=timeout_sec,
        )
        _subagents[new_id] = h2
        _registry_register(
            h2,
            caller,
            entry.get("reasoning_effort") or "",
            webhook=entry.get("webhook") or "",
            resumed_from=task_id,
        )
        _logger.info(
            "resume_subagent id=%s resumed_from=%s session=%s pid=%s",
            new_id, task_id, session_id, h2.pid,
        )

        if not wait:
            async def _bg_wait() -> None:
                if _subagent_sem is not None:
                    await _subagent_sem.acquire()
                stats: dict[str, Any] = {}
                sampler = asyncio.create_task(
                    _sample_process_stats(h2.pid, h2.output_file, stats)
                )
                try:
                    res = await rt.wait(h2, timeout_sec)
                    await _finalize_subagent(new_id, res, stats)
                    _logger.info(
                        "resume_subagent id=%s (background) done exit=%s duration=%.1fs",
                        new_id, res.exit_code, res.duration_sec,
                    )
                except Exception:  # noqa: BLE001
                    _logger.exception("resume_subagent id=%s background wait failed", new_id)
                finally:
                    sampler.cancel()
                    try:
                        await sampler
                    except asyncio.CancelledError:
                        pass
                    if _subagent_sem is not None:
                        _subagent_sem.release()

            asyncio.create_task(_bg_wait())
            return json.dumps(
                {
                    "ok": True,
                    "task_id": new_id,
                    "resumed_from": task_id,
                    "session_id": session_id,
                    "runtime": h2.runtime,
                    "model": h2.model,
                    "pid": h2.pid,
                    "started_at": h2.started_at,
                    "log_file": str(h2.output_file) if h2.output_file else "",
                },
                ensure_ascii=False,
            )

        stats: dict[str, Any] = {}
        sampler = asyncio.create_task(
            _sample_process_stats(h2.pid, h2.output_file, stats)
        )
        try:
            res = await rt.wait(h2, timeout_sec)
        finally:
            sampler.cancel()
            try:
                await sampler
            except asyncio.CancelledError:
                pass
        await _finalize_subagent(new_id, res, stats)
        _logger.info(
            "resume_subagent id=%s done exit=%s duration=%.1fs",
            new_id, res.exit_code, res.duration_sec,
        )
        return json.dumps(
            {
                "ok": res.exit_code == 0,
                "task_id": new_id,
                "resumed_from": task_id,
                "session_id": session_id,
                "result": res.to_dict(),
            },
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001
        _logger.exception("resume_subagent failed")
        return json.dumps({"ok": False, "task_id": new_id, "error": str(e)}, ensure_ascii=False)
    finally:
        if _subagent_sem is not None:
            _subagent_sem.release()


# ---- 工具：usage_stats（token / cost 用量聚合）----

@mcp.tool()
async def usage_stats() -> str:
    """聚合所有 subagent 的 token / cost 用量（按模型、按天分组）。

    数据来源：registry（每个任务的 runtime/model/时间）+ transcript 里的
    usage 事件。opencode / grok / codex 有 usage 数据；其它 runtime 的
    adapter 不吐 usage，计入 tasks_without_usage，不编造。

    返回 JSON：
        - total: {tasks, tokens{input,output,reasoning,total,cache{read,write}}, cost}
        - by_model: {"runtime/model": 同上}（按 total tokens 降序）
        - by_day: {"YYYY-MM-DD": 同上}
        - tasks_total / tasks_with_usage / tasks_without_usage
    """
    _init()
    from .usage_stats import collect_usage

    return json.dumps(collect_usage(), ensure_ascii=False, indent=2)


# ---- 工具：list_tools（多模态工具总览）----

@mcp.tool()
async def list_tools() -> str:
    """列出所有可用的多模态/工具型 adapter（区别于 list_runtimes 那种 subagent）。

    返回每个 tool 的：name、binary、是否安装、可用的 operation 列表。

    例如 mmx 提供：chat / image_generate / speech / music_generate /
    search / vision / quota / voices / video_generate / video_get。
    """
    _init()
    assert _tools is not None
    info = [t.info() for t in _tools.values()]
    return json.dumps(
        {"count": len(info), "tools": info},
        ensure_ascii=False,
        indent=2,
    )


# ---- 工具：mmx 多模态（每个 operation 一个 MCP tool）----

async def _mmx_call(operation: str, **kwargs) -> str:
    _init()
    assert _tools is not None and _logger is not None
    mmx = _tools.get("mmx")
    if mmx is None:
        return json.dumps(
            {"ok": False, "error": "mmx 不可用（没装或不在 PATH）", "operation": operation},
            ensure_ascii=False,
        )
    try:
        result: ToolResult = await mmx.call(operation, **kwargs)
        _logger.info(
            "mmx.%s ok=%s duration=%.2fs",
            operation, result.ok, result.duration_sec,
        )
        d = result.to_dict()
        # mmx 失败时也用 ok=false 透传错误
        return json.dumps({"ok": result.ok, **d}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        _logger.exception("mmx.%s 异常", operation)
        return json.dumps({"ok": False, "operation": operation, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def mmx_chat(
    message: str,
    model: str = "MiniMax-M3",
    system: str = "",
    max_tokens: int = 4096,
    temperature: float = 0.7,
) -> str:
    """mmx 文本对话（调 MiniMax Messages API，包装 mmx text chat）。

    参数:
        message: 用户消息
        model: 模型 ID（默认 MiniMax-M3）
        system: 系统提示
        max_tokens: 最大输出 token
        temperature: 采样温度
    """
    return await _mmx_call(
        "chat",
        message=message, model=model, system=system,
        max_tokens=max_tokens, temperature=temperature,
    )


@mcp.tool()
async def mmx_image_generate(
    prompt: str,
    aspect_ratio: str = "1:1",
    n: int = 1,
    out_dir: str = "",
    out: str = "",
    seed: int = 0,
    model: str = "",
) -> str:
    """mmx 图像生成（image-01 / image-01-live）。

    参数:
        prompt: 图像描述
        aspect_ratio: 宽高比（"16:9", "1:1", "9:16"...）
        n: 生成张数
        out_dir: 下载到这个目录（推荐）
        out: 保存到具体文件路径（仅 n=1）
        seed: 复现种子（同 seed + 同 prompt 出同样图）
        model: 模型名（默认让 mmx 自己选）
    """
    return await _mmx_call(
        "image_generate",
        prompt=prompt, aspect_ratio=aspect_ratio, n=n,
        out_dir=out_dir, out=out, seed=seed, model=model,
    )


@mcp.tool()
async def mmx_speech(
    text: str,
    voice: str = "",
    out: str = "",
    format: str = "mp3",
    speed: float = 0,
    pitch: int = 0,
) -> str:
    """mmx 语音合成（TTS，speech-2.8-hd / 2.6 / 02）。

    参数:
        text: 要念的文本
        voice: 语音 ID（先用 mmx_voices 查）
        out: 保存到文件路径（否则 mmx 走 hex 模式打印 body）
        format: mp3 / pcm / flac / wav / opus
        speed: 速度倍率
        pitch: 音调调整
    """
    return await _mmx_call(
        "speech",
        text=text, voice=voice, out=out, format=format,
        speed=speed, pitch=pitch,
    )


@mcp.tool()
async def mmx_music_generate(
    prompt: str,
    out: str = "",
    lyrics: str = "",
) -> str:
    """mmx 音乐生成。

    参数:
        prompt: 风格/情绪/乐器描述
        out: 保存路径
        lyrics: 歌词（可选；不传就是纯背景乐）
    """
    return await _mmx_call(
        "music_generate",
        prompt=prompt, out=out, lyrics=lyrics,
    )


@mcp.tool()
async def mmx_search(query: str, count: int = 10) -> str:
    """mmx 网络搜索（用 MiniMax Search API）。

    参数:
        query: 搜索关键词
        count: 返回条数
    """
    return await _mmx_call("search", query=query, count=count)


@mcp.tool()
async def mmx_vision(image: str, prompt: str = "") -> str:
    """mmx 看图理解（vision describe）。

    参数:
        image: 本地路径或 https URL
        prompt: 额外提示（不传就用默认 describe）
    """
    return await _mmx_call("vision", image=image, prompt=prompt)


@mcp.tool()
async def mmx_quota() -> str:
    """mmx 配额查询（看 API 余额）。"""
    return await _mmx_call("quota")


@mcp.tool()
async def mmx_voices() -> str:
    """列出 mmx 支持的语音 preset。"""
    return await _mmx_call("voices")


@mcp.tool()
async def mmx_video_generate(
    prompt: str,
    out: str = "",
    duration: int = 6,
    resolution: str = "768P",
    model: str = "",
) -> str:
    """mmx 视频生成（异步，返回 task_id，要轮询拿结果）。

    参数:
        prompt: 视频描述
        out: 保存路径（轮询拿到后下载）
        duration: 6 / 10 秒
        resolution: 768P / 1080P
        model: 模型名（默认让 mmx 选）
    """
    return await _mmx_call(
        "video_generate",
        prompt=prompt, out=out, duration=duration,
        resolution=resolution, model=model,
    )


@mcp.tool()
async def mmx_video_get(task_id: str) -> str:
    """查询 mmx 视频任务状态。

    参数:
        task_id: mmx_video_generate 返回的 task_id
    """
    return await _mmx_call("video_get", task_id=task_id)


# ---- 工具：hedge-gateway 多模态（qwen 系，OpenAI 兼容 HTTP）----

async def _hedge_call(operation: str, **kwargs) -> str:
    _init()
    assert _tools is not None and _logger is not None
    hedge = _tools.get("hedge")
    if hedge is None:
        return json.dumps(
            {"ok": False, "error": "hedge 不可用（未配置 base_url）", "operation": operation},
            ensure_ascii=False,
        )
    try:
        result: ToolResult = await hedge.call(operation, **kwargs)
        _logger.info(
            "hedge.%s ok=%s duration=%.2fs",
            operation, result.ok, result.duration_sec,
        )
        return json.dumps({"ok": result.ok, **result.to_dict()}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        _logger.exception("hedge.%s 异常", operation)
        return json.dumps({"ok": False, "operation": operation, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def hedge_vision(
    image: str,
    prompt: str = "",
    model: str = "qwen3.8-max-thinking",
    max_tokens: int = 4096,
) -> str:
    """hedge-gateway 看图理解（qwen 系视觉模型）。

    参数:
        image: 本地图片路径（自动转 base64，≤20MB）或 http(s) URL（网关代下载）
        prompt: 提问（默认"描述这张图片"）
        model: vision 模型（qwen3.8-max / qwen3.8-max-thinking / qwen3.8-128k /
               qwen3.8-262k / qwen3.7-plus；更专门的 qwen3-vl-plus 也可用）
        max_tokens: 最大输出 token
    """
    return await _hedge_call(
        "vision", image=image, prompt=prompt, model=model, max_tokens=max_tokens,
    )


@mcp.tool()
async def hedge_image_generate(
    prompt: str,
    size: str = "1024x1024",
    n: int = 1,
    out_dir: str = "",
    out: str = "",
    model: str = "",
) -> str:
    """hedge-gateway 生图（/v1/images/generations，已实测可用）。

    参数:
        prompt: 图像描述
        size: 宽x高（如 "1024x1024"）
        n: 张数
        out_dir: 保存目录（推荐）
        out: 保存到具体文件路径（仅 n=1）
        model: 模型名（默认让网关自己选）
    """
    return await _hedge_call(
        "image_generate", prompt=prompt, size=size, n=n,
        out_dir=out_dir, out=out, model=model,
    )


@mcp.tool()
async def hedge_video_generate(
    prompt: str,
    duration: int = 6,
    resolution: str = "768P",
    model: str = "",
    out: str = "",
) -> str:
    """hedge-gateway 生视频。注意：网关侧 /v1/videos/generations 代码在未实测，尽力透传。

    参数:
        prompt: 视频描述
        duration: 时长（秒）
        resolution: 分辨率（如 "768P" / "1080P"）
        model: 模型名（默认让网关自己选）
        out: 若响应直接带视频数据则保存到此路径；异步任务型响应原样透传
    """
    return await _hedge_call(
        "video_generate", prompt=prompt, duration=duration,
        resolution=resolution, model=model, out=out,
    )


@mcp.tool()
async def hedge_models() -> str:
    """列出 hedge-gateway 当前可用的模型（GET /v1/models）。"""
    return await _hedge_call("models")


# ---- 工具：cluster（多 worker 集群）----

@mcp.tool()
async def list_workers() -> str:
    """列出当前 cluster 的所有 worker 状态（idle/busy + 当前任务 + 统计）。

    一个 worker = 一个 runtime 进程 slot。N 个 worker 并行消费同一个 topic。
    """
    _init()
    await _ensure_cluster_started_async()
    assert _cluster is not None and _logger is not None

    if not _cluster.is_running:
        return json.dumps(
            {
                "ok": False,
                "error": "cluster 未运行；设 HUB_CLUSTER_ENABLED=true + 配置 pool 后重启，或调 list_workers 触发 lazy start",
            },
            ensure_ascii=False,
        )

    info = _cluster.info()
    _logger.info("list_workers size=%d", info["size"])
    return json.dumps(info, ensure_ascii=False, indent=2)


@mcp.tool()
async def submit_cluster_task(
    payload: str,
    from_model: str = "unknown",
    pool: str = "",
    acceptance_json: str = "",
    webhook: str = "",
    ctx: Context | None = None,
) -> str:
    """提交一个任务到 cluster 的某个 pool（默认第一个 pool 的 topic），等任意 worker claim 跑。

    参数:
        payload: 任务内容（worker 会作为 prompt 喂给 subagent）
        from_model: 发布者标识（未指定时自动识别调用方平台）
        pool: 派给哪个 pool（按 name 匹配；空 = 第一个 pool）
        acceptance_json: 验收配置（可选；与 publish_task 同格式）
        webhook: 状态变更通知 URL（可选）

    适合批量派活给 DeepSeek 集群（或其他统一模型）处理。
    多 pool 场景下：用 pool="codex" 派到 codex pool，pool="deepseek" 派到 opencode pool。
    """
    _init()
    await _ensure_cluster_started_async()
    assert _cluster is not None and _store is not None and _logger is not None
    caller = _detect_caller(ctx, from_model)

    if not _cluster.is_running:
        return json.dumps(
            {"ok": False, "error": "cluster 未运行；配置 pool 后再调，或调 scale_workers 触发 lazy start"},
            ensure_ascii=False,
        )

    # 选 pool
    target_pool = None
    if pool:
        for p in _cluster.pools:
            if p.spec.name == pool:
                target_pool = p
                break
        if target_pool is None:
            return json.dumps(
                {"ok": False, "error": f"pool '{pool}' 不存在", "available": [p.spec.name for p in _cluster.pools]},
                ensure_ascii=False,
            )
    else:
        target_pool = _cluster.default_pool()
        if target_pool is None:
            return json.dumps(
                {"ok": False, "error": "cluster 没有可用 pool"},
                ensure_ascii=False,
            )

    try:
        acceptance = json.loads(acceptance_json) if acceptance_json else None
    except json.JSONDecodeError as e:
        return json.dumps({"ok": False, "error": f"acceptance_json 解析失败：{e}"})

    for_model = f"{target_pool.spec.runtime}/{target_pool.spec.model}"
    task = await _store.publish(
        topic=target_pool.spec.topic,
        payload=payload,
        from_model=caller,
        for_model=for_model,
        acceptance=acceptance,
        webhook=webhook,
    )
    _logger.info(
        "submit_cluster_task task_id=%s pool=%s topic=%s for_model=%s from=%s",
        task.task_id, target_pool.spec.name, target_pool.spec.topic, for_model, caller,
    )
    return json.dumps(
        {
            "ok": True,
            "task_id": task.task_id,
            "topic": task.topic,
            "pool": target_pool.spec.name,
            "for_model": for_model,
            "from_model": caller,
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def scale_workers(n: int, pool: str = "") -> str:
    """动态调整 cluster 的 worker 数。

    参数:
        n: 目标 worker 数（0 = 停掉 cluster，但 enabled 标志不变）
        pool: 缩放哪个 pool（按 name 匹配；空 = 第一个 pool）

    返回 JSON：{ok, old_size, new_size, pool}
    """
    _init()
    await _ensure_cluster_started_async()
    assert _cluster is not None and _logger is not None

    if not _cluster.is_running:
        return json.dumps({"ok": False, "error": "cluster 未运行；配置 pool 后再调"}, ensure_ascii=False)

    if n < 0:
        return json.dumps({"ok": False, "error": "n 必须 >= 0"}, ensure_ascii=False)

    # 选 pool
    target_pool = None
    if pool:
        for p in _cluster.pools:
            if p.spec.name == pool:
                target_pool = p
                break
    else:
        target_pool = _cluster.default_pool()
    if target_pool is None:
        return json.dumps({"ok": False, "error": f"pool '{pool or '(default)'}' 不存在"}, ensure_ascii=False)

    if n == 0:
        # 缩到 0 = 停这个 pool
        old_size = len(target_pool.workers)
        await target_pool.stop()
        target_pool.spec.size = 0
        _logger.info("scale_workers 0：pool '%s' 已停", target_pool.spec.name)
        return json.dumps(
            {"ok": True, "old_size": old_size, "new_size": 0, "pool": target_pool.spec.name, "stopped": True},
            ensure_ascii=False,
        )

    if not target_pool._worker_tasks:  # noqa: SLF001
        # 没启动过，直接设 size 调 start
        target_pool.spec.size = n
        await target_pool.start()
        _logger.info("scale_workers fresh start: pool=%s size=%d", target_pool.spec.name, n)
        return json.dumps(
            {"ok": True, "old_size": 0, "new_size": n, "pool": target_pool.spec.name, "note": "fresh start"},
            ensure_ascii=False,
        )

    old, new = await target_pool.scale(n)
    _logger.info("scale_workers pool=%s %d → %d", target_pool.spec.name, old, new)
    return json.dumps(
        {"ok": True, "old_size": old, "new_size": new, "pool": target_pool.spec.name},
        ensure_ascii=False,
    )


@mcp.tool()
async def cluster_stats() -> str:
    """cluster 总览：所有 pool 的 worker 状态汇总 + 各自 topic 的队列堆积。

    返回 JSON：
        - pool_count / pools[].size / pools[].busy / pools[].idle
        - pools[].throughput.processed / failed / avg_duration
        - pools[].queue.pending / claimed
    """
    _init()
    await _ensure_cluster_started_async()
    assert _cluster is not None and _store is not None and _logger is not None

    if not _cluster.is_running:
        return json.dumps({"ok": False, "error": "cluster 未运行；配置 pool 后再调"}, ensure_ascii=False)

    info = _cluster.info()
    pool_stats_list = []
    total_processed = 0
    total_failed = 0

    for p in _cluster.pools:
        pinfo = p.info()
        workers = pinfo["workers"]
        n_busy = sum(1 for w in workers if w["status"] == "busy")
        n_idle = sum(1 for w in workers if w["status"] == "idle")
        avg_durs = [w["stats"]["avg_duration_sec"] for w in workers if w["stats"]["avg_duration_sec"] > 0]
        avg_dur = sum(avg_durs) / len(avg_durs) if avg_durs else 0.0
        p_processed = sum(w["stats"]["processed"] for w in workers)
        p_failed = sum(w["stats"]["failed"] for w in workers)
        total_processed += p_processed
        total_failed += p_failed

        topic_peek = await _store.peek(pinfo["topic"], limit=100)
        pending = sum(1 for t in topic_peek if t.status == "pending")
        claimed = sum(1 for t in topic_peek if t.status == "claimed")

        pool_stats_list.append({
            "name": pinfo["name"],
            "runtime": pinfo["runtime"],
            "model": pinfo["model"],
            "topic": pinfo["topic"],
            "size": pinfo["size"],
            "busy": n_busy,
            "idle": n_idle,
            "throughput": {
                "processed": p_processed,
                "failed": p_failed,
                "avg_duration_sec": round(avg_dur, 2),
                "success_rate": round(p_processed / (p_processed + p_failed), 3) if (p_processed + p_failed) > 0 else 0.0,
            },
            "queue": {
                "pending": pending,
                "claimed": claimed,
            },
            "workers": workers,
        })

    return json.dumps(
        {
            "ok": True,
            "cluster": {
                "enabled": info["enabled"],
                "pool_count": info["pool_count"],
            },
            "pools": pool_stats_list,
            "totals": {
                "processed": total_processed,
                "failed": total_failed,
                "success_rate": round(total_processed / (total_processed + total_failed), 3) if (total_processed + total_failed) > 0 else 0.0,
            },
        },
        ensure_ascii=False,
        indent=2,
    )


# ---------- 入口 ----------

def _ensure_cluster_started() -> None:
    """确保 cluster 已经在 mcp 的 event loop 里启动（lazy 启动）。

    cluster.start() 内部用 asyncio.create_task，所以必须在 running event loop 里调。
    mcp.run() 起来后第一个 cluster tool 被调时才执行到这里，正好。
    """
    global _cluster
    if _cluster is None or not _cluster.enabled:
        return
    if _cluster.pool._worker_tasks:  # noqa: SLF001
        return  # 已经启动了
    try:
        # 拿当前 event loop，start() 内部 create_task 会自动用
        loop = asyncio.get_event_loop()
        # start() 自身是 async 但只做 create_task，立即返回
        future = asyncio.ensure_future(_cluster.start())
        # 等待 start() 本身（不等 worker）完成
        # 但 ensure_future 返回的 future 不能用 .result() 在 loop 里 block
        # 简单方案：用 loop.create_task + await
    except Exception:  # noqa: BLE001
        pass
    # 改成直接 await（这是 mcp tool 函数的 async 上下文里）
    # 但 _ensure_cluster_started 是同步函数，调用方是 async tool 函数
    # 改成 async：


async def _ensure_cluster_started_async() -> None:
    """async 版本：在 mcp tool 上下文里调。"""
    global _cluster
    if _cluster is None or not _cluster.enabled:
        return
    # 已经启动了就不用再启
    if _cluster.is_running:
        return
    try:
        await _cluster.start()
    except Exception as e:  # noqa: BLE001
        if _logger is not None:
            _logger.error("cluster 启动失败：%s", e)


def main() -> None:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(
        prog="mcp-hub",
        description="MCP Hub —— 多模型互调/互派活服务",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http", "cluster-only"],
        default="stdio",
        help="MCP 传输方式（默认 stdio，给本地 CLI 工具接；cluster-only 只跑 cluster worker 不启 MCP）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 模式监听地址")
    parser.add_argument("--port", type=int, default=8765, help="HTTP 模式监听端口")
    parser.add_argument(
        "--no-cluster",
        action="store_true",
        help="启动时不跑 cluster（即使 .env 里 HUB_CLUSTER_ENABLED=true）",
    )
    args = parser.parse_args()

    _init()
    assert _logger is not None and _cluster is not None

    if args.no_cluster and _cluster.enabled:
        _logger.info("--no-cluster 覆盖：关闭 cluster 所有 pool")
        for sp in _cluster.specs:
            sp.enabled = False

    if args.transport == "cluster-only":
        # 只跑 cluster，不启 MCP transport（dashboard 派活、worker 认领的独立模式）
        if not _cluster.enabled:
            print("cluster 未启用 —— 在 .env 里设 HUB_CLUSTER_ENABLED=true + HUB_CLUSTER_SIZE=N", file=sys.stderr)
            sys.exit(1)
        _run_cluster_only()
        return

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        # sse / streamable-http
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport=args.transport)


def _run_cluster_only() -> None:
    """cluster-only 模式：起 cluster worker，不启 MCP transport。

    适合：dashboard 独立跑（port 8766）+ mcp-hub server 跑 cluster（cluster-only），
    dashboard 派活到 data/tasks.json，cluster worker 认领 → fork 子 agent 跑。

    多 pool 模式：会启所有 enabled 的 pool，每个 pool 监听自己的 topic。
    """
    import asyncio
    import signal
    import threading

    assert _cluster is not None
    assert _logger is not None

    pool_lines = []
    for p in _cluster.pools:
        pool_lines.append(
            f"  - pool='{p.spec.name}' size={p.spec.size} runtime={p.spec.runtime} model={p.spec.model} topic={p.spec.topic}"
        )
    print(f"mcp-hub cluster-only: {len(_cluster.pools)} pool(s) 启动中", flush=True)
    for line in pool_lines:
        print(line, flush=True)
    print(f"queue: {_settings.hub_queue_path}", flush=True)  # noqa: F821
    print("Ctrl-C 停止", flush=True)

    async def runner():
        await _cluster.start()
        # 保持 event loop 跑着
        stop = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                # Windows 上 asyncio 不支持 add_signal_handler，用 fallback
                pass
        try:
            await stop.wait()
        finally:
            print("停止 cluster ...", flush=True)
            await _cluster.stop()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass
