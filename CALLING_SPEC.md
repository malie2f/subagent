# mcp-hub 调用规范（给所有派工 agent 看）

> 版本：2026-08-04 · 适用于 mcp-hub SSE 模式
> 谁再报 `-32602`（Invalid params），先对照本文档自查，90% 是参数格式问题，不是 hub 挂了。
> 2026-08-04 起：-32602 的报错本身就会带真实原因和本清单摘要（不用猜了）；
> hub 重启后客户端重连不重新 initialize 也不再报错（服务端 stateless 容错）。
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
  "wait": true,
  "webhook": "http://127.0.0.1:9000/hook"
}
```

- `runtime` 和 `model` 必须配套（见第 4 节），配错直接 spawn 失败。
- `wait=false` 立即返回 `task_id` 和 `log_file`（死亡现场日志路径），之后用 `subagent_status(task_id)` 查。
- `timeout_sec` 到点 hub 会 kill 直接子进程（opencode/codex 支持 session 的还会自动续跑最多 2 次）。
- `webhook` 可选：终态时 POST 推送 `subagent.done` / `subagent.failed`，body 含 exit_code / exit_code_source / duration_sec / summary / stderr_tail / peak_rss_mb / peak_tokens / log_file。
- `from_model` 不用传，hub 会自动识别调用方平台（优先 MCP 握手 clientInfo.name，再 UA / 父进程 / 环境变量）。

### subagent_status —— 查状态（registry 落盘为准，死后/重启后可查）

- `task_id` 留空 = 列表（新→旧最多 50 条，`total` 是 registry 总条数）；填 ID = 单条详情。
- 详情字段：`status`(running/done/dead) / `exit_code` / `exit_code_source`（real=真实退出码，unknown=进程死透不可考，**不再谎报 0**）/ `duration_sec` / `summary` / `stderr_tail` / `peak_rss_mb` / `peak_tokens` / `session_id`（runtime 原生会话 id，antigravity 的 conversation_id 也映射在这）/ `log_file` / `caller` / `webhook` / `resumed_from` / `is_alive` / `result`（内存里有完整结果时带）。
- `peak_tokens` 是日志里 step-finish 的 context tokens 峰值——zen-v4f 免费池 ~200k 会猝死，盯它判断该不该拆任务。

### resume_subagent —— 死可续（opencode/codex/grok/qoder/codebuddy/antigravity）

```json
{
  "task_id": "原任务ID",
  "task": "（可选）追加指令；留空=自动拼\"基于当前进度继续完成原任务\"",
  "timeout_sec": 600,
  "wait": true
}
```

- 从原任务日志提取 session id，用同一 session 拉起新进程（复用上下文，不用从头来）。
- 优先用 registry 里落盘的 `session_id`（任务终态时已从结果映射进来）；没有再读日志提取。
- 返回新 `task_id` + `session_id` + `resumed_from`；新任务的 status/webhook 行为与 spawn 一致。
- 失败情形都有明确中文报错：registry 查不到 / runtime 不支持 resume / 日志文件没了 / 提取不到 session id。

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

### hedge_* —— hedge-gateway 多模态（qwen 系，OpenAI 兼容）

- `hedge_vision(image, prompt, model=qwen3.8-max-thinking)`：看图理解。`image` 给本地路径（自动 base64，≤20MB）或 http(s) URL（网关代下载）。vision 模型：qwen3.8-max / max-thinking / -128k / -262k、qwen3.7-plus、qwen3-vl-plus。
- `hedge_image_generate(prompt, size, n, out_dir/out)`：生图落盘（已实测）。
- `hedge_video_generate(prompt, duration, resolution, out)`：生视频（网关侧代码在未实测，响应原样透传）。
- `hedge_models()`：列网关模型（130+，qwen3.5–3.8 全系含 -image/-video/-search 后缀）。
- 网关地址和 key 在 dashboard「连接」页填写（或本机 `.env`），发布包不预置。

## 4. runtime ↔ model

**不要写死私人号池/中转。** 以本机 `list_runtimes` / `list_models` 为准，且该 runtime 必须已在 dashboard「连接」页点过连接，否则 `spawn_subagent` 会拒绝。

常见配对（CLI 自己登录后才有模型）：

| runtime | 说明 |
|---|---|
| `opencode` | OpenCode CLI，模型名形如 `provider/model` |
| `codex` | Codex CLI |
| `claude` | Claude Code CLI |
| `antigravity` | Gemini CLI |
| `kimi` / `qoder` / `grok` / `dsh` / `zcode` / `codebuddy` | 本机对应 CLI |

`runtime` 和 `model` 必须配套（`list_runtimes` 里该 runtime 的 models 列表）。配错直接 spawn 失败。

## 5. 派工策略

1. 先打开 dashboard「连接」页，只连接你要用的 CLI。
2. 用 `list_runtimes` / `recommend_model` 选当前机器上真实存在的模型。
3. 不要假设任何预置号池、私人 VPS 或多模态网关。

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
# 先在 dashboard「连接」页连接 opencode，再用 list_runtimes 里真实存在的模型：
spawn_subagent(
  runtime="opencode",
  model="deepseek/deepseek-v4-flash",
  task="用一句话回答：你好",
  workdir=".",
  timeout_sec=60,
  wait=True,
)
```
