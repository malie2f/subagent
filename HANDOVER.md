# 子智能体接手文档

> 产品名 **子智能体**；MCP 接入名 **subagent**；仓库/Python 包仍是 `mcp-hub` / `mcp_hub`。读完应能：拉起服务、分清工人池 vs 任务组、改完不踩坑。模型由用户定义，不绑 mimo。
> 最后更新：2026-09-08（dashboard 关系图 + 思考预览；8765/8766 需同时在听）
> 仓库：`C:\Users\Lenovo\.minimax\agents\mavis\workspace\mcp-hub`
> 本轮改动 **尚未 git commit**（工作区脏）。pytest：**125 passed**。

---

## 这是什么

本机（Windows）子智能体调度。MCP 只是接入方式，不是产品名。三块能力：

1. **子代理调度**：`spawn_subagent` 把活派给本机 CLI（opencode / claude / codex / kimi / grok / antigravity / dsh / qoder / zcode / codebuddy）。死亡现场落 `data/subagents_registry.json`。
2. **任务组（crew）**：共同目标 + 黑板（`crew_post` / `crew_poll`）+ 监督者解卡/纠偏。这才是「多 AI 协作」。
3. **本机工人池（cluster）**：一个 Python 进程里 N 个 asyncio worker 抢 JSON 队列。**不是** Kimi Work 那种云上多 Agent 集群。

另有 hedge / mmx 多模态封装。对外：SSE `http://127.0.0.1:8765/sse` + dashboard `http://127.0.0.1:8766`。工具数以 `list_tools` 为准（远多于旧文档的 29）。

---

## 现在机器上开着什么

| 服务 | 地址 | 说明 |
|---|---|---|
| hub | http://127.0.0.1:8765/sse | 需 `python -m mcp_hub --transport sse --port 8765`。`start_mcp_hub.ps1` 在 hub 已占用端口时往往**只拉 dashboard**。 |
| dashboard | http://127.0.0.1:8766 | 监督 / 集群 tab 有关系图 + 任务/思考预览。前端 `?v=20260909a`，轮询用 Idiomorph morph（不是 innerHTML）。8766 常掉，用 `python -m mcp_hub.dashboard --port 8766`。 |

工人 **不预挂固定池、不绑定任何厂商模型**。集群/任务组只是调度工具；`runtime` + `model` 由调用方传入。模型要先在本机接入（OpenCode provider 或连接页）。mimo 只是某次连通性试验用过的一种模型。

`.env`：`HUB_CLUSTER_ENABLED=false`，`HUB_CLUSTER_POOLS_JSON=[]`。

---

## 两套「集群」不要混

| | 工人池 `cluster/` | 任务组 `crew.py` |
|---|---|---|
| 是什么 | 本机队列工人 | 共同目标 + 黑板 + 监督 |
| 互相说话 | **否** | **是**（`crew_post` / `crew_poll`） |
| 共同目标 | 无（各领各的活） | 有 |
| 监督者 | 无 | dashboard「监督」或 `crew_supervise` |
| 实验 | 工人池已关 | 1936–1972 三人年表，见下 |

工人池：全在本机一个进程；`scale_workers` 加的是协程不是机器；claim 拼 `for_model` 时可能双重前缀 `opencode/opencode/mimo-v2.5-free`（已知小坑）；`list_workers` 可能报 `'size'`。

---

## 任务组怎么用

MCP：

- `crew_create(goal)`
- `crew_add_member(crew_id, role, task, runtime, model, workdir)` — 写 workdir `.mcp.json` 指向 `http://127.0.0.1:8765/sse`
- `crew_post(crew_id, text, from_role, to)` — `to` 空=全员
- `crew_poll(crew_id, since_seq, for_role)` — 把 `last_seq` 记下下次用
- `crew_status` / `crew_supervise(action=unstick|correct|flag_off_track|flag_ok|kill)`

数据：`data/crews.json`。卡死：日志 5 分钟无更新或 pid 死了仍 running。走歪：**不自动发现**，人/另一个模型点「标走歪」。

