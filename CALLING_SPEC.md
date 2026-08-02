# mcp-hub 调用规范（给所有派工 agent 看）

> 版本：2026-08-02 · 适用于 mcp-hub SSE 模式
> 谁再报 `-32602`（Invalid params），先对照本文档自查，90% 是参数格式问题，不是 hub 挂了。
> 工具全清单和模型选择经验看 [MCP_USAGE.md](./MCP_USAGE.md)；本文档只讲调用规范。

---

## 1. 接入点

| 用途 | 地址 |
|---|---|
| MCP 工具调用（SSE） | `http://127.0.0.1:8765/sse` |
| Dashboard（看板，只读+派活表单） | `http://127.0.0.1:8766` |
| 队列文件（原始数据） | `mcp-hub/data/tasks.json` |

- 只支持 **SSE 传输**接入远程调用；stdio 模式是给本地 CLI 一对一接的，不要拿来当共享入口。
- 连接后先 `initialize`，再 `list_tools` 确认工具列表，然后 `call_tool`。

## 2. 参数规范（防 -32602）

- **严格按工具 schema 传参**，schema 里没有的字段一律不传（多传 = Invalid params）。
- 类型必须对：`timeout_sec` 是 **整数**，`wait` 是 **布尔**，`max_tokens` 是整数。
- 所有带 `_json` 后缀的参数（`metadata_json`、`acceptance_json`）传的是 **JSON 字符串**，不是对象：
  - ✅ `"metadata_json": "{\"key\": 1}"`
  - ❌ `"metadata_json": {"key": 1}`
- `workdir` 用绝对路径，Windows 下建议 `C:/...` 正斜杠。
- 字符串参数不要传 null，可省略则用默认值。

## 3. 核心工具速查

### spawn_subagent —— 开真 CLI 子代理（最常用）

```json
{
  "runtime": "opencode",
  "model": "opencode-go/deepseek-v4-flash",
  "task": "任务指令全文",
  "workdir": "C:/Users/Lenovo/some/project",
  "timeout_sec": 600,
  "wait": true
}
```

- `runtime` 和 `model` 必须配套（见第 4 节），配错直接 spawn 失败。
- `wait=false` 立即返回 `task_id`，之后用 `subagent_status(task_id)` 查。
- `from_model` 不用传，hub 会自动识别调用方平台（codex/kimicode/claude/...）。

### publish_task / dispatch_and_wait —— 走队列异步派活

- `publish_task(topic, payload, ...)`：扔进队列就返回，由 cluster worker 认领。
- `dispatch_and_wait(topic, prompt, worker_model, ...)`：扔进去并阻塞等结果。

### submit_cluster_task —— 派给集群 pool

- `pool="deepseek"`（opencode-go/deepseek-v4-flash × 3 worker）
- `pool="codex"`（gpt-5.6-terra × 3 worker）

### call_model —— 直连 API 同步调用

- 只用于直调适配器（当前只有 `minimax` 在线）。调 CLI 类模型请用 `spawn_subagent`。

### usage_stats —— token / cost 用量聚合

- 无参数，返回 total / by_model / by_day 三组聚合；opencode/grok/codex/claude/zcode 有数据，其余 runtime 计入 tasks_without_usage。

## 4. runtime ↔ model 配对表（2026-08-02 实测存活）

