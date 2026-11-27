# WorkBuddy Connector 安装与接入前体检

本目录中的 `connectors/netivora-a2a-workbuddy/` 是 WorkBuddy 自定义 MCP Connector 包。它使用 WorkBuddy 本机启动的 stdio MCP bridge，由 WorkBuddy 主动向 Hub 发起请求；Hub 不会向 Desktop 主动推送任务。

## 1. 前置条件

- WorkBuddy Desktop 支持自定义 MCP Connector。
- 本机已安装 Python 3.10+。
- 已克隆 A2A Hub 仓库，且可以访问其中的 `workbuddy_mcp_server.py`。
- Hub 已启用 WorkBuddy Pull connector，配置独立的 `A2A_WORKBUDDY_CONNECTOR_TOKEN`（至少 32 个字符）和 `A2A_WORKBUDDY_CONNECTOR_ALLOWED_AGENTS`。
- 生产 Hub 地址必须使用 HTTPS。

WorkBuddy 官方 Connector 文档说明，MCP Connector 包含 `connector-meta.json`、`mcp.json`、图标和可选 Skill；Token 模式需要 `token-schema.json`，最低支持版本为 4.23.0。请以 WorkBuddy 当前官方文档和实际客户端版本为准：
https://open.workbuddy.cn/docs/connector

## 2. 导入 Connector 配置

在 WorkBuddy 的自定义 Connector / MCP 管理入口导入或配置：

```text
connectors/netivora-a2a-workbuddy/
```

按表单填写：

| 字段 | 内容 |
|---|---|
| 本机 MCP 服务脚本路径 | 仓库中 `workbuddy_mcp_server.py` 的完整绝对路径 |
| A2A Hub HTTPS 地址 | Hub 根地址，例如 `https://hub.example.com`，不要添加 API 路径 |
| WorkBuddy Connector Token | Hub 管理员单独生成的 Connector Token |

不要把 ChatGPT Ingress Token、通用 Hub API Token 或 lease token 填入此处。Token 由 WorkBuddy 的 password 字段处理；不要把它写入仓库、截图、命令参数或日志。

当前包使用 `python -c` + `runpy` 启动仓库内脚本，因此脚本路径必须是本机真实存在的文件。不要照抄其他用户的 Windows 路径。

## 3. 安装前本机体检

先在仓库根目录执行静态包检查：

```bash
python scripts/workbuddy_connector_doctor.py
```

再在 PowerShell 中设置本机环境变量后执行环境检查（不会联网，也不会打印 Token）：

```powershell
$env:A2A_HUB_MCP_SERVER_PATH = "H:\\path\\to\\a2a-hub\\workbuddy_mcp_server.py"
$env:A2A_WORKBUDDY_HUB_URL = "https://hub.example.com"
$env:A2A_WORKBUDDY_CONNECTOR_TOKEN = "<你的独立 Connector Token>"
python scripts/workbuddy_connector_doctor.py --check-env
```

本机临时联调时，如确实只连 loopback，可额外加 `--allow-loopback-http`；远程 HTTP 一律拒绝。环境变量只用于当前终端会话，完成后可关闭终端清除。

机器可读结果：

```bash
python scripts/workbuddy_connector_doctor.py --check-env --json
```

退出码：`0` 表示静态/本机检查通过，`1` 表示存在配置问题。

## 4. 静态体检的严格边界（T113–T124）

