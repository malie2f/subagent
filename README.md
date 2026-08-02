# MCP Hub

> **让多个 AI 编程工具（Claude Code / Codex / Kimi Code / OpenCode / MiniMax Code）通过统一协议互相调用、互相派活。**

> **给其他模型/助手看的使用说明 → [MCP_USAGE.md](./MCP_USAGE.md)**

```
┌──────────────────────────────────────────────────────────────────┐
│                        MCP Hub (本项目)                          │
│                                                                  │
│   ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌──────────┐  │
│   │ Anthropic  │  │   OpenAI   │  │  Moonshot  │  │ MiniMax  │  │
│   │  (Claude)  │  │   (Codex)  │  │   (Kimi)   │  │          │  │
│   └────────────┘  └────────────┘  └────────────┘  └──────────┘  │
│                                                                  │
│   6 个 MCP 工具：list_models / call_model / publish_task /       │
│                  claim_task / complete_task / queue_status /     │
│                  dispatch_and_wait                               │
│                                                                  │
│   任务队列（JSON 文件，支持 publish/claim/complete/topic）      │
└──────────────────────────────────────────────────────────────────┘
        ▲           ▲           ▲           ▲           ▲
        │           │           │           │           │
   ┌────┴───┐  ┌────┴───┐  ┌────┴───┐  ┌────┴───┐  ┌───┴────┐
   │ Claude │  │ Codex  │  │  Kimi  │  │ Open-  │  │ Mavis  │
   │  Code  │  │  CLI   │  │  Code  │  │  Code  │  │  Code  │
   └────────┘  └────────┘  └────────┘  └────────┘  └────────┘
```

## 它解决什么问题

每个 AI 编程工具都绑定一个模型（Claude Code 只能用 Claude，Kimi Code 只能用 Kimi...）。但实际工作里：

- 长文档总结 Kimi 强，但 Claude 强在逻辑推理
- 写代码 Claude 强，但 GPT 在某些库上更准
- 国产场景 Kimi/MiniMax 强，但偶尔需要 Claude 兜底

MCP Hub 让 **任意一个 AI 工具可以调用其他 AI 模型**，就像调用一个普通工具一样。

---

## 安装

```bash
git clone <your-repo>  # 或者直接拷代码
cd mcp-hub
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入你有的 API Key
```

> Python 3.10+，推荐 3.12。Windows / macOS / Linux 都能跑。

---

## 配置 `.env`

```bash
# 至少填一个，其他的按需填
ANTHROPIC_API_KEY=sk-ant-xxx        # Claude
OPENAI_API_KEY=sk-xxx                # GPT / Codex
MOONSHOT_API_KEY=sk-xxx              # Kimi
MINIMAX_API_KEY=eyJxxx               # MiniMax

# 自定义（Ollama / OpenRouter / vLLM / 国产私有部署）
CUSTOM_OPENAI_BASE_URL=http://localhost:11434/v1
CUSTOM_OPENAI_API_KEY=ollama
CUSTOM_OPENAI_MODEL=qwen2.5-coder:7b
```

只填哪些 key，hub 就只装载哪些模型。其他没填的模型在 `list_models` 里就看不到。

---

## 三种使用方式

### 方式 1：作为 MCP server 接入其他 CLI 工具

**接入 Claude Code** —— 编辑 `~/.claude.json` 或在项目里用 `.mcp.json`：

```json
{
  "mcpServers": {
    "hub": {
      "command": "python",
      "args": ["-m", "mcp_hub.server"],
      "cwd": "/path/to/mcp-hub"
    }
  }
}
```

接入后，Claude Code 的工具列表里会多出 6 个 hub 工具。你可以这样跟 Claude 说：

> "用 kimi 总结一下我刚贴的这段长文"
> "把这段代码交给 gpt 看看有没有 bug"
> "把这个需求发布成异步任务，等 claude 接手"

**接入 Codex** —— `~/.codex/config.toml`：

```toml
[mcp_servers.hub]
command = "python"
args = ["-m", "mcp_hub.server"]
cwd = "/path/to/mcp-hub"
```

**接入 OpenCode** —— `~/.config/opencode/opencode.json`：

```json
{
  "mcp": {
    "hub": {
      "type": "local",
      "command": ["python", "-m", "mcp_hub.server"],
      "cwd": "/path/to/mcp-hub"
    }
  }
}
```

**接入 Kimi Code** —— `~/.kimi-code/mcp.json`（Kimi Code CLI ≥0.23.5）：

如果 mcp-hub 已经以 SSE 模式跑在 `http://127.0.0.1:8765/sse`：

```json
{
  "mcpServers": {
    "hub": {
      "transport": "sse",
      "url": "http://127.0.0.1:8765/sse",
      "startupTimeoutMs": 30000,
      "toolTimeoutMs": 600000
    }
  }
}
```

