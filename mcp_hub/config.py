"""配置加载 —— 从环境变量和 .env 文件读取所有 provider 的 API 信息。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelConfig(BaseModel):
    """单个模型的连接配置。"""

    name: str
    provider: str
    api_key: str
    base_url: str
    model: str
    enabled: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)


class ModelAlias(BaseModel):
    """逻辑模型名 → 真实 provider/model 的映射，支持 fallback 优先级。

    例：
      {"alias": "deepseek", "candidates": ["opencode-go/deepseek-v4-pro",
                                              "opencode-go/deepseek-v4-flash",
                                              "opencode/deepseek-v4-flash-free",
                                              "botcf/deepseek-v4-pro"]}
    """

    alias: str
    candidates: list[str]


class HubSettings(BaseSettings):
    """Hub 全局配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 队列
    hub_queue_path: str = "./data/tasks.json"
    hub_log_level: str = "INFO"

    # ===== Anthropic (Claude) =====
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-sonnet-4-5"

    # ===== BotCF Claude（Claude Code CLI 中转） =====
    # 主通道
    botcf_claude_api_key: str = ""
    # Claude Code CLI 用 Anthropic SDK，baseURL 不带 /v1；botcf Anthropic 模式为 https://botcf.com
    botcf_claude_base_url: str = "https://botcf.com"
    # 稳定通道（超时兜底）
    botcf_claude_stable_api_key: str = ""
    botcf_claude_stable_base_url: str = "https://botcf.com"

    # ===== OpenAI (Codex) =====
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"

    # ===== Moonshot (Kimi) =====
    moonshot_api_key: str = ""
    moonshot_base_url: str = "https://api.moonshot.cn/v1"
    moonshot_model: str = "kimi-k2-0905-preview"

    # ===== MiniMax =====
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimaxi.com/v1"
    minimax_model: str = "MiniMax-Text-01"

    # ===== 通用自定义 =====
    custom_openai_api_key: str = ""
    custom_openai_base_url: str = ""
    custom_openai_model: str = ""

    # ===== Model alias JSON（环境变量注入）=====
    # 例：[{"alias":"deepseek","candidates":["opencode-go/deepseek-v4-pro", ...]}]
    hub_model_aliases_json: str = ""

    # ===== Subagent 并发 =====
    # 0 = 不限制（推荐）；其他 = 最大并发数
    hub_max_concurrent_subagents: int = 0

    # ===== OpenCode runtime =====
    # 默认给 `opencode run` 传 --auto（自动批准权限），否则需要审批的工具调用
    # 在非交互模式下会卡住。关掉（false）则不传 --auto。
    hub_opencode_auto: bool = True

    # ===== 超时断线续跑（opencode / codex 原生 session resume）=====
    # 子 agent 超时被 kill 后，若 runtime 支持 resume（supports_resume=True）
    # 且日志里能提取到 session id，自动用同一 session 续跑，最多重试这么多次。
    hub_resume_max_attempts: int = 2

    # ===== Cluster（多 worker 集群）=====
    hub_cluster_enabled: bool = False
    hub_cluster_size: int = 0                  # 默认 worker 数（0 = 不开）—— 老的单 pool 配置
    hub_cluster_runtime: str = "opencode"      # 用哪个 runtime 做 worker —— 老的单 pool 配置
    hub_cluster_model: str = ""                # 统一 model，留空 = 用 hub_cluster_model_default
    # 默认走 OpenCode Go 套餐的 deepseek-v4-flash（用户订阅的，量大便宜）
    # fallback 链只在主 model 跑不通时按顺序切
    hub_cluster_model_default: str = "opencode-go/deepseek-v4-flash"
    hub_cluster_fallback_models: str = ""      # JSON 数组，如 '["botcf/deepseek-v4-flash","opencode/deepseek-v4-flash-free"]'
    hub_cluster_topic: str = "cluster.work"    # 集群消费的 topic —— 老的单 pool 配置
    hub_cluster_workdir: str = "."             # worker 的工作目录
    hub_cluster_concurrency_per_worker: int = 1  # 每个 worker 同时跑几个任务
    hub_cluster_task_timeout_sec: int = 600   # 单任务超时
    hub_cluster_poll_interval_sec: float = 2.0  # 没任务时 sleep 多久再 poll

    # ===== Cluster 多 pool 配置（v3.2 新）=====
    # HUB_CLUSTER_POOLS_JSON 是 JSON 数组，每个元素是一个 pool 配置：
    #   [
    #     {
    #       "name": "deepseek",
    #       "enabled": true,
    #       "size": 3,
    #       "runtime": "opencode",
    #       "model": "opencode-go/deepseek-v4-flash",
    #       "topic": "cluster.work",
    #       "workdir": ".",
    #       "concurrency_per_worker": 1,
    #       "task_timeout_sec": 600,
    #       "poll_interval_sec": 2.0
    #     },
    #     {
    #       "name": "codex",
    #       "enabled": true,
    #       "size": 3,
    #       "runtime": "codex",
    #       "model": "gpt-5.6-terra",
    #       "topic": "cluster.work.codex",
    #       "workdir": ".",
    #       "concurrency_per_worker": 1,
    #       "task_timeout_sec": 600,
    #       "poll_interval_sec": 2.0
    #     }
    #   ]
    # 如果这个字段填了，优先用它；否则用上面老的 HUB_CLUSTER_* 字段包成单 pool。
    hub_cluster_pools_json: str = ""

    # ===== Dashboard 模型白名单 =====
    # 派活 tab 默认显示所有 model（卡片墙很挤）。配这个 JSON 数组只显示这几个。
    # 例：HUB_DASHBOARD_PINNED_MODELS='["opencode-go/deepseek-v4-flash","opencode-go/kimi-k3"]'
    # 留空 = 显示全部
    hub_dashboard_pinned_models: str = ""

    # ===== 模型黑名单（死了的通道）=====
    # 这些模型会被从 runtime 的 list_models() 里过滤掉，dashboard 和派活都看不到。
    # 例：HUB_MODEL_BLOCKLIST='["botcf/deepseek-v4-flash","botcf/deepseek-v4-pro","botcf/mimo-v2.5-pro","zhipuai/glm-5.2"]'
    # 留空 = 不拉黑
    hub_model_blocklist: str = ""

    def model_blocklist(self) -> list[str]:
        """解析模型黑名单。"""
        import json
        if not self.hub_model_blocklist:
            return []
        try:
            data = json.loads(self.hub_model_blocklist)
            return [m for m in data if isinstance(m, str)]
        except json.JSONDecodeError:
            return []

    # ===== provider 白名单（按 provider 限制可用模型）=====
    # 形如 HUB_PROVIDER_ALLOWLIST='{"opencode-go":["deepseek-v4-flash","deepseek-v4-pro"]}'
    # 命中 provider 的模型只有名单内的可见；没列的 provider 不受限。
    # 用途：opencode go 套餐其他模型没性价比，只放行 ds-v4-flash/pro。
    hub_provider_allowlist: str = ""

    def provider_allowlist(self) -> dict[str, list[str]]:
        """解析 provider 白名单。"""
        import json
        if not self.hub_provider_allowlist:
            return {}
        try:
            data = json.loads(self.hub_provider_allowlist)
            return {
                str(k): [m for m in v if isinstance(m, str)]
                for k, v in data.items()
                if isinstance(v, list)
            }
        except json.JSONDecodeError:
            return {}

    def cluster_fallback_models(self) -> list[str]:
        """解析 fallback model 列表。"""
        import json
        if not self.hub_cluster_fallback_models:
            return []
        try:
            data = json.loads(self.hub_cluster_fallback_models)
            return [m for m in data if isinstance(m, str)]
        except json.JSONDecodeError:
            return []

    def cluster_model_resolved(self) -> str:
        """如果 hub_cluster_model 没填，用默认。"""
        return self.hub_cluster_model or self.hub_cluster_model_default

    def cluster_pool_specs(self) -> list[dict]:
        """解析多 pool 配置。优先读 HUB_CLUSTER_POOLS_JSON；空则用老的 HUB_CLUSTER_* 字段包成单 pool。

        返回 list[dict]，每个 dict 是一组 pool 字段（name / size / runtime / model / topic ...）。
        ClusterManager 再把它包成 PoolSpec dataclass。

        如果 hub_cluster_enabled=false，返空列表。
        """
        import json

        if not self.hub_cluster_enabled:
            return []

        if self.hub_cluster_pools_json:
            try:
                data = json.loads(self.hub_cluster_pools_json)
                if isinstance(data, list) and data:
                    # 校验每条至少有 name/runtime/model/topic
                    for i, item in enumerate(data):
                        if not isinstance(item, dict):
                            raise ValueError(f"pool #{i} 不是 dict")
                        for k in ("name", "runtime", "model", "topic"):
                            if k not in item or not item[k]:
                                raise ValueError(f"pool #{i} 缺字段 '{k}'")
                    # 补默认字段
                    for item in data:
                        item.setdefault("enabled", True)
                        item.setdefault("size", 1)
                        item.setdefault("workdir", self.hub_cluster_workdir)
                        item.setdefault("concurrency_per_worker", self.hub_cluster_concurrency_per_worker)
                        item.setdefault("task_timeout_sec", self.hub_cluster_task_timeout_sec)
                        item.setdefault("poll_interval_sec", self.hub_cluster_poll_interval_sec)
                    return data
            except (json.JSONDecodeError, ValueError) as e:
                print(f"[hub] HUB_CLUSTER_POOLS_JSON 解析失败: {e}，回退到老的 HUB_CLUSTER_* 单 pool 配置", flush=True)

        # 回退到老的 HUB_CLUSTER_* 字段（包成单 pool）
        if self.hub_cluster_size <= 0:
            return []
        return [{
            "name": self.hub_cluster_runtime,  # 默认 name = runtime
            "enabled": True,
            "size": self.hub_cluster_size,
            "runtime": self.hub_cluster_runtime,
            "model": self.cluster_model_resolved(),
            "topic": self.hub_cluster_topic,
            "workdir": self.hub_cluster_workdir,
            "concurrency_per_worker": self.hub_cluster_concurrency_per_worker,
            "task_timeout_sec": self.hub_cluster_task_timeout_sec,
            "poll_interval_sec": self.hub_cluster_poll_interval_sec,
        }]

    def model_configs(self) -> list[ModelConfig]:
        """把环境变量摊平成模型配置列表。"""
        configs: list[ModelConfig] = []

        if self.anthropic_api_key:
            configs.append(
                ModelConfig(
                    name="claude",
                    provider="anthropic",
                    api_key=self.anthropic_api_key,
                    base_url=self.anthropic_base_url,
                    model=self.anthropic_model,
                )
            )

        if self.openai_api_key:
            configs.append(
                ModelConfig(
                    name="gpt",
                    provider="openai",
                    api_key=self.openai_api_key,
                    base_url=self.openai_base_url,
                    model=self.openai_model,
                )
            )

        if self.moonshot_api_key:
            configs.append(
                ModelConfig(
                    name="kimi",
                    provider="moonshot",
                    api_key=self.moonshot_api_key,
                    base_url=self.moonshot_base_url,
                    model=self.moonshot_model,
                )
            )

        if self.minimax_api_key:
            configs.append(
                ModelConfig(
                    name="minimax",
                    provider="minimax",
                    api_key=self.minimax_api_key,
                    base_url=self.minimax_base_url,
                    model=self.minimax_model,
                )
            )

        if self.custom_openai_api_key and self.custom_openai_base_url:
            configs.append(
                ModelConfig(
                    name="custom",
                    provider="custom",
                    api_key=self.custom_openai_api_key,
                    base_url=self.custom_openai_base_url,
                    model=self.custom_openai_model or "default",
                )
            )

        return configs

    def model_aliases(self) -> list[ModelAlias]:
        """解析 model alias JSON；如果没配，返回默认的 alias 列表。

        默认 alias 用 OpenCode 协议：provider/model 形式
        """
        import json

        if self.hub_model_aliases_json:
            try:
                data = json.loads(self.hub_model_aliases_json)
                return [ModelAlias(**item) for item in data]
            except (json.JSONDecodeError, TypeError) as e:
                print(f"[hub] 解析 hub_model_aliases_json 失败: {e}", flush=True)

        # 默认 alias 列表 —— 优先用 OpenCode Go/Zen 套餐（用户订阅）
        return [
            ModelAlias(
                alias="deepseek",
                candidates=[
                    "opencode-go/deepseek-v4-pro",       # Go 套餐
                    "opencode-go/deepseek-v4-flash",     # Go 套餐
                    "opencode/deepseek-v4-flash-free",   # Zen 免费（极小任务兜底）
                ],
            ),
            ModelAlias(
                alias="minimax",
                candidates=[
                    "opencode-go/minimax-m2.7",         # Go 套餐
                    "opencode-go/minimax-m3",           # Go 套餐
                    "opencode/minimax-m2.7",            # Zen 免费
                ],
            ),
            ModelAlias(
                alias="claude",
                candidates=[
                    "anthropic/claude-sonnet-4-5",
                    "anthropic/claude-opus-4-6",
                ],
            ),
            ModelAlias(
                alias="kimi",
                candidates=[
                    "moonshot/kimi-k2-0905-preview",
                ],
            ),
            ModelAlias(
                alias="gpt",
                candidates=[
                    "openai/gpt-5.2",
                    "openai/gpt-4o",
                ],
            ),
            ModelAlias(
                alias="gemini",
                candidates=[
                    "antigravity/gemini-3.6-flash-medium",
                    "antigravity/gemini-3.6-flash-high",
                    "antigravity/gemini-3.6-flash-low",
                ],
            ),
            ModelAlias(
                alias="opus",
                candidates=[
                    "antigravity/claude-opus-4-6-thinking",
                    "antigravity/claude-sonnet-4-6",
                ],
            ),
        ]


def load_settings() -> HubSettings:
    """加载配置，自动向上找 .env 文件。"""
    return HubSettings()  # type: ignore[call-arg]


def ensure_queue_dir(queue_path: str) -> Path:
    """确保队列文件所在目录存在。"""
    p = Path(queue_path).expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
