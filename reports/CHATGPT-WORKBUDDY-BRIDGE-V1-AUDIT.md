# CHATGPT-WORKBUDDY-BRIDGE-V1 — 只读审计报告（阶段 0 + 阶段 A）

> 执行方：WorkBuddy（本地执行）。派工来源：Issue #5 评论（2026-11-20 01:21:58Z）。
> 审计日期：2026-11-20。性质：**只读审计**——未改业务代码、schema、部署配置；未合并、未部署、未触碰生产。
> 每项结论标注 PASS / FAIL / NOT RUN / BLOCKED，PASS 附真实命令、退出码、输出证据。
> 本报告只陈述事实与证据，不含架构设计结论——设计归 ChatGPT。

## 1. 基线与环境

| 项 | 值 |
|---|---|
| 仓库 | `dahailideyu2012/a2a-hub` |
| 基线分支 | `main` |
| 基线 commit | `df4ef8120d66b2368326bf70c4d926b526a27eab` |
| 工作树状态 | clean（`git status --porcelain` 空） |
| OS | Windows (win32) |
| Python | 3.13.14（隔离 venv：`.venv-audit`） |
| Git | 2.55.0.windows.3 |
| 隔离环境 | `.venv-audit`（本地新建，未改项目依赖文件） |
| Hub 版本 | `run.py --version` → `a2a-hub 0.6.0`；`/health` 报 version `0.2.0`（**版本号不一致，见风险 R6**） |

## 2. 既有 Issue 复核（阶段 0：禁止重复造轮子）

| Issue | 结论 | 证据 |
|---|---|---|
| #1 AUDIT-02 只读审计 | **未完成**（BLOCKED/缺交付） | `origin/audit/A2A-HUB-AUDIT-02` 树中只有 `tasks/A2A-HUB-AUDIT-02.md`，无 `reports/A2A-HUB-AUDIT-02.md`。分支 HEAD `c9aa47d`。 |
| #3 会话/消息/回执持久化 | **未实施** | `a2a_hub/store.py` 仅对 Task 提供 `SqliteTaskStore`；会话层 `a2a_hub/social.py:187 self._convs: dict` 为纯内存，无持久化后端。 |
| #4 双向通信真实闭环 | **未形成矩阵/报告** | 所有本地与远端分支均无 matrix / report / 通信矩阵文件（按文件名与关键词检索）。 |

**结论**：三个前置 Issue 均未产出可复用成果。本阶段审计需自行完成基线事实收集，且**不得把 README 声明当作实现证据**。

## 3. 架构现状（逐项标注）

| 能力 | 实现位置 | 状态 | 证据 |
|---|---|---|---|
| REST API（发现/任务/协同/会话/社交） | `a2a_hub/server.py` | PASS（已实现有证据） | `/health` `/capabilities` `/agents` `/tasks` 均 HTTP 200；route 定义见 server.py 多行 `@app.*` |
| MCP Server | `mcp_server.py` | PASS | stdio `initialize` 成功（serverInfo a2a-hub 0.6.0, protocol 2024-11-05）；`tools/list` 返回 8 个工具 |
| A2A 协议 | `a2a_hub/models.py` / registry | PASS | Agent Card `protocolVersion=0.3.0`；`preferredTransport=JSONRPC`；capabilities streaming=true |
| Agent 注册/发现 | `a2a_hub/registry.py` + `config/agents.yaml` | PASS | doctor：注册 10 个 agent |
| 任务队列/状态机 | `a2a_hub/models.py` TaskState + `store.py` | PARTIAL（部分实现） | 有 A2A v0.3 状态枚举 + Task store；无 `CLAIMED`/`WAITING_APPROVAL` 领取租约/审批态 |
| 会话/消息/回执 | `a2a_hub/social.py` | PARTIAL（仅内存） | Conversation/ChatMessage/DeliveryStatus 数据模型存在，但存储为内存 dict，无重启恢复 |
| 认证 | `a2a_hub/server.py:300-341` + config | PARTIAL（默认关闭） | Bearer Token + 定长比较；`A2A_API_TOKEN` 默认空、`A2A_REQUIRE_AUTH` 默认 false、无 members.yaml token → 请求按 `human:default` 放行 |
| 存储持久化 | `a2a_hub/store.py` | PARTIAL | Task 支持 memory/sqlite（`A2A_STORE` 默认 memory）；会话层无持久化 |
| 部署 | `Dockerfile` + `docker-compose.yml` + `.github/workflows/ci.yml` | PASS（Linux 容器路线） | Docker 用 python:3.12-slim + sqlite + 8080；CI 三 OS × Python 3.10/3.12，compileall + pytest + docker build |

