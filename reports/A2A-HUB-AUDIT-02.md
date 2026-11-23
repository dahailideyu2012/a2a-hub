# A2A-HUB-AUDIT-02 — Read-only technical audit

**审计日期**：2026-11-23（GMT+8）
**审计执行方**：WorkBuddy（只读执行，遵循 `tasks/A2A-HUB-AUDIT-02.md`）
**结论**：**NOT READY FOR PRODUCTION — 可用于本地原型（prototype-ready）**。存在 1 项 P0（默认匿名可驱动 agent + 默认 0.0.0.0 暴露）、多项 P1（CORS 通配+credentials、bypassPermissions 默认、会话/消息/回执无持久化）。
**报告本身是唯一事实来源；本次未修改 README 或任何项目状态文件。**

---

## 1. 审计环境与基线

| 项 | 值 |
|---|---|
| OS | Windows 10 Pro 19045.6466（MINGW64_NT-10.0-19045，x86_64） |
| Python | 3.13.14（隔离 venv `.venv-audit`，pytest 9.2.0 / fastapi 0.165.0 / httpx） |
| Git | 2.55.0.windows.3 |
| 仓库 | `dahailideyu2012/a2a-hub` |
| 审计分支 HEAD | `c9aa47dd421e88643635e3bf5c55f9a45aa1f229`（`docs: add read-only A2A Hub audit task`） |
| 工作副本 | `git worktree add --detach` 到系统 temp（独立目录），未切当前 `feature/chatgpt-workbuddy-bridge-v1` 分支 |
| 工作树状态 | 审计副本初始 clean；venv 与测试 DB 均在 temp，不在仓库内 |
| 应用版本 | `run.py --version` = `a2a-hub 0.6.0`；`/health` 自报 `version 0.2.0`（**版本不一致，见 P2-1**）；Agent Card `version 0.6.0` |
| 隔离措施 | 全部 DB（`audit02-*.db`）、relations.json、日志均在 `%TEMP%`；仅用 echo/memory/local CLI 探测，无付费 API 调用 |

**既有本地改动保护**：主工作区 `H:/soft/netivora/a2a-hub` 始终保持 `HEAD=759b4bb`、`feature/chatgpt-workbuddy-bridge-v1`、仅历史未跟踪 `?? .audit-tmp/`，未做任何切分支/清理/覆盖。

---

## 2. 检查结论汇总（PASS / FAIL / NOT RUN / BLOCKED）

| # | 检查项 | 结论 |
|---|---|---|
| 1.1 | 隔离 venv 依赖安装 | **PASS** |
| 1.2 | `pytest -q`（全量） | **PASS — 633 passed, exit 0** |
| 1.3 | `python run.py --version` | **PASS — a2a-hub 0.6.0, exit 0** |
| 1.4 | `python run.py capabilities` | **PASS — exit 0** |
| 1.5 | `python run.py doctor --json` | **PASS — exit 0, ok=true**（含 2 项 optional 未通过，见正文） |
| 1.6 | Hub 本地启动 + `/health` | **PASS — HTTP 200** |
| 1.7 | Agent Card（`/.well-known/agent.json`） | **PASS — HTTP 200** |
| 1.8 | `/capabilities`、`/agents` | **PASS — HTTP 200** |
| 1.9 | MCP initialize / tools/list / tools/call | **PASS — stdio JSON-RPC，握手+8 工具+调用全部 OK** |
| 1.10 | echo agent 端到端往返 | **PASS — state=completed** |
| 2.1–2.6 | WorkBuddy 双向通信（Personal Desktop） | **BLOCKED — 无真实 `workbuddy` CLI / 桌面桥 / 账户**（详见第 4 节） |
| 2.1–2.6 | WorkBuddy Enterprise | **BLOCKED — 无企业环境/凭据**（不得推断 Personal 具备 Enterprise 能力） |
| 3.x | 安全审计 | 见第 5 节（P0×1 / P1×3 / P2×3，附证据） |
| 4.1 | 持久化：Tasks 重启存活 | **PASS — 隔离 SQLite 重启后 tasks/get 返回 completed** |
| 4.2 | 持久化：Conversations / Messages / Receipts | **FAIL（设计缺口）— 无持久化实现**，见第 6 节 |
| 4.3 | SQLite 并发/备份恢复 | **NOT RUN（inspection only）** — 见第 6 节分析 |
| 5.x | Cloudflare 部署 | **决策结论** — 见第 7 节 |