如果希望 Kimi 自己 stdio 启动一个 mcp-hub 实例：

```json
{
  "mcpServers": {
    "hub": {
      "command": "python",
      "args": ["-m", "mcp_hub.server", "--transport", "stdio"],
      "cwd": "/path/to/mcp-hub"
    }
  }
}
```

接入后 Kimi 会把 hub 工具当作内置工具使用，例如：

```bash
kimi -p "请用 spawn_subagent 调用 codex 的 gpt-5.6-luna 写一段快速排序"
```

**接入 MiniMax Code (Mavis)** —— 在 Mavis 配置里加 MCP server 即可。

---

### 方式 2：直接用 CLI 调（不需要 MCP 客户端）

```bash
# 列出已配置模型
python -m mcp_hub.cli models

# 直接调一个模型
python -m mcp_hub.cli call kimi "用一句话介绍自己"
python -m mcp_hub.cli call claude "写一个 Python 装饰器：超时报警" --max-tokens 2048

# 发布任务到队列
python -m mcp_hub.cli publish code-review "检查 src/auth.py 的安全性" --for claude

# 认领 + 完成任务
python -m mcp_hub.cli claim code-review --worker gpt
python -m mcp_hub.cli complete <task_id> --worker gpt --result "已检查，3 个问题"

# 看队列状态
python -m mcp_hub.cli status

# 持续打印某个 topic 的新任务
python -m mcp_hub.cli watch code-review
```

---

### 方式 3：作为 Python 库

```python
import asyncio
from mcp_hub.config import load_settings
from mcp_hub.models import build_adapters
from mcp_hub.models.base import ChatRequest, Message

async def main():
    adapters = build_adapters(load_settings().model_configs())
    resp = await adapters["kimi"].chat(
        ChatRequest(messages=[Message(role="user", content="hi")], max_tokens=256)
    )
    print(resp.text)

asyncio.run(main())
```

---

## MCP 工具一览

被 MCP 客户端看到时，工具签名是这样的：

| 工具 | 说明 |
|---|---|
| `list_models()` | 列出 hub 装载的所有模型（直连 API） |
| `call_model(model, prompt, system?, max_tokens?, temperature?)` | 同步调一个模型（直连 API） |
| `list_runtimes()` | 列出可用的 subagent runtime（CLI 子进程型 agent） |
| `recommend_model(task, priority?)` | 根据任务描述推荐最适合的 runtime + model（fast/balanced/quality） |
| `spawn_subagent(runtime, model, task, workdir?, timeout_sec?, wait?, from_model?)` | 真的 fork 一个 CLI 子 agent 进程；`runtime`/`model` 传 `"auto"` 会自动路由 |
| `subagent_status(task_id?)` | 看子 agent 状态 |
| `cancel_subagent(task_id)` | 杀掉子 agent |
| `list_tools()` | 列出可用的多模态/工具型 adapter（mmx 等） |
| `mmx_chat(message, model?, system?, max_tokens?, temperature?)` | mmx 文本对话 |
| `mmx_image_generate(prompt, aspect_ratio?, n?, out_dir?, out?, seed?, model?)` | mmx 图像生成 |
| `mmx_speech(text, voice?, out?, format?, speed?, pitch?)` | mmx 语音合成（TTS） |
| `mmx_music_generate(prompt, out?, lyrics?)` | mmx 音乐生成 |
| `mmx_search(query, count?)` | mmx 搜索 |
| `mmx_vision(image, prompt?)` | mmx 看图理解 |
| `mmx_quota()` | mmx 配额查询 |
| `mmx_voices()` | mmx 语音 preset 列表 |
| `mmx_video_generate(prompt, out?, duration?, resolution?, model?)` | mmx 视频生成（异步） |
| `mmx_video_get(task_id)` | mmx 视频任务状态 |
| `publish_task(topic, payload, from_model?, for_model?, metadata_json?, max_retries?, acceptance_json?, webhook?)` | 异步发布任务 |
| `verify_task(task_id, verifier, passed, score?, issues?)` | 写验收结果 |
| `claim_task(topic, worker, for_model?)` | 认领一个待处理任务 |
| `complete_task(task_id, worker, result, error?)` | 写回任务结果 |
| `queue_status(task_id?)` | 队列总览 / 单任务详情 |
| `dispatch_and_wait(topic, prompt, worker_model, ...)` | 一键派活并等结果（轮询） |

---

## 模型分工与自动路由

hub 里模型很多，但各有各的强项。`recommend_model` 和 `spawn_subagent(..., runtime="auto", model="auto")` 会调用一个 LLM（当前用 MiniMax-M3）根据任务描述智能选择模型，而不是死板的关键词匹配。LLM 失败时会回退到规则路由。

### 按任务类型推荐