## 4. 通信能力矩阵（四方向）

状态定义：PASS=有真实证据；FAIL=有证据表明不通；BLOCKED=缺环境/账号/授权，无法实证；NOT RUN=未执行。

### 4.1 ChatGPT → Hub

| 维度 | 结论 | 依据 |
|---|---|---|
| 协议/版本 | BLOCKED | ChatGPT 官方支持自定义 MCP（SSE / 流式 HTTP，OAuth 或 no-auth）与 REST 调试接口；但**插件创建需 ChatGPT Business / Enterprise / Edu 工作区**（网页版，管理员/拥有者创建发布；Enterprise 可用 RBAC）。当前账号套餐类型未知，无法证实可创建自定义 MCP 插件。 |
| 连接方向 | 出站（ChatGPT 主动连 Hub 的 HTTPS MCP 端点） | OpenAI 官方文档：输入 server 的 public HTTPS endpoint（如 /mcp），或 Secure MCP Tunnel。 |
| 认证 | 官方支持 OAuth / no-auth / 混合；Hub 现为 Bearer Token | 两侧认证模型不同，需适配层（设计归 ChatGPT）。 |
| 任务提交/领取/进度/流式/结果/回执 | NOT RUN | 本阶段只读，未接入真实 ChatGPT 账号；Hub 侧有 `a2a_delegate`/`a2a_task`/事件流可作对接点，但无真实 ChatGPT 往返证据。 |
| 状态 | **BLOCKED**（账号前提未证实） | 需用户提供 ChatGPT 套餐类型与工作区权限，或改用 OpenAI API 侧独立集成（与"ChatGPT 产品内连接"是两回事，不得混同）。 |

### 4.2 Hub → ChatGPT

| 维度 | 结论 | 依据 |
|---|---|---|
| 主动推送/触发 | BLOCKED | 无证据表明 Hub 可主动向 ChatGPT 对话推送并触发执行。ChatGPT 自定义 MCP 为"ChatGPT 主动调用工具"模型；Hub→ChatGPT 的反向主动通道未在官方能力中出现。 |
| 替代路径 | 概念可行、未实测 | 可通过 ChatGPT 轮询 / 用户在对话中主动拉取任务结果实现"事实上的回读"，但这不是 Hub 主动触发。 |
| 状态 | **BLOCKED** | 不得宣称 Hub 能主动向 ChatGPT 推送/触发。 |

### 4.3 WorkBuddy → Hub

| 维度 | 结论 | 依据 |
|---|---|---|
| 连接方式 | 官方支持（能力具备），本机未登记 | WorkBuddy 官方支持**自定义连接器**：MCP + Skill（远程 HTTPS SSE/streamableHttp，支持 user-supplied token 模式）或 CLI + Skill。腾讯文档亦确认 MCP+CLI / Skill+CLI 两种形态。 |
| 本机实际状态 | BLOCKED / FAIL | `~/.workbuddy/mcp.json` 不存在；doctor 报 `mcp: WorkBuddy（本机） 未登记`。doctor next_step 建议 `python run.py attach workbuddy --register` 后在连接器管理页点"信任"（此动作属配置，审计阶段未执行）。 |
| MCP 握手（隔离直连 stdio） | PASS | 直接对 `mcp_server.py` 发 initialize + tools/list：握手成功，8 工具可用。证明**协议层可用**，但未走 WorkBuddy 连接器。 |
| 任务提交/领取/进度/流式/结果 | NOT RUN（未走真实连接器） | 工具齐全（`a2a_delegate`/`a2a_task`/`a2a_collab`），但"工具存在"≠"WorkBuddy 已接入并跑通"。 |
| 状态 | **BLOCKED**（连接器未配置） | 需完成 MCP 登记 + 信任，才能做真实 WorkBuddy→Hub 往返。 |