---

## 3. Part 1 — 环境与运行时证据

### 3.1 全量测试（PASS）
```
$ ./.venv-audit/Scripts/python.exe -m pytest -q --basetemp=<temp>
633 passed, 1 warning in 21.91s        EXIT=0
```
warning：StarletteDeprecationWarning（httpx testclient 弃用），非失败。数字 633 与任务书「633 项测试」精确吻合，确认审计基线正确。

### 3.2 CLI 三命令（PASS）
```
$ python run.py --version            -> a2a-hub 0.6.0                EXIT=0
$ python run.py capabilities         -> 核心/社交/协同能力清单         EXIT=0
$ python run.py doctor --json        -> {"ok": true, "checks":[...]}  EXIT=0
```
`doctor --json` 关键 check：`env.python` PASS（Python 3.13.14）、`env.agents` PASS（config/agents.yaml）、`agents.ready` PASS 但 detail = 「3 个就绪 / 7 个不可用（workbuddy, qwen-office, coze, deepseek, kimi, glm, local-llm）」、`mcp` check `ok=false` optional（WorkBuddy 本机 MCP 未登记，`~/.workbuddy/mcp.json`）。`ok=true` 顶层成立是因为失败项均标 optional。

### 3.3 Hub HTTP（PASS）
独立 uvicorn：`A2A_STORE=sqlite A2A_DB_PATH=<temp> python -m uvicorn a2a_hub.server:app --host 127.0.0.1 --port <free>`
```
GET /health                     -> HTTP 200  {"status":"ok","hub":"A2A Hub","agents":10,"version":"0.2.0"}
GET /.well-known/agent.json     -> HTTP 200  {"name":"A2A Hub","version":"0.6.0","protocolVersion":"0.3.0",...}
GET /capabilities               -> HTTP 200  (capabilities 清单)
GET /agents                     -> HTTP 200  {"count":10, ...}
```
日志提示：`members.yaml 里没有任何成员配置 token —— 门禁会启用，但请求一律按 human:default 处理`（P0 佐证）。

### 3.4 MCP stdio 握手（PASS）
驱动 `mcp_server.py` 的 JSON-RPC over stdio（PROCESS exit 0）：
```
[id=1 initialize]      protocol=2024-11-05 server={'name':'a2a-hub','version':'0.6.0'} caps=['tools']
[id=2 tools/list]      count=8 names=[a2a_agents, a2a_route, a2a_delegate, a2a_collab,
                                      a2a_task, a2a_social_init, a2a_social, a2a_social_act]
[id=3 tools/call a2a_agents] isError=false，返回 10 个 agent 清单
```
设计说明：MCP server 走进程内 `Hub`（不打 HTTP），需装依赖的解释器拉起；写操作默认需 `confirm=true`。

### 3.5 echo 端到端往返（PASS）
```
$ A2A_STORE=sqlite A2A_DB_PATH=<temp> python run.py ask "echo hello-audit-2026" --agent echo
-> taskId=task-fbbfc658de6f4dcdac38ca5bd3303485  state=completed   EXIT=0
```
JSON-RPC `message/send` 通过 `POST /` 亦成功返回 task 与 artifact（见第 5 节 P0 证据）。

---

## 4. Part 2 — WorkBuddy 双向通信矩阵

**严格边界**：任务书禁止用 echo/mock 冒充真实 WorkBuddy 接入。`run.py doctor` 明确把 workbuddy 列为「不可用」，本机 `command -v workbuddy` = NOT FOUND、`WORKBUDDY_CLI` 未设置、`~/.workbuddy/mcp.json` 未登记该 Hub。因此 Personal 与 Enterprise 均按 BLOCKED 处理，**不推断**。