| 任务类型 | 推荐模型 | Runtime | 说明 |
|---|---|---|---|
| 中文长文本 / 总结 / 翻译 / 润色 | `kimi-code/kimi-for-coding` | `kimi` | 中文强、上下文长 |
| 代码生成 / 重构 / 调试 / Code Review | `gemini-3.6-flash-medium` | `antigravity` | 速度快，多模态 coding 也胜任 |
| 代码生成 / 重构 / 调试（高质量档） | `gpt-5.6-sol` | `codex` | GPT 5.6 Sol 实力极强，复杂代码首选 |
| 复杂推理 / 数学 / 算法 / 深度分析 | `gpt-5.6-sol` | `codex` | GPT 5.6 Sol 推理深度最高 |
| DeepSeek 通用任务（优先 Go 套餐） | `opencode-go/deepseek-v4-flash` | `opencode` | Go 套餐付费模型，能力强于 free |
| DeepSeek 高质量推理 | `opencode-go/deepseek-v4-pro` | `opencode` | Go 套餐最强 DeepSeek |
| 图像 / 视频 / 截图 / 多模态理解 | `gemini-3.6-flash-medium` | `antigravity` | Gemini 原生多模态 |
| 快速闲聊 / 简单问答 / 一句话任务 | `gemini-3.6-flash-low` | `antigravity` | 响应快、成本低 |
| 批量 / 低成本 / 特小任务 | `opencode/deepseek-v4-flash-free` | `opencode` | OpenCode 免费模型，只做特小任务 |

### 按质量/速度分层

同一类任务还能按优先级微调：

- `fast`：选最快/最便宜的（如 `gemini-3.6-flash-low`、`gpt-5.6-luna`）
- `balanced`：默认均衡档（如 `gpt-5.6-terra`、`gemini-3.6-flash-medium`）
- `quality`：选能力最强的（如 `opus`、`gpt-5.6-sol`）

### 用法示例

让 hub 自动挑模型：

```bash
kimi -p "用 spawn_subagent 自动选个模型，帮我写一段快速排序"
```

在调用里显式传 `auto`：

```json
{
  "runtime": "auto",
  "model": "balanced",
  "task": "帮我写一段快速排序"
}
```

会先走 `recommend_model`，然后实际 spawn 到 `codex/gpt-5.6-terra`。

直接传 model alias（如 `deepseek` / `gemini` / `claude`），`runtime='auto'` 会根据 alias 自动推断正确的 runtime：

```bash
kimi -p "用 spawn_subagent(runtime='auto', model='deepseek') 写一个 Python 二分查找"
```

这会直接 spawn 到 `opencode/opencode-go/deepseek-v4-pro`，不需要手动指定 `runtime='opencode'`。

---

## Subagent Runtimes —— 真·fork CLI 子 agent

`models/` 是直连 API 的轻量调用。`runtimes/` 是不一样的另一层：真的 fork 一个 AI 编程 CLI 进程当子 agent。

**为什么需要这个？**

Claude Code 内部自带 Bash/Edit/Read 工具链和长 context，能自己迭代修代码、跑测试、看 diff。
如果你只想"问一个问题"，`call_model` 够了。但如果你想"让一个 AI 帮我改一个文件树"——`spawn_subagent` 才能用上 CLI agent 的全套能力。

**目前支持的 runtime：**

| Runtime | CLI | 状态 | 备注 |
|---|---|---|---|
| `opencode` | `opencode` 1.18+ | ✅ | OpenCode Go/Zen 套餐，多 provider 都能跑 |
| `claude` | `claude` 2.1+ | ✅ | Claude Code |
| `kimi` | `kimi` 0.23+ | ✅ | Kimi Code |
| `antigravity` | `antigravity` 1.1+ / `agy` | ✅ | Google Antigravity；会自动探测 Windows 系统代理 / 常见本地代理端口 |
| `zcode` | `zcode` 0.15+ | ✅ | 智谱 Z.ai 的 ZCode；走 botcf provider（见下方配置说明） |
| `qoder` | `qoderclicn` 1.1+ | ✅ | Qoder CN CLI；qwen3.8max / qwen3.7plus / DeepSeek-V4 等 |
| `minimax` | — | 🟡 STUB | MiniMax Code 桌面 app 还没修好 CLI 入口 |

**权限自动通过与防挂起**

所有 runtime 在 spawn 时都会：
1. 传各自 CLI 的最强自动批准/权限绕过参数（如 `--auto`、`--dangerously-skip-permissions`、`--mode yolo`、`-p` 等）。
2. 把子进程的 `stdin` 设成 `DEVNULL`：万一 CLI 在 headless 模式下仍尝试读取 stdin 等权限确认，子进程会立即读到 EOF 而失败退出，不会无限挂起。

如果某个任务仍超时，优先排查：任务是否过大需要拆小、模型是否响应极慢、CLI 是否版本太老不支持当前参数。