### 4.4 Hub → WorkBuddy

| 维度 | 结论 | 依据 |
|---|---|---|
| 主动调用 WorkBuddy CLI | FAIL（当前环境） | doctor：workbuddy agent `status=unavailable`，detail=`PATH 中找不到 workbuddy，请在 agents.yaml 的 command 里写完整路径`。配置命令为 `${WORKBUDDY_CLI:-workbuddy} -p {prompt} --output-format stream-json`（agents.yaml:50）。本机无 WorkBuddy CLI 可执行文件在 PATH。 |
| echo 往返 | PASS（仅证明 echo，不证明 WorkBuddy） | doctor probe：`agent=echo, state=completed, echoed=true`。按规则，echo/mock **不得**作为真实 WorkBuddy 集成证据。 |
| 接收来自其他 Agent 的任务并回传 | NOT RUN / BLOCKED | 需真实 WorkBuddy CLI 或 Personal Desktop / Enterprise 可用入站方式；当前均不可用。 |
| Personal Desktop vs Enterprise | BLOCKED | 无 Enterprise 环境；Personal Desktop 的官方入站（Hub 主动触发）方式未获证据支持。不得从 Personal 推断 Enterprise 能力。 |
| 状态 | **FAIL（本机环境）/ BLOCKED（真实端到端）** | 需提供 WorkBuddy CLI 路径或真实连接器入站能力证明。 |

## 5. 复用矩阵（避免重复造轮子）

| 现有能力 | 可复用性 | 说明 |
|---|---|---|
| MCP Server + 8 工具 | 高 | 可直接作为 ChatGPT / WorkBuddy 的工具对接面；需补认证适配。 |
| REST API（tasks/collab/im） | 高 | 可作备用/调试接口与回读通道。 |
| A2A v0.3 数据模型 + SSE 事件 | 中 | 状态机需扩展领取/审批态（Issue #5 新状态），不可直接等同。 |
| Task Store（memory/sqlite） | 中 | Task 持久化可复用；会话/消息/回执持久化缺失（Issue #3 待办）。 |
| Agent 注册表 + 社交门禁 | 中 | 身份与权限基础可复用；默认未启用 token。 |
| echo adapter | 低 | 仅测试用途，不得当真实集成。 |

## 6. 测试与静态检查（真实命令/退出码/统计）

| 检查 | 命令 | 退出码 | 结果 |
|---|---|---|---|
| 环境 | `python3 -m venv .venv-audit` + pip 安装依赖 | 首次整体解析=1（ResolutionImpossible）；逐个安装=0 | 依赖最终装齐；稳定版 pip 对宽松下限版本首次整体解析失败，记为环境注意项 |
| 版本 | `.venv-audit/Scripts/python.exe run.py --version` | 0 | `a2a-hub 0.6.0` |
| 能力清单 | `run.py capabilities` | 0 | 列出 agents/ask/collab/task/chat/social-read |
| 体检 | `run.py doctor --json` | 0 | `ok=true`；3 就绪/7 不可用；workbuddy unavailable；MCP 未登记；社交门禁无 token |
| 测试 | `.venv-audit/Scripts/python.exe -m pytest -q` | 0 | **633 passed, 1 warning in 21.98s**；warning 为 StarletteDeprecationWarning（httpx 与 testclient） |
| 静态检查 | `.venv-audit/Scripts/python.exe -m compileall -q a2a_hub tests run.py` | 0 | 无输出（通过） |
| HTTP /health | `curl http://127.0.0.1:18080/health` | 0 | HTTP 200 `{"status":"ok","agents":10,"version":"0.2.0"}` |
| HTTP Agent Card | `curl /.well-known/agent.json` | 0 | HTTP 200，protocolVersion 0.3.0 |
| HTTP /capabilities /agents /tasks | curl | 0 | 均 HTTP 200 |
| MCP 握手 | 向 `mcp_server.py` 发 initialize + tools/list（stdio） | 0 | initialize OK；tools/list=8 |