WebUI：http://127.0.0.1:8766 → **监督** 或 **集群**。任务组画成关系图：监督者、黑板、成员；箭头 = 黑板留言（`crew_post`）和监督动作。点成员节点，下面两个预览窗分别是分工 / 思考+最近输出。`GET /api/crews/<id>/members/<mid>/preview`。年表 markdown **不在网页里渲染**。8766 没起来就看不见。

成员 CLI 必须读项目 `.mcp.json` 才会出现 `crew_post`。OpenCode 实测会。有的 CLI 只认用户级 MCP 配置，则邮局对它是摆设。

---

## 本轮代码改动（未 commit）

实用化：连接页手动联机；发布默认不预置 VPS/密钥/号池；风控 tab 删除；hedge 无内置网关。

修复：`dispatch_and_wait` 无 worker 时自己 claim+spawn+complete；cluster worker 走连接门闩；`resume_subagent` 校验 caller（占位符 unknown/用户不拦）。

新文件：`mcp_hub/connections.py`、`mcp_hub/crew.py`、`tests/test_connections.py`、`tests/test_crew.py`、`tests/test_dispatch_resume.py`。

---

## 1936–1972 实验（测交流是否摆设）

- crew_id：`104c34e4db46`
- 模型：`opencode/mimo-v2.5-free` × 3（archivist-a/b/c）
- 产物：`data/crew-mimo-1936/events-1936-1947.md` 等三个文件

**邮局真通了**：transcript 里有 `mcp-hub_crew_post` / `mcp-hub_crew_poll`，黑板 seq 2–8 是成员自己发的。A poll 到 B/C 已开工后才写「无年段交叉」。

**协作很浅**：没有定向 `to`、没有互改文件、没有对账。三条平行流水线 + 报进度。

**内容质量差**：有条目但不是完整事件集，史实有张冠李戴。那是 mimo 能力，不是通信失败。

---

## 派活 / 回传 / 续聊（源码事实）

`publish_task` 只入 `data/tasks.json`（pending）。认领要么别人 `claim_task`，要么（若用户自己开了）cluster worker。结果：`queue_status` 轮询，或发布时 webhook。`spawn_subagent` 不进队列。

`resume_subagent` **无 ACL**，只有 caller 不同且双方都可识别才拒。支持 resume：opencode/codex/grok/qoder/codebuddy/antigravity。kimi/claude/dsh/zcode 没有原生同会话。

`spawn_subagent` / `resume` / hedge / mmx 要连接页先连。`publish_task` 不检查连接。

现网主力模型名是 `deepseek/deepseek-v4-flash` 等；文档里的 `opencode-go/*` 已经常 404。

---

## 规矩

1. 改完 `python -m pytest -q`，目标 **125 passed**（旧文 124/123/109 过期）。
2. 提交：`git -c user.name=mcp-hub -c user.email=hub@local commit`。**绝不 stage `mcp_hub/runtimes/qoder.py`**。
3. `-32602` 必须带 CALLING_SPEC §2 自查清单。
4. 别碰根目录 RE 产物：`valid_*.bin`、`dis_*.txt`、`ctr_*`。别 commit 也别删。
5. 改 hub 代码必须重启 8765；改 dashboard 静态资源 bump `?v=` 并重启 8766。
6. `.env` / `data/connections.json` / `data/crews.json` 含本机状态，不进发布包。

## 坑

1. Windows spawn 必须 `CREATE_NO_WINDOW`（已收口 asyncio + Popen）。
2. SSE stateless，别依赖 MCP session。
3. `start_mcp_hub.ps1` 不保证杀掉旧 hub 再起新代码。
4. 登录自启计划任务文档写了，系统里经常没有。
5. 仪表盘状态用 `/api/events` SSE（有文件变化才刷），2s 轮询只是 SSE 失败后备。DOM 更新走 `setHtml()` / Idiomorph。Flask 必须 `threaded=True`，否则 SSE 会堵住其它 API。

## 自检

```bash
cd /c/Users/Lenovo/.minimax/agents/mavis/workspace/mcp-hub
python -m pytest -q
# 125 passed
# 8765 和 8766 都应 LISTENING
```