**Antigravity 代理自动探测**

Antigravity CLI 默认不会自动走系统代理。`mcp_hub/runtimes/antigravity.py` 会在第一次调用时按以下顺序探测可用代理并注入 `HTTP_PROXY`/`HTTPS_PROXY`：

1. 环境变量 `ANTIGRAVITY_PROXY`
2. 环境变量 `HTTP_PROXY` / `HTTPS_PROXY`
3. Windows 系统代理设置
4. 常见本地代理端口：`17891`、`7890`、`7897`、`10808`、`1080`、`8080`、`8888`、`8889` 等

如果探测失败，调用会提示“Authentication required”或网络超时，此时需要手动启动你的代理客户端，或显式设置 `ANTIGRAVITY_PROXY=http://host:port` 后重启 mcp-hub。

**Antigravity Claude 模型容量不足自动回退**

`spawn_subagent` 调用 `opus` / `claude-sonnet-4-6` 等 Antigravity Claude 模型时，如果服务端返回 503 / high traffic / 超时，mcp-hub 会自动重试回退到 `gemini-3.6-flash-medium`。返回结果里会带 `fallback: true`、`original_model`、`fallback_model` 以及警告文本，调用方可以看到发生了回退。

**ZCode 配置说明**

ZCode CLI 从 `~/.zcode/cli/config.json` 读取模型配置。要让 mcp-hub 的 `spawn_subagent(runtime='zcode', ...)` 可用，需要在该文件里写明默认模型和 provider：

```json
{
  "model": "b3cb9134-70b1-4a97-b0c3-adb3a77665ce/glm-5.2",
  "provider": {
    "b3cb9134-70b1-4a97-b0c3-adb3a77665ce": {
      "name": "botcf",
      "kind": "anthropic",
      "options": {
        "apiKey": "sk-xxxxxxxx",
        "baseURL": "https://botcf.com/v1",
        "apiKeyRequired": true
      },
      "source": "custom",
      "models": {
        "glm-5.2": { "limit": { "context": 1000000 }, "modalities": { "input": ["text"], "output": ["text"] } },
        "glm-5.2-fast": { "limit": { "context": 1000000 }, "modalities": { "input": ["text"], "output": ["text"] } }
      },
      "enabled": true
    }
  }
}
```

说明：
- `model` 必须是 `"provider_id/model_id"` 格式的字符串。
- 配置中只保留真正可用的 provider，并把不相关/未授权的 provider 删掉或设为 `"enabled": false`，否则 zcode 校验会报错 "Model config is missing"。
- mcp-hub 的 zcode adapter 会自动读取该配置，按 `provider.models` 生成可用模型列表，并在每次 spawn 时为指定 model 创建隔离的临时 HOME 目录，从而支持 `glm-5.2` / `glm-5.2-fast` 切换。
- 默认使用 `--mode yolo`（自动执行工具）。如果长任务在 headless 子进程里卡住，可设置环境变量 `ZCODE_DEFAULT_MODE=plan` 让 zcode 只输出计划/分析（不会执行文件编辑或工具调用），从而避免交互式阻塞。计划模式适合“先出方案、再由人审阅”的场景，不适合需要自动完成的执行类任务。

**Qoder 配置说明**

Qoder CN CLI（`qoderclicn`）是独立的 AI 编程 Agent，不需要额外 provider 配置；只要命令行已登录（`qoderclicn status` 能显示用户名），mcp-hub 就可以直接调用。

安装：

```bash
npm install -g qoder-cn-cli
# 或
qodercli install
```

登录后确认：

```bash
qoderclicn status
qoderclicn --list-models
```

`mcp_hub/runtimes/qoder.py` 会调用 `qoderclicn --list-models` 自动发现可用模型，常见模型包括 `Qwen3.8-Max-Preview`、`Qwen3.7-Plus`、`DeepSeek-V4-Pro`、`DeepSeek-V4-Flash`、`GLM-5.2`、`Kimi-K2.7-Code`、`MiniMax-M2.7`。spawn 时会自动加 `--print --permission-mode auto --add-dir <workdir>`，确保子 agent 在 headless 环境下自动完成工具调用。

qoder 支持**原生 session 续跑**（`supports_resume=True`）：spawn 时 hub 把 task_id 映射成固定 UUID 传给 `--session-id`，超时或手动续跑时用 `qoderclicn --resume <uuid>` 拉起同一 session，模型能识别原会话上下文（实测记忆完整保留）。与 opencode / codex 一样走 hub 的 `_resume_retry`（最多 `HUB_RESUME_MAX_ATTEMPTS` 次，默认 2）和 dashboard 的手动续跑入口。

**调用方式（任意 MCP 客户端都能用）：**

