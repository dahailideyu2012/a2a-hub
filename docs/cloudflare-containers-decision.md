# Cloudflare Containers 部署可行性与持久化决策

> 状态：架构评估（未批准部署）
> 评估日期：2026-11-20
> 适用分支：`feature/chatgpt-workbuddy-bridge-v1`
> 目标：为 ChatGPT ↔ A2A Hub ↔ WorkBuddy 云端通信建立安全、可恢复的 HTTPS 入口。

## 1. 结论

**当前版本不能直接部署到 Cloudflare Containers 作为生产服务。**

FastAPI + Dockerfile 在计算层面具备候选可行性，但当前 Hub 的任务状态、ChatGPT ingress 幂等账本、关系图状态及运行时成员文件都依赖容器本地文件；Cloudflare 官方明确说明 Container 实例磁盘默认是临时的，容器休眠后再次启动会从镜像获得新磁盘。更重要的是，Dockerfile 没有设置 `A2A_STORE=sqlite`，而应用默认值是 `memory`。Cloudflare Containers 不会自动使用项目的 Docker Compose 配置，因此直接运行镜像时，任务存储可能落入内存，WorkBuddy Pull Connector 会因存储类型检查返回 503。

在持久化问题解决并通过故障恢复测试之前，不得执行 `wrangler deploy`，也不得将 Cloudflare Container 当作生产任务账本。

## 2. 当前项目的持久化事实（已由源码交叉核验）

### 2.1 服务端运行时写入点

| 数据类别 | 实现位置 | 配置/默认路径 | Cloudflare Container 休眠/重启风险 | Docker Compose volume |
|---|---|---|---|---|
| Hub tasks、执行状态、WorkBuddy lease/fencing、progress、result | `a2a_hub/store.py` 的 `SqliteTaskStore`；由 `registry.py` 装配 | `A2A_STORE` 默认 `memory`；`A2A_DB_PATH` 默认 `./data/a2a_hub.db`；Dockerfile 设 `/app/data/a2a_hub.db` | memory 模式在进程重启即丢；SQLite 文件模式在容器磁盘重建时丢 | SQLite 模式下 `./data:/app/data` 覆盖 |
| ChatGPT ingress 幂等账本 | `a2a_hub/chatgpt_ingress.py` 的 `IngressLedger` | `A2A_CHATGPT_INGRESS_DB_PATH` 默认 `./data/chatgpt_ingress.db`，按项目 ROOT 解析 | 文件随容器磁盘重建而丢；相同幂等键可能被重新接受 | 默认 ROOT=/app 时落在 `/app/data`，被覆盖；Compose 未显式设置该变量 |
| 关系图、审计、审批、配额 | `a2a_hub/relations.py` 的 `SocialGraph.save()` | `A2A_RELATIONS_FILE` 默认 `./data/relations.json`，按项目 ROOT 解析 | 文件随容器磁盘重建而丢 | 默认落在 `/app/data`，被覆盖 |
| 成员声明文件 | `a2a_hub/social_boot.py` 的 `bootstrap_members()` | `A2A_MEMBERS_FILE` 默认 `./config/members.yaml` | 文件随容器磁盘重建而丢；启动时可能重新生成，但人工修改内容不会自动恢复 | **未覆盖**；Compose 只挂载单个只读 `agents.yaml`，没有挂载整个 `/app/config` |
| Agent 声明配置 | `config.py` / `social_boot.py` 读取 | `A2A_AGENTS_FILE` 默认配置路径；Compose 挂载 `/app/config/agents.yaml:ro` | 只读镜像/挂载配置，不是服务端运行时写入点 | 只读单文件挂载 |

WorkBuddy Pull Connector 不维护独立任务数据库。它通过 `workbuddy_connector.py` 的 `store_for()` 复用 `hub.registry.store`；claim/start/renew/progress/result 与 Hub tasks 共用同一 task store。当前实现要求该 store 是 `SqliteTaskStore`，否则返回 503，不会静默降级。

