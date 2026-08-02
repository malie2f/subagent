# Claude Code CLI 接入 mcp-hub 说明

## 1. Claude Code CLI 的 API 模式

Claude Code CLI（`claude` 命令）底层基于 **Anthropic SDK**，通过 `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL` 连接服务端。

BotCF 同时支持两种模式：

| 模式 | Base URL | 适用客户端 |
|---|---|---|
| OpenAI 兼容 | `https://botcf.com/v1` | OpenCode / Codex / 大多数第三方客户端 |
| Anthropic 原生 | `https://botcf.com`（SDK 自动拼 `/v1/messages`，不要手写） | Claude Code CLI |

## 2. mcp-hub 的隔离配置

Claude Code CLI 会读取 `~/.claude/settings.json`，其中 `env` 块的优先级高于进程环境变量。如果你的 `settings.json` 里配了其他服务商（例如 DeepSeek）的 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL`，子 agent 会被全局配置覆盖，导致请求发到错误的后端。

mcp-hub 的 `claude` runtime 已做隔离：每个子 agent 都有独立的临时 home 目录，里面只放当前 provider 的 `settings.json`，确保不会和你本地其他 Claude Code 配置冲突。

## 3. 安装 Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
# 或
claude install
```

安装后确认：

```bash
claude --version
# 期望 >= 2.1.x
```

## 4. 在 `.env` 中配置 key

### 官方 Anthropic key

```env
ANTHROPIC_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ANTHROPIC_BASE_URL=https://api.anthropic.com
```

### botcf-claude（用于 Claude Code CLI）

```env
BOTCF_CLAUDE_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
BOTCF_CLAUDE_BASE_URL=https://botcf.com

BOTCF_CLAUDE_STABLE_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
BOTCF_CLAUDE_STABLE_BASE_URL=https://botcf.com
```

**注意**：`BOTCF_CLAUDE_BASE_URL` 必须是 `https://botcf.com`，不要加 `/v1`。

## 5. mcp-hub 中可用的 Claude 模型

### 官方 Anthropic key

| runtime | model | 说明 |
|---|---|---|
| `claude` | `sonnet` | 最新 Sonnet |
| `claude` | `opus` | 最强推理 |
| `claude` | `haiku` | 轻量 |
| `claude` | `claude-opus-4-6` | 特定版本 |

### botcf-claude

| runtime | model | 说明 |
|---|---|---|
| `claude` | `botcf-claude/claude-opus-5` | 最强推理/兜底 |
| `claude` | `botcf-claude/claude-opus-4-6` | 强推理 |
| `claude` | `botcf-claude-stable/claude-opus-5` | 稳定通道（主通道超时时 fallback） |
| `claude` | `botcf-claude-stable/claude-opus-4-6` | 稳定通道（主通道超时时 fallback） |

调用示例：

```json
{
  "runtime": "claude",
  "model": "botcf-claude/claude-opus-5",
  "task": "...",
  "workdir": ".",
  "timeout_sec": 600
}
```

## 6. 超时与 fallback 策略

- botcf-claude 主通道超时后，hub 会先重试一次主通道；仍超时则切到 `botcf-claude-stable/*`。
- 若 botcf-claude 系列最终失败，且错误属于超时/服务端错误，hub 会自动兜底到 `codex/gpt-5.6-sol`。

## 7. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `API Error: 402 Insufficient Balance` | key 余额不足，或 key 当前分组在该通道下无额度 | 去控制台确认余额/分组；或改用 `runtime="opencode"` |
| `runtime 'claude' 不可用` | Claude Code CLI 没安装 | `npm install -g @anthropic-ai/claude-code` |
| `unknown model` | 模型名不是 runtime 支持的 | 用 `sonnet`/`opus`/`haiku`/`claude-opus-4-6` 或 `botcf-claude/*` |
| 返回内容明显不是预期模型 | 本地 `~/.claude/settings.json` 可能覆盖了 key/baseURL | mcp-hub 已做隔离；如仍异常，检查子 agent 日志中的 `apiKeySource` 和实际请求地址 |

## 8. 历史变更

- **2026-07-27**：修复 Claude Code CLI 配置被用户全局 `~/.claude/settings.json` 覆盖的问题。每个子 agent 现在使用独立的 home 目录和 `settings.json`。
