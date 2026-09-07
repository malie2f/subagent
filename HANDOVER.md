# mcp-hub 接手文档

> 写给下一位维护者/接手 agent。读完这份应该能：把服务拉起来、知道钱和流量从哪走、改完不踩坑。
> 最后更新：2026-09-08（UI 硬边缘+双主题改版后）

## 这是什么

一个跑在本机（Windows）的 MCP 服务，两件事：

1. **子代理调度器**：把任务派给本机各种 CLI agent runtime（opencode / codex / kimi / grok / antigravity / dsh 等），带队列、验收流水线、cluster 池、死亡现场记录。
2. **多模态工具网关**：封装 hedge-gateway（qwen 系视觉/生图/生视频）和 mmx（MiniMax 全家桶）成 MCP 工具。

对外形态：一个 MCP server（SSE），任何 MCP 客户端接上就有 29 个工具可用；外加一个只读监控 dashboard。

## 服务拓扑

| 服务 | 地址 | 启动方式 |
|---|---|---|
| hub MCP 服务 | http://127.0.0.1:8765/sse | `python -m mcp_hub --transport sse --port 8765` |
| dashboard | http://127.0.0.1:8766 | `python -m mcp_hub.dashboard --port 8766` |

两个都由 `start_mcp_hub.ps1` 拉起（已在跑就跳过）。**注意：脚本注释说挂了登录自启计划任务，但 2026-09-08 实测系统里查不到这个任务——重启机器后要手动跑一次脚本。**

### 重启操作手册（Git Bash）

```bash
# hub（8765）
PID=$(netstat -ano | grep '127.0.0.1:8765' | grep LISTENING | awk '{print $NF}' | head -1) && taskkill //PID $PID //F
# dashboard（8766）同理换端口。然后：
cd /c/Users/Lenovo/.minimax/agents/mavis/workspace/mcp-hub
powershell -NoProfile -ExecutionPolicy Bypass -File start_mcp_hub.ps1
# 确认：netstat 看两个端口都 LISTENING；日志在 logs/hub.err.log / dashboard.err.log
```

dashboard 改前端（templates/static）必须重启才生效（模板有缓存版本号 `?v=`，改了记得 bump，当前 `v=20260908`）。

## 代码地图

```
mcp_hub/
  server.py       MCP server 入口，工具注册/信封/错误富化
  cli.py          CLI 入口
  config.py       环境变量加载（.env）
  registry.py     子代理注册表（死必报/死可查的落盘）
  routing.py      模型 alias → 真实 runtime/model 路由
  fallbacks.py    alias fallback 链
  usage_stats.py  token/成本聚合
  notify.py       webhook 通知
  runtimes/       每个 CLI agent 一个适配器（base.py 是契约）
  tools/          hedge.py（qwen 多模态）、mmx.py（MiniMax）
  queue/          任务队列（topic/claim/complete/verify）
  cluster/        worker 池（scale_workers 动态伸缩）
  dashboard/      server.py + api.py + templates/index.html + static/{app.js,style.css}
data/             subagents_registry.json、tasks 队列、dashboard_state.json（前端白名单）
logs/             服务日志
tests/            pytest，基线 109 过
```

## 外部依赖（挂了先查这里）

| 依赖 | 位置 | 用途 |
|---|---|---|
| hedge-gateway | VPS 202.60.229.202:27941（实为 qwen2api docker 容器） | qwen 聊天/视觉/生图/生视频 |
| zen-gost HTTP 代理 | VPS :27942 | 生图 CDN 下载回退（见"坑"#3） |
| hedge-gateway Go 二进制 | VPS :27945（/root/hedge-gateway/） | 备用网关，当前未挂进 hub |
| VPS SSH | `ssh -i ~/.ssh/vps_qwen root@202.60.229.202` | 唯一可用的钥匙（kimi_vps_key 不在授权列表） |
| mmx | MiniMax API（.env 里 MINIMAX_*） | 语音/音乐/视频/搜索 |
| moonshot / botcf-claude | .env 里各自 KEY/BASE_URL | 直连模型 API |

配置全在 `.env`（`config.py` 读）。cluster 池子由 `HUB_CLUSTER_*` 系列变量定义。