| 通信能力 | Personal Desktop | Enterprise | 证据 / 缺失条件 |
|---|---|---|---|
| 1. WorkBuddy 连接 Hub MCP 端点 | BLOCKED | BLOCKED | 无真实 `workbuddy` CLI（配置默认 `${WORKBUDDY_CLI:-workbuddy} -p {prompt}`，config/agents.yaml:50；adapters/cli_agents.py:387）；MCP 本机登记缺失。MCP server 本身可用（3.4），但那是 Hub 侧能力，不等于客户端已接入。 |
| 2. WorkBuddy 主动发消息给 Hub | BLOCKED | BLOCKED | 同上；无可用真实 CLI 发起。 |
| 3. Hub 调用真实 WorkBuddy CLI | BLOCKED | BLOCKED | 无 CLI 可执行文件；`doctor agents.ready` 标记 workbuddy blocked。未用 echo 代替。 |
| 4. WorkBuddy 接收来自其他 Agent 的任务 | BLOCKED | BLOCKED | 需真实客户端在线领取，环境缺失。 |
| 5. WorkBuddy 返回执行输出 | BLOCKED | BLOCKED | 同上。 |
| 6. 多轮会话上下文保持 | NOT RUN | NOT RUN | 无真实多轮客户端；代码层会话无持久化（见第 6 节），即便接入也存在跨重启上下文丢失风险。 |

**可确认的服务端事实**（非客户端验收）：Hub 暴露 8 个 MCP 工具，具备 `a2a_delegate`（派活）、`a2a_task`（取结果）、`a2a_route`（路由）等双向协作所需原语；`POST /` JSON-RPC、`/tasks`、`/im/conversations/{id}/messages` 等 REST 端点齐备。**服务端能力真实存在，但「真实 WorkBuddy 客户端能用」未获证实。**

---

## 5. Part 3 — 安全审计

### P0-1 — 默认配置下匿名可驱动全部 Agent，且默认绑定 0.0.0.0（P0）
- 位置：`a2a_hub/config.py:46`（`host default="0.0.0.0"`）、`:54`（`api_token default=""`）、`:55`（`require_auth default=False`）；`a2a_hub/server.py:297-335`（`resolve_member` 三档行为）、`:341-344`。
- 影响：无 `members.yaml`（默认）时退化到单 token 模式；`api_token` 为空 → `resolve_member` 直接返回 `Member(human:default)`，**任何人均可无认证调用写端点**。结合默认 `0.0.0.0`，一台公网可达的服务器开箱即可被匿名派任务、触发本地 CLI agent（若 CLI 存在）。
- 复现实证（隔离环境，无 Authorization 头）：
  ```
  POST /  jsonrpc message/send (agentId=echo, 无 token)
  -> HTTP 200 {"jsonrpc":"2.0","id":1,"result":{"id":"task-7949cc1a...","status":{"state":"completed"},...}}
  ```
- 修复：默认改 `require_auth=true` 且绑定 `127.0.0.1`；生产部署强制要求 `A2A_API_TOKEN` 或成员 token；启动即拒绝「require_auth=true 但无 token」（该检查已存在于 :341，但默认值为 false 使其不生效）。

### P1-1 — CORS 通配源 + 允许凭证（P1）
- 位置：`a2a_hub/server.py:387-393`（`allow_origins=["*"]`, `allow_credentials=True`, methods/headers 全 `*`）。
- 影响：任意网站可在浏览器携带凭证跨域调用 Hub 全部端点。
- 复现实证（隔离环境）：
  ```
  OPTIONS /health  Origin: https://evil.example
  -> HTTP 200
     Access-Control-Allow-Origin: https://evil.example
     Access-Control-Allow-Credentials: true
     Access-Control-Allow-Methods: DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT, QUERY
  ```
  （Starlette 反射了 Origin，而非字面 `*`，浏览器侧部分缓解，但语义上等于放行任意站点 + 凭证。）
- 修复：`allow_origins` 显式白名单；凭证场景绝不用 `*`；按环境隔离配置。