```
# 让 Claude Code 起一个 opencode 子 agent 去重构代码
> "用 hub.spawn_subagent 开一个 opencode 子 agent，
   跑 '把 src/legacy/*.py 重构成异步'，超时 300s"

# 用 kimi 子 agent 写单元测试
> "用 hub.spawn_subagent(runtime='kimi', model='kimi-k2', 
   task='给 src/auth.py 写 pytest')"

# 用 Antigravity 的 Gemini / Opus 子 agent
> "用 hub.spawn_subagent(runtime='antigravity', model='gemini',
   task='总结一下这段代码')"
> "用 hub.spawn_subagent(runtime='antigravity', model='opus',
   task='审查 src/auth.py 的安全性')"

# 用 ZCode（智谱 GLM）子 agent
> "用 hub.spawn_subagent(runtime='zcode', model='glm-5.2',
   task='给 src/utils.py 写一个二分查找实现')"

# 用 Qoder 子 agent（qwen3.8max）
> "用 hub.spawn_subagent(runtime='qoder', model='Qwen3.8-Max-Preview',
   task='给 src/utils.py 写一个二分查找实现')"

# 传 model alias 让 hub 自动路由到 qoder
> "用 hub.spawn_subagent(runtime='auto', model='qwen3.8max',
   task='给 src/utils.py 写一个二分查找实现')"

# 非阻塞 spawn，task_id 拿回来轮询
> "用 hub.spawn_subagent(wait=False, ...) 然后 hub.subagent_status 看进度"
```

**自己加新 runtime** —— `mcp_hub/runtimes/<name>.py` 继承 `RuntimeAdapter`，在 `runtimes/__init__.py` 的 `REGISTRY` 里登记。三个方法必须实现：`is_available()` / `list_models()` / `spawn()` / `wait()` / `cancel()`。参考 `opencode.py` / `claude.py` / `kimi.py` 任意一个。

---

## 多模态 Tools —— 同步调用型 CLI 工具

跟 `runtimes/` 相对，`tools/` 是"调一下、等结果"的同步工具型 CLI（多模态、搜索等）。

**目前集成的：**

| Tool | CLI | 操作 |
|---|---|---|
| `mmx` | `mmx` (MMX-CLI) | `chat` / `image_generate` / `speech` / `music_generate` / `search` / `vision` / `quota` / `voices` / `video_generate` / `video_get` |

`mmx` 是 MiniMax 多模态 CLI：文/图/音/视频/搜索一把梭。

**调用方式：**

```
# mmx 调 MiniMax-M3 写一段话
> "用 hub.mmx_chat('写一句关于 MCP 的标语', max_tokens=100)"

# 出图
> "用 hub.mmx_image_generate('赛博朋克风的小猫咪', aspect_ratio='16:9', n=2, out_dir='./out')"

# 语音合成
> "用 hub.mmx_speech('你好世界', voice='English_expressive_narrator', out='./hello.mp3')"

# 视频生成（异步）
> "用 hub.mmx_video_generate('日落下的雪山延时', duration=10) → 拿 task_id → mmx_video_get 轮询"
```

**mmx 需要 API Key** —— 从 `.env` 里的 `MINIMAX_API_KEY`（或单独配 `MMX_API_KEY`）读，server 启动时自动注入（mmx CLI 不读环境变量，只能用 `--api-key` flag 传，server 帮你处理）。

**自己加新 tool** —— `mcp_hub/tools/<name>.py` 继承 `ToolAdapter`，在 `tools/__init__.py` 的 `REGISTRY` 里登记。参考 `mmx.py`。然后在 `server.py` 加对应的 `@mcp.tool()` 包装。

---

## 长程任务支持

mcp-hub 对需要跑很久的任务有两套机制：

### 1. 非阻塞子 agent + 状态查询

`spawn_subagent` 的 `wait=False` 会立即返回 `task_id`，主调方可以去做别的事，稍后查询或取消：

```bash
# 非阻塞启动
kimi -p "用 spawn_subagent(wait=False) 让 codex 分析整个项目的依赖问题"

# 之后随时查状态
kimi -p "用 subagent_status 看下任务进度"

# 太慢或跑飞了可以取消
kimi -p "用 cancel_subagent 杀掉它"
```

参数：
- `timeout_sec` 默认 600 秒，长任务可设 1800 / 3600 甚至更高
- `wait=False` 返回 `{ok, task_id, runtime, model, pid, started_at}`
- `subagent_status(task_id)` 看是否还在跑、退出码、stdout 摘要
- `cancel_subagent(task_id)` 强制 kill

### 2. 异步任务队列 + 集群 worker

适合“发布一个任务，等某个 worker 有空了再处理”的场景：