## 前端（dashboard）

- 三件套：`templates/index.html` + `static/style.css` + `static/app.js`，无构建步骤，改完重启即生效。
- **风格规约（2026-09-08 起）：硬边缘，禁止 border-radius / 软阴影；双主题「月之暗面」（默认）/「月之亮面」，全部颜色走 CSS 变量**——`:root` 是暗面，`html.moon-light` 覆盖成亮面，加新颜色时两处都要加变量，不许写死 hex。
- 主题切换按钮在 header（`#theme-toggle`），存 localStorage `mcp-hub-theme`；防闪烁由 `<head>` 内联脚本负责。
- tab：总览 / 派活 / 运行时 / 子 Agent / 集群 / 任务 / 用量 / 风控。
- 派活 tab 的模型白名单存 `data/dashboard_state.json`，重启不丢。

## 规矩（违反会被打回）

1. **测试**：改完跑 `python -m pytest -q`，基线 109 过才算完。
2. **提交**：`git -c user.name=mcp-hub -c user.email=hub@local commit`。**绝不 stage `mcp_hub/runtimes/qoder.py`**——那是别人的 WIP。
3. **工具报错要带教程**：调用方参数错了必须返回 CALLING_SPEC.md §2 自查清单（信封层 -32602 和参数校验层都要），这是和其他 agent 协作的命根子，见 `CALLING_SPEC.md`。
4. **别碰根目录的 RE 产物**：`valid_*.bin`、`dis_*.txt`、`ctr_*` 等是另一个逆向项目 subagent 倒进来的，不属于本仓库，别 commit 也别删（删前问）。
5. MCP 客户端会话重启后 hub 要重启才能被本会话重新发现工具——改完 hub 代码记得重启 8765。

## 坑（都踩过，别重踩）

1. **Windows 子进程弹窗**：所有 spawn 必须过 `CREATE_NO_WINDOW` 注入（已收口到 asyncio + subprocess.Popen 两层），绕开就会弹一堆空控制台窗口把浏览器搞崩。
2. **SSE stateless**：ServerSession 是强制 stateless 的，hub 重启后客户端重连不会重新 initialize，别加依赖 session 状态的逻辑。
3. **cdn.qwenlm.ai 本机被 RST**：生图生成端一直好的，坏的是下载。`hedge.py` 的 `_download` 直连失败会自动走 VPS zen-gost :27942 代理（CONNECT 隧道，重试 3 次防上游轮询死节点），`HEDGE_DOWNLOAD_PROXY` 可覆盖、设 `off` 禁用。若 zen 号池重整，这条回退会一起挂。
4. **子代理猝死**：`spawn_subagent` 有死亡现场（exit code/RSS/日志尾部落 registry）、webhook 终态通知、`resume_subagent` 续跑。排查先看 `data/subagents_registry.json` 和交付文件，再调 `subagent_status`。
5. **qwen2api 上游风控**：agentic 调用偶发被上游拦，zen-v4f 免费池走 `opencode/zen-v4f/deepseek-v4-flash-free`。

## 近期变更时间线（倒序）

- `128e5d8` dashboard 硬边缘 UI + 月之暗面/月之亮面双主题（默认暗面）
- `8cef149` hedge 图片 CDN 下载代理回退
- `ca26e87` 接入 DeepSeek Harness（dsh --profile headless）
- `65d011b` tokenrhythm（基元律动）接入
- `c0cb02d` 风控 tab（调用热力图 + 账号风控表）
- `b19ca38` DeepSeek 官方 v4-pro 走 opencode 接入
- `fd40f47` antigravity 稳定性 + 会话映射
- `a695272` 子代理死必报/死可查/死可续（P0）

## 上手自检清单

```bash
cd /c/Users/Lenovo/.minimax/agents/mavis/workspace/mcp-hub
python -m pytest -q          # 应 109 passed
netstat -ano | grep -E '127.0.0.1:(8765|8766)' | grep LISTENING   # 两个都该在
curl -s http://127.0.0.1:8766/ -o /dev/null -w '%{http_code}'     # 200
```

再开一个 MCP 客户端调 `list_runtimes` / `list_models`，能出列表 = 全链路通。