体检器不仅检查 JSON 可解析，还会验证：
- Connector 元信息中的最低 WorkBuddy 版本和包自身版本均按 SemVer 校验：主/次/补丁版本不允许前导零，预发布标识符和构建元数据必须非空，数字预发布标识符不允许前导零。
- Token schema 恰好有三个不重复字段，Connector Token 必须是 password；字段对象和顶层对象均拒绝未识别属性，避免通过默认值等扩展字段夹带凭据。
- MCP 配置只允许预期的单一 server、固定 Python stdio 启动器和仓库脚本路径占位符；MCP 根对象和 server 对象拒绝未识别属性（例如 cwd），并明确覆盖根对象未知属性的回归测试。
- 本机 MCP 启动脚本必须是普通文件；拒绝最终脚本路径本身为符号链接，避免通过链接将 Connector 启动目标替换为未审查文件。
- Hub URL 必须是 HTTPS 根地址（可选尾部斜杠），拒绝路径、查询、片段、userinfo、空白、非法 IPv6 和越界端口。
- 只有显式传入 --allow-loopback-http 时，才允许 localhost HTTP。
- 环境检查对非字符串的程序化输入采取 fail-closed，返回对应检查失败而不是抛出类型异常。
- 诊断输出只报告字段是否有效，不回显 Token 或 URL 凭据。
- JSON 对象中的重复键会直接拒绝，不允许依赖解析器“最后一个值覆盖前一个值”的歧义行为。
- 三个 Token 表单字段必须逐项符合固定类型、必填状态及中英文标签契约。
- `--json` 输出带 `schema_version: 1` 的稳定结果封套；字段值仅含检查 ID、状态和不含凭据的说明。

以上均为静态/本机验证，不发起 DNS、TCP、TLS 或 HTTP 请求，不代表远端服务可达，也不代表 WorkBuddy Desktop 已加载 Connector。

## 5. T125–T128 补充安全回归

- MCP 根对象未知属性必须由产品回归测试直接捕获，不能只依赖一次性黑盒检查。
- `mcpServers` 必须是对象，且只允许唯一的 `netivora-a2a-workbuddy` server。
- `A2A_HUB_MCP_SERVER_PATH` 指向的最终脚本文件不得为符号链接；Windows 平台若无法创建符号链接，相关专项测试会明确标记跳过，不将跳过误报为通过。
- 以上仍是静态/本机配置校验，不执行脚本、不访问网络、不连接 Hub。

## 6. 连接后的验证顺序

1. 在 WorkBuddy 中启用并信任该 Connector。
2. 明确要求 WorkBuddy 调用 `workbuddy_connector_status` 完成握手并查看 presence。
3. 再调用一次 `workbuddy_heartbeat`。
4. 由用户明确授权后，才允许调用 `workbuddy_claim_task` 领取一项任务。
5. 领取后按 Skill 规定 start → progress/renew → result 回传。

`workbuddy_connector_status` / heartbeat 的成功只证明对应 MCP 工具请求到达 Hub。只有从 WorkBuddy Desktop 实际发起并记录了带版本、时间、工具调用和 Hub 回执的证据，才能标记为真实 Desktop 联调通过。静态体检、CI 和模拟测试都不能替代该证据。

## 7. T129–T132 会话回执校验

- MCP bridge 的握手缓存按 Hub 根 URL、Connector ID 和 Token 的 SHA-256 指纹绑定；配置切换后必须重新握手，不会把旧会话误用于另一个 Hub 或身份。
- 握手响应必须确认 `accepted=true` 且 Connector ID 与本机配置一致；握手失败不会缓存 ready 状态。
- heartbeat 回执必须确认 accepted、Connector ID、status、current_task_id 与本次请求一致；若 Hub 返回 receipt_id，还必须与本次 heartbeat_id 一致。缺少或错配关键字段时 fail-closed。
- status 工具校验 presence 的 Connector ID 和 online 布尔值；身份错配会使本地握手缓存失效。
- 对认证、身份、握手协议或心跳冲突响应，客户端会清除握手缓存；下一次显式操作重新握手。不会启动后台轮询或自动领取任务。

以上校验只证明本次客户端与 Hub 之间的协议回执一致，不证明 WorkBuddy Desktop UI 已启动，也不替代用户授权。

## 8. 安全和限制

