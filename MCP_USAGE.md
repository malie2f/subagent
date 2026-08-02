# MCP Hub 使用说明（给其他模型 / AI 助手）

MCP Hub 是一个本地 MCP 服务，让你（当前 AI）把别的模型、别的 CLI 编程助手当成工具调用。

**两个地址**

- MCP 服务（SSE）：`http://127.0.0.1:8765/sse`
- 可视化面板（看子任务/模型/日志）：`http://127.0.0.1:8766`

**一句话原则**

- 简单问答、翻译、总结 → `call_model`
- 需要读写文件、跑命令、多步迭代的编程任务 → `spawn_subagent`
- 不知道用哪个模型 → `recommend_model` 或直接把 `runtime` / `model` 传 `auto`

---

## 1. 先连上

支持 MCP 协议的客户端都能接。以 SSE 为例：

```json
{
  "mcpServers": {
    "mcp-hub": {
      "type": "remote",
      "url": "http://127.0.0.1:8765/sse"
    }
  }
}
```

连上后工具名一般带前缀，例如 `mcp__mcp-hub__spawn_subagent`。

---

## 2. 常用工具速查

| 场景 | 工具 | 关键参数 |
|---|---|---|
| 同步问一个模型 | `call_model` | `model`, `prompt` |
| 派一个 CLI 子 agent 干活 | `spawn_subagent` | `runtime`, `model`, `task`, `workdir`, `wait`, `reasoning_effort` 可选 |
| 让 hub 推荐模型 | `recommend_model` | `task`, `priority` (`fast`/`balanced`/`quality`) |
| 看有哪些 runtime | `list_runtimes` | — |
| 看模型别名 | `list_model_aliases` | — |
| 批量派活给 worker 集群 | `submit_cluster_task` | `payload`, `pool` |
| 看队列/任务状态 | `queue_status` | `task_id` 可选 |
| 多模态（图/语音/视频/搜索） | `mmx_*` | 看各自参数 |

---

## 3. 最小可用示例

### 3.1 同步问模型

```json
{
  "model": "deepseek",
  "prompt": "1+1 等于几？"
}
```

`model` 可以是别名（`deepseek`/`minimax`/`claude`/`kimi`/`gpt`/`gemini`/`opus`），也可以是具体名（`opencode-go/deepseek-v4-flash`、`codebuddy/hy3`）。

### 3.2 派子 agent 改代码（推荐）

```json
{
  "runtime": "opencode",
  "model": "opencode-go/deepseek-v4-flash",
  "task": "把 src/auth.py 里的登录函数拆成两个函数，保持行为不变，跑通 pytest",
  "workdir": "F:/你的项目",
  "timeout_sec": 1200,
  "wait": true
}
```

- `wait: true`：阻塞等结果，返回 `summary/stdout/artifacts`
- `wait: false`：立即返回 `task_id`，子 agent 在后台跑，之后用 `subagent_status(task_id=...)` 或 dashboard 看结果
- `reasoning_effort`（可选）：思考等级 `none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`（是否可用取决于模型——codex/gpt-5.6 全系含 max，DeepSeek V4 官方 API 实测有 high/max；CLI 不校验直接透传），仅 codex/opencode 等支持的 runtime 生效，其余忽略

### 3.3 让 hub 自己选模型

```json
{
  "runtime": "auto",
  "model": "auto",
  "task": "把这个 TypeScript 项目的构建错误修掉",
  "workdir": "F:/你的项目",
  "wait": true
}
```

也可以只让模型 auto：`runtime: "opencode"`, `model: "auto"`。

---

## 4. 当前可用的 runtime 和常用模型

> 跑 `list_runtimes` 拿实时列表；下面是当前稳定可用的组合。

| runtime | 说明 | 常用 model |
|---|---|---|
| `opencode` | OpenCode CLI，模型最多 | `opencode-go/deepseek-v4-flash`、`opencode-go/deepseek-v4-pro`、`opencode-go/kimi-k3` |
| `codebuddy` | CodeBuddy CLI | `hy3`、`glm-5.2`、`deepseek-v4-pro`、`kimi-k2.7` |
| `claude` | Claude Code CLI | `botcf-claude/claude-opus-5`、`botcf-claude-stable/claude-opus-5` |
| `codex` | OpenAI Codex CLI | `gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna` |
| `antigravity` | Antigravity CLI | `gemini-3.6-flash-medium`、`gemini-3.1-pro-high` |
| `grok` | Grok Build CLI（grok-4.5，美国服务） | `grok-4.5` |
| `kimi` | Kimi Code CLI | `kimi-code/kimi-for-coding` |
| `qoder` | Qoder CN CLI | `Qwen3.8-Max-Preview`、`Qwen3.7-Plus` |
| `zcode` | ZCode CLI | `glm-5.2` |