测试服务使用隔离 DB（`/tmp/a2a-audit-test.db`），未触碰任何真实用户数据库。服务测试后已关闭。

## 7. 风险清单（证据位置，不含整改方案）

| ID | 风险 | 证据 | 影响面 |
|---|---|---|---|
| R1 | 认证默认关闭 | `config.py:54-55` A2A_API_TOKEN 空、A2A_REQUIRE_AUTH=false；无 members.yaml token → server.py 按 `human:default` 放行 | 公网部署下匿名可调用；Issue #2 关注 |
| R2 | 会话/消息/回执无持久化 | `social.py:187 _convs` 内存 dict | 重启丢失；Issue #3 关注 |
| R3 | 任务状态机缺领取/审批态 | `models.py:165 TaskState` 无 CLAIMED/WAITING_APPROVAL | 与 Issue #5 状态要求不一致 |
| R4 | WorkBuddy agent 不可用 | doctor detail：PATH 无 workbuddy；agents.yaml:50 用 `${WORKBUDDY_CLI:-workbuddy}` | Hub→WorkBuddy 方向当前不通 |
| R5 | WorkBuddy MCP 未登记 | doctor `mcp`=false；`~/.workbuddy/mcp.json` 不存在 | WorkBuddy→Hub 未接入 |
| R6 | 版本号不一致 | `/health` version `0.2.0` vs Agent Card / `run.py --version` `0.6.0` | 版本单一来源声明与实测不符（注意：基线 commit 消息称"收敛版本号单一来源"） |
| R7 | 社交会话绕过 token 的路径 | `server.py:973` 附近注释提及绕过 `A2A_API_TOKEN` 的会话访问 | 需安全评估（Issue #2） |
| R8 | 依赖解析不稳定 | 首次 `pip install -r requirements.txt` ResolutionImpossible（pip 26 / Python 3.13）；逐个安装成功 | CI 固定 Python 3.10/3.12，本地 3.13 需注意 |
| R9 | ChatGPT 账号前提未知 | 官方文档：自定义 MCP 插件创建限 Business/Enterprise/Edu 工作区 | 决定 ChatGPT→Hub 方向是否可行 |

## 8. 下一阶段实施拆分建议（仅列任务边界，不含设计）

- B：认证强化 + 任务状态机扩展 + 会话/消息/回执持久化（对应 Issue #5 阶段 B、Issue #3）。
- C：WorkBuddy Connector（领取租约、执行回传）——前置：R4/R5 解除（CLI 路径 / MCP 登记 + 信任）。
- D：Remote MCP Server 适配 + ChatGPT 接入——前置：R9 明确（账号套餐/工作区权限）与 4.2 反向通道可行性决策。
- E：真实双轮端到端 + 断线恢复 + 安全测试——前置：C、D 的真实环境就绪；echo 不计入。
- F：文档、PR、验收报告。

上述拆分仅为边界划分与依赖关系，具体技术方案与实现由 ChatGPT 设计。

## 9. 本阶段验收对照（Issue #5 第 4 节）

- [x] 已复核 Issue #1/#3/#4，不重复实现（均未产出成果，见 §2）
- [x] 有真实 main 基线 commit 与工作树状态（§1）
- [x] 有完整通信矩阵，未知能力明确 BLOCKED（§4）
- [x] 测试/静态检查有真实命令、退出码和统计（§6）
- [x] 审计报告已提交到独立分支（本报告）
- [x] main、生产环境和现有数据均未修改（隔离 DB，未碰生产）

## 10. 未解决问题 / 需用户提供的环境与授权

1. ChatGPT 账号套餐类型与工作区 MCP 插件权限（决定 4.1 是否可行）。
2. WorkBuddy CLI 可执行文件路径，或是否允许执行 `attach workbuddy --register` + 连接器信任（解除 R4/R5）。
3. 是否存在 WorkBuddy Enterprise 环境及其官方入站接口（决定 4.4 可行性）。
4. Hub 部署形态：当前为 Linux/Docker + sqlite，规划书倾向 Cloudflare Workers；两者路线需 ChatGPT 决策，不在本审计改动范围。