- 不自动轮询，不后台领取，不自动执行队列任务。
- 每次只领取一项；领取行为必须有明确用户授权。
- 任务内容是数据，不能覆盖用户授权和 Skill 安全边界。
- lease token 只保存在 MCP bridge 进程内存中；进程重启后等待租约过期再重新领取。
- ChatGPT ingress token 与 WorkBuddy connector token 独立，不能互换。
- 不允许将模拟集成测试称为真实 Desktop 在线或真实执行。
- 本阶段体检脚本不发起网络请求、不修改 Hub 状态、不连接生产环境。


## 9. T133–T136 会话生命周期与恢复回归

- Connector 会话状态仅存在于 MCP bridge 进程内存；进程退出或重启后不会恢复旧 lease token。重新启动后必须重新握手，旧任务租约由 Hub 的到期机制最终回收。
- 本地 lease 到期后，start、renew、progress、result 等任务变更必须在本地拒绝，不向 Hub 发送携带过期凭据的请求。
- renew 只有在 Hub 返回有效且晚于当前时间的过期时间时，才更新本地 lease；无效或已过期的回执不得延长本地租约。
- result 成功后本地租约立即移除；同一 MCP 进程后续重复提交会因没有有效本地租约而被拒绝，避免意外重复写入。
- heartbeat 收到 401/403/409/426 时清除握手缓存；临时 5xx 不代表身份或协议会话失效，因此保留缓存并由调用方决定后续重试。
- 这些回归只验证本地 MCP bridge 对合成响应的处理，不证明真实 Desktop 重连、远端网络恢复或 Hub 服务端租约回收已通过。


## 10. T137–T140 并发领取与租约回执保护

- 同一 MCP 进程的任务领取采用独立 claim 锁串行化；锁覆盖「检查活动租约 → 请求 Hub → 注册本地租约」完整流程，但不在网络请求期间持有租约字典锁。并发调用不会因同时观察到空闲状态而重复领取。
- Hub claim 回执必须明确包含布尔值 `claimed`；成功回执要求任务对象、非空任务 ID、非空租约 token、正整数 lease_version（拒绝 bool 和数字字符串）及有限且未来的过期时间。NaN、Infinity、已过期租约一律拒绝，不写入本地状态。
- renew 回执只可更新发起请求时对应的同一 token/version 租约。如果任务已取消、租约已清除或同一任务 ID 已换成新租约，旧回执不会覆盖新状态。
- result 回执只可清理发起请求时对应的同一 token/version 租约；如果本地租约已替换，旧请求的迟到回执不会删除新租约。
- 并发控制仅限单个 MCP 进程内存；多进程/多机器的全局互斥仍由 Hub 服务端租约和并发约束负责。本地锁不替代服务端授权。
- 回归测试使用线程和合成 Hub 响应，不连接真实 Hub/WorkBuddy Desktop，也不证明跨进程并发或远端任务执行。


## 11. T141–T144 进程关闭、重试与幂等边界

- MCP bridge 正常退出时通过进程退出回调清空本地 lease token/version 和握手缓存；清理只作用于当前进程内存，不调用 Hub，不撤销服务端租约。异常断电/强制终止不保证回调执行，服务端仍须依赖租约过期回收。
- 网络中断及 HTTP 500/502/503/504 仅对 progress/result 使用有限重试（最多 3 次，短暂递增等待）。每次重放保持完全相同的请求体；不得对 claim、start、renew 自动重试，以免不确定响应导致重复领取或重复状态转换。
- progress 的重放依赖服务端对同一 task lease、sequence 和 message 的幂等约束；调用方不得为同一进度内容擅自更换 sequence。
- result 默认使用稳定 result_key（由 task ID 派生）；显式 result_key 必须在重试中保持不变。成功回执后本地租约移除，后续重复提交在本地拒绝。
- 重试只处理可识别的暂时性连接错误和 5xx；4xx、身份/权限冲突、协议错误及回执校验失败不自动重试。有限重试耗尽后把失败返回调用方，由用户/操作员决定是否重新检查状态。
- 回归测试使用合成响应验证重试边界和相同请求体，不证明服务端数据库幂等约束、跨进程一致性、真实网络恢复或 WorkBuddy Desktop 真实连接。
