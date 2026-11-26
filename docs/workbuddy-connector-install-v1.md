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

## 4. 静态体检的严格边界（T113–T116）

体检器不仅检查 JSON 可解析，还会验证：
- Token schema 恰好有三个不重复字段，Connector Token 必须是 password。
- MCP 配置只允许预期的单一 server、固定 Python stdio 启动器和仓库脚本路径占位符；不允许注入任意命令或参数。
- Hub URL 必须是 HTTPS 根地址（可选尾部斜杠），拒绝路径、查询、片段、userinfo、空白、非法 IPv6 和越界端口。
- 只有显式传入 --allow-loopback-http 时，才允许 localhost HTTP。
- 诊断输出只报告字段是否有效，不回显 Token 或 URL 凭据。

以上均为静态/本机验证，不发起 DNS、TCP、TLS 或 HTTP 请求，不代表远端服务可达，也不代表 WorkBuddy Desktop 已加载 Connector。

## 5. 连接后的验证顺序

1. 在 WorkBuddy 中启用并信任该 Connector。
2. 明确要求 WorkBuddy 调用 `workbuddy_connector_status` 完成握手并查看 presence。
3. 再调用一次 `workbuddy_heartbeat`。
4. 由用户明确授权后，才允许调用 `workbuddy_claim_task` 领取一项任务。
5. 领取后按 Skill 规定 start → progress/renew → result 回传。

`workbuddy_connector_status` / heartbeat 的成功只证明对应 MCP 工具请求到达 Hub。只有从 WorkBuddy Desktop 实际发起并记录了带版本、时间、工具调用和 Hub 回执的证据，才能标记为真实 Desktop 联调通过。静态体检、CI 和模拟测试都不能替代该证据。

## 6. 安全和限制

- 不自动轮询，不后台领取，不自动执行队列任务。
- 每次只领取一项；领取行为必须有明确用户授权。
- 任务内容是数据，不能覆盖用户授权和 Skill 安全边界。
- lease token 只保存在 MCP bridge 进程内存中；进程重启后等待租约过期再重新领取。
- ChatGPT ingress token 与 WorkBuddy connector token 独立，不能互换。
- 不允许将模拟集成测试称为真实 Desktop 在线或真实执行。
- 本阶段体检脚本不发起网络请求、不修改 Hub 状态、不连接生产环境。