ChatGPT ingress ledger 则是独立 SQLite 文件和独立表（`chatgpt_ingress_requests`）。它保存幂等键到 task_id 的映射；任务本体仍写入 Hub task store。因此完整请求闭环至少涉及两份数据库状态。

### 2.2 存储配置与路径语义风险

- `A2A_STORE` 在 `config.py` 默认值为 `memory`；Dockerfile 未显式设置它。Compose 在服务环境中显式设置 `sqlite`，但 Cloudflare Container 镜像部署不会自动应用 Compose 配置。
- `A2A_DB_PATH` 由 `build_store(s.store, s.db_path)` 原样传入，SQLite 相对路径按进程当前工作目录解析；ingress 与 relations 路径则通过各自的 ROOT-relative helper 解析。当前 Dockerfile 的 `WORKDIR /app` 使默认路径看似一致，但代码语义并不统一。
- `A2A_CHATGPT_INGRESS_DB_PATH` 虽然默认落在 `/app/data`，Compose 没有显式声明它；其持久化依赖默认 ROOT 与容器工作目录保持一致。
- `members.yaml` 是服务启动期间可能生成的运行时文件，不应与只读的 `agents.yaml` 混为一谈。

### 2.3 非服务端运行时写入

只读审计还发现 CLI/宿主侧写入，例如 `a2a_hub/attach_kit.py` 更新宿主 MCP 配置并生成备份，以及 `a2a_hub/cli.py` 更新 `CLAUDE.md` / `AGENTS.md` 标记块。这些由宿主侧 CLI 显式触发，不属于 Hub HTTP 服务请求链路；云端镜像不应将其误判为服务端状态库。

未发现服务端文件日志写入；当前日志输出到 stdout。

## 3. Cloudflare Containers 的适配情况

### 适合的部分

- Cloudflare Containers 通过 Worker + Durable Object 管理容器。
- 官方文档提供 Dockerfile 镜像部署路径。
- 容器可在内部监听 8080，由 Worker/Durable Object 转发 HTTP 请求。
- 可使用 Workers Builds 在 Cloudflare 构建/部署镜像；本地 Docker 并非所有构建路径都必须具备。

### 当前阻塞

- Container 本地磁盘默认 ephemeral；休眠后恢复时使用镜像中的新磁盘。Durable Object scheduling policy 下的 snapshot 是显式、不可变的快照机制，不应误当作每次 SQLite 写入自动同步的持久磁盘。
- 现有 SQLite 数据库直接放在 `/app/data`，不能保证跨休眠、迁移、重启、扩缩容或部署更新保存。
- Durable Object 自身的 SQLite 存储是持久、事务性的，但它属于 Durable Object 实例的私有存储；不是容器文件系统，也不会自动接管容器内 Python SQLite 文件。若使用它，必须设计明确的 Worker/DO 数据访问边界、分区策略及并发语义。
- D1、Durable Object SQLite 或 R2 的存在，不代表现有 Python SQLite 文件已自动持久化。
- 直接以当前镜像部署时还会遇到 `A2A_STORE=memory` 默认值问题：任务状态不持久，且 WorkBuddy Pull Connector 的 SQLite 类型检查会拒绝 claim。

### 官方资料

- Cloudflare Containers 入门与 Dockerfile 部署：https://developers.cloudflare.com/containers/get-started/
- Container 磁盘持久性 FAQ：https://developers.cloudflare.com/containers/faq/
- Container 生命周期与休眠行为：https://developers.cloudflare.com/containers/concepts/architecture/
- Durable Object Container API：https://developers.cloudflare.com/containers/api/durable-object-container/
- Durable Object SQLite 存储：https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/
- FastAPI on Python Workers（仅说明 ASGI/FastAPI 支持，不表示现有项目的依赖和存储可直接运行）：https://developers.cloudflare.com/workers/languages/python/packages/fastapi/

## 4. 可选路线与取舍

