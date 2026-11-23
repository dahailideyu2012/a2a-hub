# T21 — WorkBuddy Desktop 真实连接证据与离线校验

## 目标与边界

T21 的第一阶段只验证真实 WorkBuddy Desktop 是否能加载 Connector、启动本机 stdio MCP Server，并完成 Hub handshake、presence 和 heartbeat。**不领取任务、不执行任务、不调用 claim_task、不访问生产**。

T20 的 doctor/CI PASS 只证明 Connector 包和静态配置规则通过，不证明 Desktop 已安装、已连接或在线。Hub 的 presence online 也只是认证心跳 TTL 内的服务端遥测，不单独证明桌面 UI 正在运行。

## 当前开发交付

- `scripts/workbuddy_desktop_evidence.py`：离线校验操作者提供的 JSON 证据清单。
- `examples/workbuddy-desktop-evidence.example.json`：字段模板，不是真实测试结果。
- `tests/test_workbuddy_desktop_evidence.py`：结构、安全边界与失败场景测试。

## 连接验收顺序

1. 在 WorkBuddy Desktop 中导入 Connector，并记录实际客户端版本。
2. 确认本机 MCP Server 已由 Desktop 启动；只记录 UI 状态/本地观察证据，不复制环境变量或命令行中的 Token。
3. 从 Desktop 发起 Connector status，确认 Hub handshake 被接受。
4. 确认 presence 查询返回 online，并保存不含凭据的回执标识。
5. 从 Desktop 发起一次 heartbeat，确认 Hub 接受并返回 receipt。
6. 结束本轮连接检查。此阶段不得领取任务。

## 证据清单格式

以 example JSON 为模板，填写实际版本、connector id、Hub 根地址、环境、会话时间及五个事件。事件顺序固定：

- `connector_imported`
- `mcp_server_started`
- `handshake_accepted`
- `presence_online`
- `heartbeat_accepted`

每个事件必须提供带时区的 ISO-8601 时间、来源（`desktop_ui` / `hub_receipt` / `operator_observation`）和不含秘密的 evidence_ref。不要把截图内容、Token、Cookie、Authorization header、lease token、环境变量值或完整日志粘贴进 JSON。截图应由操作者在安全渠道单独保管，分享前遮蔽敏感信息。

## 离线校验

在仓库根目录运行：

```bash
python scripts/workbuddy_desktop_evidence.py examples/workbuddy-desktop-evidence.example.json
pytest -q tests/test_workbuddy_desktop_evidence.py
```

示例文件仅用于验证格式，不能作为真实连接证据。生产 Hub 必须使用 HTTPS；只有本机 loopback 测试可显式使用 `--allow-loopback-http`。

## 如何解释结果

- `PASS`：只代表 JSON 字段、事件顺序、时间、Hub URL 和操作者声明结构通过。
- `FAIL`：证据清单结构不合格或包含疑似凭据字段。
- 本工具离线运行，不连接 Hub、不启动 WorkBuddy、不验证截图真伪、不验证回执签名、不提供密码学真实性保证。
- 证据中的 `operator_attestation` 是操作者声明，不是数字签名。
- WorkBuddy Desktop 真实连接是否成立，必须由独立验收者核对客户端现场证据与 Hub 侧对应回执。不得仅凭模板、doctor、CI 或模拟测试宣称真实 Desktop 已连接。

## 下一阶段

T21 后续阶段在用户明确授权后，使用非生产/隔离 Hub 做一次受控连接复核。只有在连接证据通过后，才单独讨论是否授权一次无敏感内容的测试任务；本阶段没有任务领取或执行授权。


## T22 — 单次连接检查工具（不领取任务）

WorkBuddy Desktop 导入 Connector 后，可显式调用 MCP 工具 `workbuddy_connection_check` 完成一次受控检查：

1. authenticated handshake；
2. 读取一次 presence；
3. 发送一次 `idle` heartbeat（`current_task_id=null`）；
4. 再读取一次 presence，并返回脱敏的回执摘要。

该工具不调用 `claim`、不领取任务、不执行任务、不启动后台轮询。只有 Hub 明确接受 heartbeat 后才返回成功；失败时返回 MCP error。返回值包含 `task_claimed=false` 与 `task_executed=false`，但仍只是服务端请求回执，不是 WorkBuddy Desktop UI 的密码学证明。真实联调必须由操作者在非生产环境中运行，并由独立验收者核对客户端 UI 与 Hub 回执。


## T23 — 连接检查回执追踪字段

T23 为 `workbuddy_connection_check` 增加一次检查的 `check_id`、本地 UTC 时间戳和明确区分的 heartbeat ID / Hub receipt ID。若 Hub 响应没有真实 `receipt_id`，字段保持 null，不再用本地 heartbeat ID 冒充 Hub 回执编号。

- 时间戳由本机 MCP 进程记录，用于排序和诊断，不是 Hub 签名时间。
- `heartbeat_id` 是客户端生成的请求关联 ID；`heartbeat_receipt_id` 仅在 Hub 实际返回时有值。
- 仍然只执行 handshake、presence GET、单次 idle heartbeat、presence GET。
- 不调用 claim/start/renew/progress/result，不领取或执行任务。
- 这些追踪字段有助于操作者将 Desktop 现场观察与 Hub 日志进行关联，但本身不构成数字签名或真实 Desktop UI 证明。


## T24 — 最终 Presence 身份确认

T24 收紧 workbuddy_connection_check 的成功条件：Hub 接受单次 idle heartbeat 后，第二次 presence 查询还必须明确返回 online=true，并且 connector_id 必须与当前 Connector 配置一致。否则工具返回错误，不再把“heartbeat accepted”单独包装成完整连接检查成功。

该检查仍然只是 Hub 服务端遥测与身份字段匹配，不证明 WorkBuddy Desktop UI 的真实性，也不领取或执行任务。操作者仍需在非生产环境完成 Desktop 现场验证。


## T25 — 连接证据事件时间线校验

T25 强化离线证据清单的时间一致性校验：
- 五个事件的 observed_at 必须按清单顺序单调递增或相等；
- 每个事件时间必须落在 session.started_at 与 session.completed_at 之间；
- 相同时间戳允许出现，因为记录精度或客户端时钟可能导致多个步骤落在同一毫秒。

该校验只检查操作者提交的时间字段是否自洽，不验证客户端时钟准确性、事件真实性、截图真伪或 Hub 回执签名。PASS 仍仅代表结构与时间线规则通过。



## T26 — 连接检查失败路径追踪

T26 为连接检查的关键失败门增加安全追踪元数据。心跳未被接受、最终 presence 未明确 online、最终 connector identity 不匹配时，MCP 错误会附带本地生成的 check_id、started_at、失败阶段以及已经观察到的阶段时间戳。

- 追踪字段只包含本地生成的关联 ID、阶段名称和本地 UTC 时间戳。
- 不把 Hub 响应体、receipt ID、请求头、Connector Token、lease token 或任意异常详情复制进错误消息。
- 失败追踪用于将本地诊断与操作者记录关联；它不是 Hub 签名回执，也不证明 Desktop UI 身份。
- 成功路径的握手、两次 presence、单次 idle heartbeat 顺序不变；不领取或执行任务，不启动轮询。