- `publish_task(topic, payload, ...)` 发布任务到队列（持久化到 `./data/tasks.json`）
- `claim_task(topic, worker)` worker 认领任务
- `complete_task(task_id, worker, result)` 写回结果
- `queue_status()` 看队列状态
- `submit_cluster_task(...)` / `cluster_stats()` 一键派活到集群并轮询

Cluster 模式会常驻 N 个 worker 进程并行消费，适合批量长任务。

---

## Cluster —— 多 worker 集群

类似 Kimi 的 "agent 集群"：N 个 worker 常驻，并行消费同一个 topic 任务。每个 worker 真的 fork 一个 subagent runtime 进程跑任务。

**默认场景**：5 个 `opencode-go/deepseek-v4-flash` worker 并行处理 `cluster.work` topic。

**架构：**

```
                     ┌──────────────────┐
  submit_cluster ──→ │   TaskStore      │ ←── claim (worker 抢)
                     │  (cluster.work)  │
                     └──────────────────┘
                              ▲   ▲   ▲
                              │   │   │
                    ┌─────────┘   │   └─────────┐
                    │             │             │
              ┌─────┴────┐  ┌─────┴────┐  ┌─────┴────┐
              │ worker-1 │  │ worker-2 │  │ worker-3 │   (N 个)
              │ opencode │  │ opencode │  │ opencode │
              │ deepseek │  │ deepseek │  │ deepseek │
              │  v4-flash│  │  v4-flash│  │  v4-flash│
              └────┬─────┘  └────┬─────┘  └────┬─────┘
                   │             │             │
                   ▼             ▼             ▼
              ┌────────┐    ┌────────┐    ┌────────┐
              │ task-1 │    │ task-2 │    │ task-3 │   (并行)
              └────────┘    └────────┘    └────────┘
```

**配置**（`.env`）：

```bash
HUB_CLUSTER_ENABLED=true
HUB_CLUSTER_SIZE=5                                  # worker 数
HUB_CLUSTER_RUNTIME=opencode                        # 用哪个 runtime
HUB_CLUSTER_MODEL=opencode-go/deepseek-v4-flash     # 统一 model
HUB_CLUSTER_TOPIC=cluster.work                      # 消费的 topic
HUB_CLUSTER_WORKDIR=.                               # worker 的工作目录
HUB_CLUSTER_CONCURRENCY_PER_WORKER=1                # 每个 worker 同时跑几个任务
HUB_CLUSTER_TASK_TIMEOUT_SEC=600                    # 单任务超时
HUB_CLUSTER_POLL_INTERVAL_SEC=2.0                   # 没任务时 poll 间隔
```

**使用方式（任意 MCP 客户端）**：

```
# 看集群状态
> "用 hub.list_workers 看下当前 worker 都什么状态"

# 提交任务到集群
> "用 hub.submit_cluster_task 让 deepseek 集群把这 10 个文件批量总结一下：
   file 1: ...
   file 2: ...
   ..."

# 动态扩缩
> "流量高峰，scale_workers(20)；流量低谷，scale_workers(3)"

# 总览
> "hub.cluster_stats 看下吞吐和成功率"
```

**或者用 Python SDK 直接驱动**：

```python
import asyncio
from mcp_hub.config import load_settings
from mcp_hub.cluster import ClusterManager
from mcp_hub.queue import TaskStore

async def main():
    settings = load_settings()
    store = TaskStore(settings.hub_queue_path)
    runtimes = ...  # 用 detect_all()
    cm = ClusterManager(
        enabled=True, size=5, runtime_name="opencode",
        model="opencode-go/deepseek-v4-flash",
        topic="cluster.work", workdir=".",
        concurrency_per_worker=1, task_timeout_sec=600,
        poll_interval_sec=2.0, store=store, runtimes=runtimes,
    )
    await cm.start()
    # 等任务来...
    await asyncio.sleep(3600)
    await cm.stop()

asyncio.run(main())
```

**压测**（mock）：

```bash
python smoke_cluster.py
# 输出：3 worker × 5 任务 × 2s → 4.4s（接近 2 批 4s 下限）
```

**适用场景：**
- 批量文件处理（总结/翻译/分类）
- 批量 code review（每个 PR 让一个 worker 跑）
- 批量数据抽取（每个 chunk 让一个 worker 处理）
- 任何 "N 个相同任务 + 单任务耗时 > 5s" 的场景

**横向扩展：**
- 跨机器：把 `TaskStore` 换成 Redis 后端，worker 进程可以分布到多台机器
- 异构集群：现在用 `opencode`，但理论上可以混 `claude` + `kimi` + `opencode` 各自跑自己的模型（每种一个 worker 池）

---

## 实战场景

### 场景 1：让 Claude 调用 Kimi 总结长文档

在 Claude Code 里直接说：

> "用 hub.call_model 调 kimi 总结 /tmp/long-doc.md 的内容，要求 200 字以内"