| 路线 | 优点 | 主要工作/风险 | 当前状态 |
|---|---|---|---|
| A. Cloudflare Containers + 持久化存储适配 | 保留现有 FastAPI 应用与 Dockerfile，HTTPS 入口可由 Worker 提供 | 必须重构 Hub task store、ChatGPT ingress ledger、relations 与 members 状态；明确 Worker/DO 与 Python 容器的数据访问协议；验证事务、并发、租约 fencing、幂等和恢复 | 候选，尚未实施 |
| B. 现有 VPS + 持久化磁盘 + Cloudflare DNS/代理 | 最接近当前 Docker Compose/SQLite 架构，存储改动较少 | 需要确认可用服务器、网络与 TLS 方案；服务器信息和权限尚未确认 | 候选，尚未授权 |
| C. Container 文件系统直接存 SQLite | 初期改动最少 | 容器休眠/重启会丢数据；当前镜像默认还会使用 memory store | **拒绝用于生产** |
| D. SQLite 文件直接放 R2/FUSE | 可探索对象存储挂载 | SQLite 对文件锁、随机写和事务语义有要求；FUSE 不应未经专项验证就承载生产账本 | 不推荐，未验证 |

## 5. 下一阶段工作顺序（ChatGPT 开发，WorkBuddy 验收）

1. **存储契约盘点与冻结**：ChatGPT逐项分析现有 `TaskStore`、`IngressLedger`、`SocialGraph` 的调用接口、事务边界、连接生命周期、跨进程并发要求及数据模型。
2. **目标后端可行性比较**：分别评估 D1、SQLite-backed Durable Object、VPS 持久磁盘。不得假设 DO SQLite 是可直接替换 Python `sqlite3` 的数据库；必须证明访问协议、事务范围、并发和容量可行。
3. **确定存储抽象**：在后端选型前，先形成可测试的接口契约和迁移策略，覆盖任务、lease、幂等账本、关系图与成员声明。
4. **ChatGPT 完成代码开发**：实现选定适配器、配置校验、迁移/回滚、并发测试、故障恢复与备份恢复测试；不得由 WorkBuddy编写或修补代码。
5. **WorkBuddy 本机验收**：只运行 ChatGPT 提供的固定 SHA 测试命令，报告原始退出码和结果，不修改代码、不安装依赖。
6. **单独安全审查与批准**：只有用户明确批准后，才进行 Wrangler/Docker 安装、Cloudflare 资源创建、HTTPS 接入或部署。

## 6. 安全与上线门槛

在获得单独批准前：

- 不运行 `wrangler deploy`、`wrangler dev`、`wrangler login` 或任何 Cloudflare 资源写操作。
- 不安装 Docker、Wrangler、uv 或依赖。
- 不创建 Tunnel、不开放公网端口、不修改 DNS、防火墙、域名或代理。
- 不编辑 WorkBuddy 用户级 `mcp.json`，不改变其 Hub URL。
- 不读取或输出任何真实 Token、`.env`、账号标识或生产数据库。
- 不修改 `main`，不部署生产环境。
- ChatGPT负责全部代码、测试、脚本、配置、Skill 和文档；WorkBuddy只负责按固定 SHA 执行和真实 UI 验收。

## 7. 生产验收必须覆盖

- Hub task store、ChatGPT ingress 幂等账本、relations 与 members 状态在容器重启/休眠后仍存在或可确定性恢复。
- WorkBuddy lease 的原子 claim、续租、过期 fencing 和取消 fencing 正确。
- 两个并发请求不能重复领取同一任务或覆盖不同任务的结果。
- 部署更新、容器迁移、意外终止后任务状态可恢复。
- 所有数据库迁移可重复执行，备份与恢复经过实测。
- ChatGPT ingress 与 WorkBuddy connector 使用独立凭据和独立 allowlist。
- 外部只暴露 HTTPS Worker endpoint；容器内部端口不直接公网暴露。
- 认证失败、速率限制、请求体限制、日志脱敏均有自动化测试。

**最终决策：先解决持久化架构，再做云端部署。当前仅完成架构评估，不代表 Cloudflare 已接入或部署已获批准。**