### P1-2 — Claude Code 默认 `bypassPermissions`（P1）
- 位置：`config/agents.yaml:85`（`command: "claude -p {prompt} ... --permission-mode bypassPermissions"`）；`a2a_hub/adapters/cli_agents.py:378-381`（`ClaudeCodeAdapter.default_command` 同样含该参数）。
- 影响：Hub 派给 claude-code 的任务以「跳过权限确认」模式运行，编码 agent 可无提示改文件/执行命令；与 P0-1 叠加时，匿名请求即可经 claude-code 执行破坏性操作。
- 修复：默认改为 `default` 权限模式，`bypassPermissions` 需显式开启并配人工审批；本审计已在隔离环境避免触发该 CLI（未做付费/破坏性调用）。

### P1-3 — 会话/消息/回执无持久化（P1）
- 位置：`a2a_hub/store.py:93-155`（SQLite 仅 `tasks` 表）；`a2a_hub/relations.py`（会话/回执为内存结构，仅关系边落 `relations.json`）。
- 影响：Hub 重启后任务结果可见，但多轮会话上下文、消息历史、回执全部丢失（见第 6 节实证）。
- 修复：为 Conversation/Message/Receipt 增加持久化表并与 `A2A_DB_PATH` 统一；重启恢复测试纳入 CI。

### P2-1 — 版本号三处不一致（P2）
- `/health` 报 `version 0.2.0`；`run.py --version` 与 Agent Card 报 `0.6.0`。来源：server.py health 硬编码或取自不同常量。
- 影响：运维/监控难以据 `/health` 判断真实部署版本。修复：统一到 `a2a_hub.__version__` 单一来源。

### P2-2 — 命令注入防护到位（正面结论，P2 观察）
- `a2a_hub/adapters/cli_agents.py:283-299`：`build_argv()` 用 `split_command()` 拆分，`{prompt}` 作为独立 argv 元素替换；全仓无 `shell=True`（仅 `cli.py:90` 有 `os.system("")` 用于 Windows 终端初始化，无用户输入）。**未发现命令注入面。**

### P2-3 — 日志泄密 / 高危操作审批（正面结论 + 观察）
- 未发现把 token/secret 写进日志的代码（grep 仅命中 cli.py:822 的说明文案）。MCP `a2a_social_act` 要求 `confirm=true` 才执行写操作（`mcp_server.py:533-...`）；`capabilities.py:192` 对 `write=true` 能力给出「调用前先征得同意」提示；社交层有 `pending_approvals`/approve/deny 流程（`relations.py:1448+`）。
- 观察：`doctor` 顶层 `ok=true` 掩盖了 optional 项失败（MCP 未登记），可能误导运维认为全绿；建议区分 optional 失败展示。

---

## 6. Part 4 — 持久化与恢复

**方法**：全新隔离 SQLite（`%TEMP%/audit02-persist.db`）+ 全新 relations.json，echo 任务跑通后终止进程，换新端口用同一 DB 重启，再查任务与会话。

```
BEFORE:  message/send -> task-5c94bc07... state=completed
         GET /im/conversations -> {"count":0,"conversations":[]}
AFTER:   tasks/get(task-5c94bc07...) -> HTTP 200 state=completed   ← Tasks 存活
         GET /im/conversations -> {"count":0,"conversations":[]}
DB 表:   [('tasks',)]   task rows: 1
```

- **Tasks：PASS** — SQLite `tasks` 表整条 payload 覆盖持久化（`store.py:116-140`），重启可 `get`/`list`。
- **Conversations / Messages / Receipts：FAIL（设计缺口）** — 仅 Tasks 有表；会话与回执在内存 `SocialGraph`，关系边落 `relations.json`，消息/回执不落盘。跨重启丢失。
- **SQLite 并发**：`SqliteTaskStore` 用 `threading.Lock` + `check_same_thread=False`（:99-100），单进程内写串行化；未做 `WAL`/跨进程并发测试 → **NOT RUN**。建议生产引入 WAL 与迁移方案。
- **备份/恢复**：无备份 API 或脚本；本次以「复制 DB 文件」为理论恢复路径，**NOT RUN**（审计范围外）。
- 全程未碰任何真实用户数据库。

---

## 7. Part 5 — Cloudflare 部署决策