**模型选择经验**

- 特小任务（一句话问答、格式转换、试水验证连通性）：`opencode` + `opencode/deepseek-v4-flash-free`（opencode 官方免费版）
  - ⚠️ 免费/福利类 key 只派特小任务，不要派长任务/批量任务；挂了不要 fallback 重复试，直接换主力分组
  - ⚠️ `deepseek-fuli` 福利 key 独立分组已于 2026-08-01 晚失效（401 invalid key），分组已从 opencode.json 移除；拿到新福利 key 后可重新添加（配置备份见 `~/.config/opencode/opencode.json.bak.20260801145846`）
- 日常杂活、批量任务：`opencode` + `opencode-go/deepseek-v4-flash`（便宜快；⚠️ 走代理会 403，需直连）或 `deepseek/deepseek-v4-flash`（官方 key 直连，稳定）
- 复杂重构、兜底：`codex` + `gpt-5.6-sol` 或 `codebuddy` + `glm-5.2`
- Claude 系任务：`claude` + `botcf-claude/claude-opus-5`（超时自动切 stable）
- 多模态/搜索/语音/视频：直接用 `mmx_*` 工具
- 逆向工程 / 其它模型拒答的敏感任务：`grok` + `grok-4.5`（Grok Build，xAI 订阅额度、不按量计费；美国服务必须走代理——自动复用 antigravity 的代理探测，当前 127.0.0.1:17891；支持 `reasoning_effort=low/medium/high`，默认 high，小任务传 low 省额度；输出里的 `total_cost_usd` 只是刊例价估算，不是真实扣费）

---

## 5. 长任务 / 超时续跑

子 agent 超时会被 kill，但 **opencode / codex / qoder / codebuddy / grok 支持原生 session 续跑**：

- hub 自动用同一 session 续跑最多 2 次
- 你也可以在 dashboard 里点「手动续跑」继续给指令

所以长任务建议：

- `timeout_sec` 给足（1800~3600）
- 优先用支持续跑的 runtime

---

## 6. 常见问题

### 子 agent 只收到任务书第一行 / 空任务书秒退

2026-08-02 已修复：Windows 下 npm `.cmd` shim 经 cmd.exe 转发参数，多行 prompt 会在第一个换行处被截断（input 只剩几个 token，模型反问"请告诉我任务"）。hub 已改为解开 shim 直调真实入口：opencode/claude → 真实 exe；codebuddy/qoder/zcode → node + 脚本。hub 重启后生效；历史日志里 title-only 秒退的任务均属此 bug，原样重发即可。

### `MCP error -32602: Invalid request parameters`

两种已知原因：

1. **hub 刚重启过（最常见）**：MCP 客户端自动重连后，在 initialize 握手完成前发了 tools/call，服务端拒绝。**直接原样重发一次即可**；还红就重连 MCP（kimi 里 `/mcp` 重连或重开会话）。
2. **客户端把 `arguments` 传成了 JSON 字符串**：hub 已做兼容，正常都能解析；如果还遇到，检查客户端是否最新，或换用标准对象传参。

### 子 agent 卡住不动

- 先看 dashboard：`http://127.0.0.1:8766` 的「子 Agent」列表有实时日志和「疑似卡死」标记
- 大部分卡死是权限确认没自动过；hub 对 opencode/qoder/codebuddy 已默认加自动批准，如仍卡住请反馈

### 模型不存在

先跑 `list_runtimes` 看该 runtime 当前支持哪些 model，再精确传入；不要凭印象写模型名。

---

## 7. 快速自检

```bash
# hub 活着？
curl http://127.0.0.1:8765/sse

# dashboard 活着？
curl http://127.0.0.1:8766/api/subagents
```

如果都 200，直接开始用即可。