Claude Code 会自动调用 `call_model(model="kimi", prompt="总结 /tmp/long-doc.md ...")`，然后把结果给你。

### 场景 2：Codex 发布任务，让 Claude Code 异步处理

**终端 1**（Claude Code worker）：
```
你是一个 worker。持续 claim topic="refactor" 的任务，
拿到后用 Edit 工具按要求改代码，然后 complete_task 写回"已修改"。
```

**终端 2**（Codex 触发）：
```bash
python -m mcp_hub.cli publish refactor \
  "把 src/legacy/*.py 重构成异步，所有 IO 调用 await 化" \
  --from codex --for claude
```

### 场景 3：三模型流水线（Kimi → Claude → GPT）

见 `examples/task_chain.py`，启动 3 个 worker 进程：

```bash
# 终端 1
python examples/task_chain.py worker kimi

# 终端 2
python examples/task_chain.py worker claude

# 终端 3
python examples/task_chain.py worker gpt

# 终端 4 —— 触发
python examples/task_chain.py trigger "写一篇关于 MoE 架构的科普短文"
```

流水线：`Kimi 写大纲 → Claude 精修 → GPT 终稿`。

### 场景 4：用 OpenCode 跑 Ollama 本地模型，让 Claude Code 通过 hub 调用

```bash
# .env
CUSTOM_OPENAI_BASE_URL=http://localhost:11434/v1
CUSTOM_OPENAI_API_KEY=ollama
CUSTOM_OPENAI_MODEL=qwen2.5-coder:7b
```

启动 hub 后，Claude Code 就能用本地 qwen 当辅助。

---

## 架构

```
mcp-hub/
├── mcp_hub/
│   ├── config.py            # .env 加载，ModelConfig
│   ├── server.py            # FastMCP server，注册 22 个工具
│   ├── cli.py               # 终端 CLI（不需 MCP 客户端）
│   ├── models/              # 第一层：直连 LLM API
│   │   ├── base.py          # ModelAdapter / ChatRequest / ChatResponse
│   │   ├── anthropic.py     # Claude
│   │   ├── openai.py        # OpenAI/Codex
│   │   ├── moonshot.py      # Kimi (OpenAI 兼容)
│   │   ├── minimax.py       # MiniMax
│   │   └── generic.py       # 通用 OpenAI 兼容（Ollama/OpenRouter/vLLM）
│   ├── runtimes/            # 第二层：fork CLI 子 agent（长任务/有工具链）
│   │   ├── base.py          # RuntimeAdapter / SubagentHandle / SubagentResult
│   │   ├── opencode.py      # OpenCode CLI
│   │   ├── claude.py        # Claude Code CLI
│   │   ├── kimi.py          # Kimi Code CLI
│   │   ├── antigravity.py   # Google Antigravity CLI
│   │   ├── zcode.py         # 智谱 ZCode CLI
│   │   ├── qoder.py         # Qoder CN CLI
│   │   └── minimax.py       # MiniMax Code (STUB，等 CLI 修好)
│   ├── tools/               # 第三层：同步调用型 CLI 工具（多模态/搜索等）
│   │   ├── base.py          # ToolAdapter / ToolResult
│   │   └── mmx.py           # MMX-CLI 多模态
│   ├── cluster/             # 第四层：多 worker 集群
│   │   ├── worker.py        # ClusterWorker / WorkerStats
│   │   ├── pool.py          # ClusterPool（worker 协程循环 + scale）
│   │   └── manager.py       # ClusterManager（对外接口）
│   ├── dashboard/           # 第五层：可观测性面板（Flask web + CLI）
│   │   ├── api.py           # 状态查询（只读）
│   │   ├── server.py        # Flask + CLI 入口
│   │   ├── templates/       # HTML
│   │   └── static/          # CSS / JS
│   └── queue/
│       └── store.py         # TaskStore：publish/claim/complete/status
├── examples/
│   ├── cross_model.py       # 直接 SDK 调用：Claude + Kimi
│   ├── task_chain.py        # 多模型流水线
│   └── simple_client.py     # 纯 Python 调 MCP 工具（不需 LLM）
├── tests/
│   └── test_smoke.py        # 队列逻辑 + 配置加载
├── requirements.txt
├── pyproject.toml
├── .env.example
└── README.md
```

**三层 adapter 的关系：**

| 层 | 调用方式 | 适用场景 | 例子 |
|---|---|---|---|
| `models/` | 直连 HTTP API | 问/答、轻量 prompt | "用 kimi 总结这段" |
| `runtimes/` | fork CLI 进程（异步） | 改代码/跑测试/多步迭代 | "让 claude code 重构 auth.py" |
| `tools/` | fork CLI 进程（同步） | 多模态生成/搜索 | "mmx 出张图" |
| `cluster/` | N × runtimes | 批量并行处理 | "deepseek 集群批量总结 10 个文件" |
| `dashboard/` | Flask web + CLI | 可观测性（看 runtimes/subagents/cluster/tasks 实时状态）| `python -m mcp_hub.dashboard` 浏览器开 |

