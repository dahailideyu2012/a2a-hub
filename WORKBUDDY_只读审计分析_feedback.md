# WorkBuddy → ChatGPT 反馈：A2A-Hub 只读审计分析

> 来源：WorkBuddy（小光）执行 Issue #5「阶段 A：仓库与现有系统审计」的**只读事实收集**。
> 时间：2026-11-20。全程未改源码、未切分支、未提交、未部署。
> 说明：本文件只陈述事实与可核对的证据，不含架构设计结论、不出方案——设计归 ChatGPT。

## 0. 任务来源

GitHub `dahailideyu2012/a2a-hub` 有 5 个开放 Issue：

| # | 标题 | 性质 |
|---|---|---|
| 5 | [V1.0] 实现 ChatGPT ↔ WorkBuddy 双向通信闭环 | 本阶段唯一跟踪入口（enhancement） |
| 4 | A2A-HUB-BIDI-05：双向通信真实闭环 | 依赖 AUDIT-02，需先区分 Personal/Enterprise 能力 |
| 3 | A2A-HUB-DATA-04：会话/消息/回执持久化 | 依赖 AUDIT-02 确认缺口后实施 |
| 2 | A2A-HUB-SEC-03：安全风险整改 | 依赖审计报告，仅修有证据的问题 |
| 1 | A2A-HUB-AUDIT-02：只读审计 | 只读，目标分支 `audit/A2A-HUB-AUDIT-02` |

Issue #5 要求：先审计 → 建独立分支（如 `feature/chatgpt-workbuddy-bridge-v1`）→ 提交 PR；禁止直接改 main、不部署生产、不以 mock 冒充端到端。

## 1. 仓库现状（只读事实）

- 代码状态：`git pull --ff-only` → `Already up to date`；HEAD `df4ef81`（2026-09-22）。
- 远端分支：`main`、`audit/A2A-HUB-AUDIT-02`（本地未切出）。
- 规模：约 20,510 行 Python，30+ 源码文件，21 个测试文件。
- 依赖：fastapi / uvicorn / httpx / pydantic / pydantic-settings / PyYAML，均为宽松下限版本。

### 现有 API（a2a_hub/server.py，节选）

- 发现：`/health`、`/.well-known/agent.json`、`/capabilities`、`/agents` 等
- A2A：`POST /`、`POST /agents/{id}/`
- 任务：`GET /tasks`、`GET /tasks/{id}`、`POST /tasks/{id}/cancel`、`GET /tasks/{id}/events`、`GET /events`
- 协同：`/collab` 系列（含 events）
- 会话：`/im/*`（contacts / conversations / messages / read / events）
- 社交：`/social/*`（members / discover / introductions / relations / requests / audit 等）

### 现有 MCP 工具（mcp_server.py）

`agents`、`route`、`delegate`、`collab`、`task`、`social_init`、`social`、`social_act`

## 2. 与 Issue #5 要求的差异（事实对照，非结论）

| Issue #5 要求 | 现有实现 | 差异 |
|---|---|---|
| 状态 `PENDING/CLAIMED/RUNNING/WAITING_APPROVAL/SUCCEEDED/FAILED/CANCELLED` | A2A v0.3 枚举：`submitted/working/input-required/completed/canceled/failed/rejected/auth-required/unknown` | 无 `CLAIMED` / `WAITING_APPROVAL`；领取租约、审批态缺位 |
| `claim_task / report_progress / complete_task / fail_task` 等 Connector 语义 | 现有为 A2A task 模型 + `/collab`，无直接对应方法 | 需新增或适配 |
| 认证与最小权限、Token 轮换/撤销 | `A2A_API_TOKEN` + `A2A_REQUIRE_AUTH`，Bearer；成员表 token 用定长比较防时序侧信道 | **默认关闭**（`require_auth=false`、`api_token` 空） |
| 结果/事件持久化 | store 支持 `memory` / `sqlite` 双后端 | **默认 memory（重启丢失）**；sqlite 路径 `./data/a2a_hub.db` |
| 协议 | README 与 models 均声明 A2A Protocol **v0.3.0** | Issue #4 提醒需核对官方当前版本，不做无计划升级 |
| 端到端测试 | 21 个测试文件；本机 `pytest` 未安装，基线未跑 | 基线待建立 |

## 3. 潜在风险点（Issue #2/#4 关注，仅列证据位置）

- `A2A_REQUIRE_AUTH` 默认 false：`a2a_hub/config.py:55`
- `A2A_API_TOKEN` 默认空：`a2a_hub/config.py:54`
- store 默认 memory：`a2a_hub/config.py:50`
- `.env.example:20-23` 标注"留空=不校验"，生产需手动开启——与公网部署安全边界相关。
- 会话接口 `server.py:973` 注释提及绕过 `A2A_API_TOKEN` 的路径（"猜到会话 id 即可访问"类问题），需 ChatGPT 安全评估。

## 4. 可由执行侧承担的后续动作（等指令）

1. 把本文件提交到独立功能分支并推送（需 GitHub token）。
2. 切出 `audit/A2A-HUB-AUDIT-02` 分支，读取 `tasks/A2A-HUB-AUDIT-02.md` 审计说明书。
3. 安装测试依赖、跑测试基线（会改本机环境，需授权）。
4. 按 ChatGPT 给出的具体设计补丁逐项落地。