**事实核查**：仓库中**不存在**任何 Workers / D1 / Queues / Durable Objects 实现——无 `wrangler.toml`/`wrangler.json`、无 `package.json`、无 `.ts` 源码、无 Cloudflare SDK 依赖。代码命中 `d1/d2` 是社交距离（degree-1/2）术语，与 Cloudflare D1 无关。当前实现是 FastAPI + uvicorn + sqlite3 + `threading` + `asyncio` + 本地进程间 CLI 调用。

| 维度 | 方案 A：保留 Python Hub + Linux 服务器（可选 Cloudflare DNS/代理/TLS） | 方案 B：重写为 Workers + D1 + Queues + DO |
|---|---|---|
| 兼容性 | 原生兼容：FastAPI/uvicorn/sqlite3/threading/本地 CLI 全部可用 | 需重写：Workers 无 Python 线程/`subprocess`/本地文件系统；D1 无 `threading.Lock` 语义；CLI agent（claude/codex/workbuddy）**无法在 Workers 运行** |
| 工作量 | 低：部署+加固（鉴权、CORS、权限）即可 | 极高：全栈重写 + CLI 执行层另寻宿主机（DO 或容器）+ 测试重写（633 项） |
| 成本驱动 | 一台常驻 Linux 主机 + 域名/证书 | Workers 请求费 + D1 存储/查询 + Queues + DO；常驻 CLI 执行仍需另付主机 |
| 可靠性 | 单主机单点，需自行做 HA/备份 | 边缘高可用，但引入了本仓库不具备的分布式复杂度 |
| 维护 | 沿用现有 Python 团队与代码 | 需 TS/Workers 技能栈，双栈维护或迁移期并行 |
| 关键阻断 | 无（需先解决第 5 节 P0/P1） | CLI agent 生态与 `subprocess` 模型与 Workers 根本冲突，强行重写会丢失核心能力 |

**建议**：采用**方案 A**。先做安全加固（P0-1、P1-1、P1-2），再上 Linux 主机 + Cloudflare DNS/TLS 前置。Cloudflare Workers 仅适合未来把「发现/Agent Card/消息路由」等无状态边缘层单独剥离，**不应为迎合原 Cloudflare 偏好而整体重写**。Do not rewrite merely to force the original Cloudflare preference.

---

## 8. 优先级修复计划

**P0（上线前必须）**
1. `config.py`：默认 `require_auth=true`、`host=127.0.0.1`、生产强制 token；保留「require_auth 但无 token 则拒绝启动」检查。 closure：匿名 `POST /` 必须返回 401。

**P1（原型转生产前必须）**
2. `server.py:387`：CORS 改显式白名单，`allow_credentials` 仅对可信源开启。
3. `config/agents.yaml:85` + `cli_agents.py:378`：`bypassPermissions` 改默认关闭，配人工审批。
4. `store.py`：为 Conversation/Message/Receipt 增加持久化表 + 重启恢复测试。

**P2（可排期）**
5. 统一版本号到 `__version__`（`/health` 0.2.0 vs 0.6.0）。
6. `doctor` 区分 optional 失败展示，避免 `ok=true` 误导。
7. SQLite 引入 WAL + 备份/恢复脚本与 CI。

---

## 9. 未验证项（NOT RUN / BLOCKED 汇总）

- WorkBuddy Personal Desktop / Enterprise 全部 6 项通信：**BLOCKED**（无真实 CLI/账户/企业环境，禁止 mock 冒充）。
- SQLite 跨进程并发与备份恢复：**NOT RUN**。
- 付费/破坏性 CLI 调用（claude-code/真实 LLM）：**NOT RUN**（遵守「不做付费 API 调用」）。
- CORS 浏览器端带凭证的实际攻击（需真实浏览器+登录态）：仅做了响应头层验证，端到端未跑。

---

## 10. 最终结论

**可用于本地原型（prototype-ready），尚不可用于生产（NOT ready for production）。**
服务端核心链路（测试 633 全绿、CLI/HTTP/MCP/Agent Card/echo 往返）真实可用，Cloudflare 应走方案 A。但在 P0-1（默认匿名 + 0.0.0.0）与 P1-1/1-2/1-3 修复前暴露到网络存在实打实的未授权执行风险；WorkBuddy 真实客户端双向接入在本环境无法核验（BLOCKED），必须以真实桌面/企业环境补测后才能宣称闭环成立。