加新的第三方 AI 工具时，先想清楚它属于哪一层。

---

## Dashboard —— 可观测性面板

mcp-hub 跑起来后总得有个地方看状态。Dashboard 是个独立 Flask web + CLI，**只读不改**，跟 mcp-hub server 解耦。

**Web**（主推）：

```bash
python -m mcp_hub.dashboard                # 默认 http://127.0.0.1:8766
python -m mcp_hub.dashboard --port 9000   # 换端口
```

打开浏览器看 4 个 tab：

| Tab | 看到什么 |
|---|---|
| **Overview** | 4 个 runtime 状态 + tools + cluster 配置 + 队列统计 |
| **Runtimes** | 每个 runtime 详细：binary、是否可用、models 列表 |
| **Subagents** | 子 agent 列表（task_id / claimed_by / 状态 / 时长 / result preview）<br>+ 详情页：完整 stdout / stderr / 模型思考 |
| **Cluster** | cluster 配置 + 队列堆积（pending/claimed/done/failed）|
| **Tasks** | 任务列表（按 topic 过滤，看 payload / status / 错误）|

**CLI**（临时查）：

```bash
python -m mcp_hub.dashboard status                    # 一屏概览
python -m mcp_hub.dashboard subagent <task_id>        # 单个 subagent 详情（tail 日志）
python -m mcp_hub.dashboard watch cluster             # 实时刷新（Ctrl-C 停）
python -m mcp_hub.dashboard tasks --topic refactor    # 任务列表
python -m mcp_hub.dashboard cluster                   # cluster 状态
```

**架构**：
- dashboard 独立进程，不跟 mcp-hub server 共享内存
- 数据源：TaskStore JSON + 子 agent log 文件 + .env 配置 + runtimes `detect_all()`
- 短轮询 2s（不上 SSE，不动 mcp-hub）
- 子 agent 日志支持跨 workdir 扫描（不同任务用不同 workdir 时也能找到）

**L3 / L4 升级路径**（要动 mcp-hub）：
- L3 SSE 事件总线：subagent 状态变化主动 push，dashboard 升级成 push 模式
- L4 流式思考：runtime adapter 边跑边把 stdout 写到 log，前端 SSE 推新行

**当前已实现**：L1（静态状态）+ L2（任务历史 + 日志 tail）

---

### 为什么用文件做队列？

- **零依赖**：装完就能跑，不用再起 Redis/Postgres
- **可观测**：`cat data/tasks.json` 直接看所有任务
- **跨进程**：hub server、CLI worker、MCP client 任何进程都能 publish/claim
- **够用**：单机多模型协作场景下，单文件 + asyncio.Lock 完全够撑

需要横向扩展时，把 `TaskStore` 换成 Redis / Postgres 实现即可，接口不变。

---

## 跑测试

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

队列 + 配置的所有核心逻辑都有测试覆盖。模型 adapter 走 mock 就行，因为调真实 API 测起来贵且不稳定。

---

## 常见问题

**Q: hub server 启动报"未安装 mcp 包"？**
A: `pip install mcp`，Python 3.10+ 推荐 3.12。

**Q: 接入 Claude Code 后工具列表里没看到 hub 的工具？**
A: 检查 `cwd` 路径对不对，python 是不是同一个解释器（`which python`）。Claude Code 的 MCP 进程会从 cwd 启。

**Q: 一个任务 claim 之后 worker 死了怎么办？**
A: 默认 10 分钟超时自动放回 pending，retries+1。可以在 `TaskStore(claim_timeout_sec=...)` 调。

**Q: 想加新模型怎么办？**
A: 在 `mcp_hub/models/` 新建一个 adapter 文件，继承 `ModelAdapter` 或 `OpenAICompatibleAdapter`，然后在 `models/__init__.py` 的 `ADAPTERS` 字典里注册。配置上在 `config.py` 加对应的环境变量。

**Q: 能跑分布式吗？**
A: 文件队列是单机用的。多机协作把 `TaskStore` 换成 Redis（推荐 `aioredis`）或 NATS JetStream，保留 `publish/claim/complete` 三个核心方法签名即可，其他代码不用改。

---

## 路线图

- [x] v0.1 核心：5 个模型 adapter + 文件队列 + 6 个 MCP 工具
- [ ] v0.2 加流式输出（call_model 支持 SSE / NDJSON）
- [ ] v0.3 加 Redis 后端的 TaskStore
- [ ] v0.4 加内置 Web UI（看队列 + 看任务 + 触发）
- [ ] v0.5 加 Anthropic prompt caching / OpenAI prompt cache 自动透传

欢迎 PR。
