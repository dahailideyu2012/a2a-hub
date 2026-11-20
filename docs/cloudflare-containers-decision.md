# Cloudflare Containers 部署可行性与持久化决策

> 状态：架构评估（未批准部署）
> 评估日期：2026-11-20
> 适用分支：`feature/chatgpt-workbuddy-bridge-v1`
> 目标：为 ChatGPT ↔ A2A Hub ↔ WorkBuddy 云端通信建立安全、可恢复的 HTTPS 入口。

## 1. 结论

**当前版本不能直接部署到 Cloudflare Containers 作为生产服务。**

FastAPI + Dockerfile 在 Cloudflare Containers 的计算层面具备技术可行性，但当前 Hub 的任务状态、ChatGPT ingress 幂等账本和 WorkBuddy Pull lease/task 状态依赖文件型 SQLite。Cloudflare 官方明确说明 Container 实例磁盘默认是临时的；容器休眠后再次启动会从镜像获得一块全新的磁盘。现有 `/app/data` 中的 SQLite 文件因此不能被视为可靠持久化存储。

在持久化问题解决并通过故障恢复测试之前，不得执行 `wrangler deploy`，也不得将 Cloudflare Container 当作生产任务账本。

## 2. 当前项目的持久化事实

当前 Dockerfile 设置：

- `A2A_DB_PATH=/app/data/a2a_hub.db`
- `A2A_HOST=0.0.0.0`
- `A2A_PORT=8080`

当前 Compose 将宿主机 `./data` 挂载到容器 `/app/data`，这在普通 Docker 主机上提供了持久化目录，但不能自动转化为 Cloudflare Container 的持久磁盘。

配置层还定义独立的：

- `A2A_CHATGPT_INGRESS_DB_PATH`（默认 `./data/chatgpt_ingress.db`）
- Hub task store（由 `A2A_STORE` / `A2A_DB_PATH` 决定）
- WorkBuddy Pull connector 的 task/lease 状态（由 Hub task store 管理）

生产部署必须盘点并覆盖所有写入路径，不能只持久化主任务数据库而遗漏 ingress 幂等账本。

## 3. Cloudflare Containers 的适配情况

### 适合的部分

- Cloudflare Containers 通过 Worker + Durable Object 管理容器。
- 官方文档提供 Dockerfile 镜像部署路径。
- 容器可在内部监听 8080，由 Worker/Durable Object 转发 HTTP 请求。
- 可使用 Workers Builds 在 Cloudflare 构建环境中构建/部署镜像；本地 Docker 并非所有构建路径都必须具备。

### 当前阻塞

- Container 本地磁盘默认 ephemeral；休眠后恢复时使用镜像中的全新磁盘。
- 现有 SQLite 数据库直接放在 `/app/data`，不能保证跨休眠、迁移、重启、扩缩容或部署更新保存。
- Durable Object 自身的 SQLite 存储是持久的，但它不是容器文件系统，也不会自动接管容器内 Python SQLite 文件。
- 不能把 Cloudflare Durable Object SQLite、D1 或 R2 的存在误认为现有 Python SQLite 文件已自动持久化。

### 官方资料

- FastAPI on Python Workers（仅说明 ASGI/FastAPI 支持，不表示现有项目的依赖和存储可直接运行）：https://developers.cloudflare.com/workers/languages/python/packages/fastapi/
- Cloudflare Containers 入门与 Dockerfile 部署：https://developers.cloudflare.com/containers/get-started/
- Container 磁盘持久性 FAQ：https://developers.cloudflare.com/containers/faq/
- Container 生命周期与休眠行为：https://developers.cloudflare.com/containers/concepts/architecture/
- Durable Object SQLite 存储：https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/

## 4. 可选路线与取舍

| 路线 | 优点 | 主要工作/风险 | 当前状态 |
|---|---|---|---|
| A. Cloudflare Containers + 持久化存储适配 | 保留现有 FastAPI 应用与 Dockerfile，HTTPS 入口可由 Worker 提供 | 必须重构 Hub task store、ChatGPT ingress ledger 等存储接口；验证事务、并发、租约 fencing、幂等和恢复 | 候选，尚未实施 |
| B. 现有 VPS + 持久化磁盘 + Cloudflare DNS/代理 | 最接近当前 Docker Compose/SQLite 架构，存储改动较少 | 需要确认可用服务器、网络与 TLS 方案；服务器信息和权限尚未确认 | 候选，尚未授权 |
| C. Container 文件系统直接存 SQLite | 初期改动最少 | 容器休眠/重启会丢数据，不能接受 | **拒绝用于生产** |
| D. SQLite 文件直接放 R2/FUSE | 可探索对象存储挂载 | SQLite 对文件锁、随机写和事务语义有要求；FUSE 不应未经专项验证就承载生产账本 | 不推荐，未验证 |

## 5. 推荐推进顺序

1. **存储盘点与契约冻结**：ChatGPT梳理所有数据库、连接生命周期、事务和跨进程共享要求。
2. **存储抽象设计**：定义可测试的 TaskStore / IngressLedger 契约，明确同步/异步边界、事务语义、唯一键和 lease fencing。
3. **选择持久化实现**：优先评估 Cloudflare D1/SQLite-backed Durable Object 是否能满足多请求并发与事务需求；如改造成本或兼容性不合适，转为 VPS 持久化磁盘路线。
4. **ChatGPT完成代码开发**：实现适配器、迁移脚本、配置校验、测试和恢复演练脚本。
5. **WorkBuddy本机验收**：只运行 ChatGPT 提供的固定 SHA 测试，不改代码、不安装依赖。
6. **单独安全审查与批准**：只有用户明确批准后，才进行 Wrangler/Docker 安装、Cloudflare 资源创建、HTTPS接入或部署。

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

- Hub task store 与 ChatGPT ingress 幂等账本在容器重启/休眠后仍存在。
- WorkBuddy lease 的原子 claim、续租、过期 fencing 和取消 fencing 正确。
- 两个并发请求不能重复领取同一任务或覆盖不同任务的结果。
- 部署更新、容器迁移、意外终止后任务状态可恢复。
- SQLite/D1 迁移可重复执行，备份与恢复经过实测。
- ChatGPT ingress 与 WorkBuddy connector 使用独立凭据和独立 allowlist。
- 外部只暴露 HTTPS Worker endpoint；容器内部端口不直接公网暴露。
- 认证失败、速率限制、请求体限制、日志脱敏均有自动化测试。

**最终决策：先解决持久化架构，再做云端部署。当前仅完成架构评估，不代表 Cloudflare 已接入或部署已获批准。**