| runtime | 可用 model | 分工 |
|---|---|---|
| `opencode` | `opencode-go/deepseek-v4-flash` | **主力**，杂活默认走它，快且便宜 |
| `opencode` | `opencode-go/glm-5.2` | 质量档，难活升档 |
| `opencode` | `opencode-go/deepseek-v4-pro` | 质量档备选 |
| `opencode` | `opencode-go/minimax-m3` | 备选 |
| `opencode` | `opencode/deepseek-v4-flash-free` | 免费档，**只给极小任务** |
| `qoder` | `Qwen3.8-Max-Preview` | Qoder qwen3.8max |
| `qoder` | `Qwen3.7-Max` | Qoder qwen3.7max |
| `qoder` | `Qwen3.7-Plus` | Qoder qwen3.7plus |
| `qoder` | `DeepSeek-V4-Pro` | Qoder deepseek-v4-pro |
| `qoder` | `DeepSeek-V4-Flash` | Qoder deepseek-v4-flash |
| `qoder` | `GLM-5.2` | Qoder glm-5.2 |
| `qoder` | `Kimi-K2.7-Code` | Qoder kimi-k2.7-code |
| `qoder` | `MiniMax-M2.7` | Qoder minimax-m2.7 |
| `codex` | `gpt-5.6-sol` | 复杂推理/复杂代码/兜底 |
| `codex` | `gpt-5.6-terra` | GPT 均衡档 |
| `codex` | `gpt-5.6-luna` | 轻量/高频 |
| `antigravity` | `gemini-3.6-flash-low` / `-medium` / `-high` | 快、多模态 coding |
| `opencode` | `botcf/deepseek-v4-flash-free` | 免费档，**只给极小任务** |
| `claude` | `botcf-claude/claude-opus-5` | Claude 最强推理/兜底（Claude Code CLI + botcf Anthropic 模式） |
| `claude` | `botcf-claude/claude-opus-4-6` | Claude 强推理（Claude Code CLI + botcf Anthropic 模式） |
| `claude` | `botcf-claude-stable/claude-opus-5` | 稳定通道（主通道超时时 fallback） |
| `claude` | `botcf-claude-stable/claude-opus-4-6` | 稳定通道（主通道超时时 fallback） |
| `claude` | `sonnet` / `opus` / `haiku` / `claude-opus-4-6` | 官方 Anthropic API key |
| `kimi` | `kimi-code/kimi-for-coding` | 中文/长文本 |
| `zcode` | `glm-5.2` / `glm-5.2-fast` | 需 zcode 已登录 |
| `grok` | `grok-4.5` | **逆向/其它模型拒答的敏感任务**；美国服务必须走代理（hub 自动注入，无需配置）；xAI 订阅额度不按量计费 |
| `qwen`（opencode 通道） | `qwen/qwen3.7-plus` | 可用 |

**红线：**
- `gpt-5.6-*` 必须配 `runtime="codex"`，配 `opencode` 会挂。
- `gemini-3.6-flash-*` 必须配 `runtime="antigravity"`。
- `opencode-go/*`、`opencode/*`、`qwen/*` 配 `runtime="opencode"`。
- `botcf-claude/*`、`botcf-claude-stable/*` 配 `runtime="claude"`（Claude Code CLI + Anthropic SDK，baseURL `https://botcf.com`，**不要带 `/v1`**）。
- `qoder/*` 配 `runtime="qoder"`（qoderclicn）。
- `grok-4.5` 配 `runtime="grok"`（Grok Build CLI，二进制在 `~/.grok/bin/grok.exe`）。
- `claude` runtime 支持官方 Anthropic API key，也支持 botcf。
- mcp-hub 已为每个 Claude Code 子 agent 隔离 `~/.claude/settings.json`，避免你本地其他 Claude Code 配置（如 DeepSeek）覆盖当前 key。
- **死掉的通道（已拉黑，禁止派发）**：`botcf/deepseek-v4-flash`（空包）、`botcf/deepseek-v4-pro`（空包）、`botcf/mimo-v2.5-pro`（空包）、`botcf/饿了么5.2`、`botcf/寄了么5.2`、`botcf/克劳德欧帕兹五点二`、`zhipuai/glm-5.2`（服务端 500）、`minimax/MiniMax-M3` 直连（空响应；要走 `opencode-go/minimax-m3`）。

## 5. 派工策略（约定）

1. 杂活/批量 → `opencode-go/deepseek-v4-flash`
2. 难活/质量敏感 → `opencode-go/glm-5.2`
3. 复杂推理/兜底 → `botcf-claude/claude-opus-5`（claude）或 `gpt-5.6-sol`（codex）
4. 多模态/图相关/要快 → `gemini-3.6-flash-medium`（antigravity）
5. 极小任务才用 free 档。
6. 逆向工程/其它模型拒答的敏感任务 → `grok/grok-4.5`。
7. 模型超时或服务端错误时 hub 会自动兜底到 `codex/gpt-5.6-sol`，不用自己重试。

## 6. 出错自查清单

| 现象 | 先查 |
|---|---|
| `-32602` | 参数名/类型不对；`_json` 参数是不是传了对象；多传了 schema 外字段 |
| spawn 失败、模型不认识 | runtime 和 model 没配套（看第 4 节） |
| 连不上 SSE | hub 没起：`mcp-hub-cli service status` 看状态，`mcp-hub-cli service start` 拉起；`curl http://127.0.0.1:8765/sse` 应返回 `event: endpoint` |
| 任务没人跑 | cluster-only worker 没起；dashboard 集群页看 worker 状态 |
| 想知道谁在跑 | dashboard `http://127.0.0.1:8766` → 子 Agent / 任务 tab |

## 7. 验证连通性的最小用例

```python
# 通过 SSE 发一个最小任务，30 秒内能返回说明链路全通
spawn_subagent(
  runtime="opencode",
  model="opencode-go/deepseek-v4-flash",
  task="用一句话回答：你好",
  workdir=".",
  timeout_sec=60,
  wait=True,
)
```
