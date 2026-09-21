# A2A 协议映射说明

本文件记录「异构 agent 的原始行为」到「A2A 协议对象」的具体映射决策，
以及那些不写下来就会被后人踩坑的取舍。

---

## 1. 为什么选 A2A，而不是自定义 HTTP 接口

自定义接口的问题不是"能不能跑"，而是**每接一个 agent 就要写一套客户端**。
5 个 agent 是 5 套，10 个 agent 是 10 套，而且是 N×N 的组合。

A2A 的价值在于把"发现 + 调用 + 长任务 + 流式"这四件事标准化：

| 需求 | 自定义接口 | A2A |
| --- | --- | --- |
| 怎么知道对方能干什么 | 看文档 / 问人 | 拉 `/.well-known/agent.json` |
| 怎么发起 | 各写各的 | `message/send` |
| 长任务怎么办 | 各写各的轮询 | `tasks/get` + `tasks/resubscribe` |
| 流式怎么拿 | 各写各的 WS/SSE | `message/stream`（SSE + 规范化事件） |
| 中途取消 | 各写各的 | `tasks/cancel` |

## 2. 对象映射表

### 2.1 CLI 型 agent（Claude Code / Codex / WorkBuddy / Gemini）

| A2A 概念 | 映射 |
| --- | --- |
| `message.parts[0].text` | 作为独立 argv 元素传给 CLI（**不经过 shell**） |
| stdout 一行 | 交给解析器 → 零或多个文本增量 |
| 文本增量 | `TaskArtifactUpdateEvent`（`artifact.name = "output"`，`append=true`） |
| 进程退出码 0 | `TaskStatus.state = completed` |
| 进程退出码非 0 | `TaskStatus.state = failed` |
| 超时 / 被取消 | 杀掉子进程树 → `canceled` |
| 运行元数据（命令、解析器、字符数） | 独立 artifact `run-meta` |

**关键点：prompt 是 argv 元素，不是拼接进 shell 的字符串。**
`--prompt "rm -rf /; echo x"` 不会被执行，只会被当成一个普通参数。

### 2.2 HTTP 型 agent（OpenAI 兼容 / 千问 / DeepSeek / Kimi / GLM）

| A2A 概念 | 映射 |
| --- | --- |
| `task.history` | `messages[]`（`role: agent` → `assistant`） |
| 当前 `message` | 最后一条 `user` 消息 |
| `contextId` | 由调用方维护；同一 context 复用历史 |
| SSE `choices[0].delta.content` | `TaskArtifactUpdateEvent` 增量 |
| `[DONE]` | 流结束 → `completed` |
| 网关不支持流式 | 自动降级为一次性调用 |
| HTTP 4xx/5xx | `failed`，错误正文前 600 字符进 status message |

### 2.3 扣子 Coze

| A2A 概念 | 映射 |
| --- | --- |
| `task.contextId` | Coze `conversation_id`（适配器内部缓存并复用，维持多轮） |
| `conversation.message.delta` | artifact 增量 |
| `conversation.message.completed`（`type=answer`） | 产出（仅在前面无增量时使用） |
| `type=follow_up` | 丢弃（追问建议不是答案） |
| `conversation.chat.failed` | `failed` |
| `code != 0` | `failed`，带 Coze 的 `msg` |

### 2.4 远程 A2A 服务（级联）

| 本地 | 远程 |
| --- | --- |
| `message/stream`（优先） | 对端 `message/stream`，失败降级 `message/send` |
| 对端 `artifact-update` | 直接转发为本地产出 |
| 对端 `status-update` | 映射为本地状态（未知状态 → `working`） |
| 对端返回整包 `task` | 拆成产出 + 终态 |
| 对端 Agent Card | 合并进本地卡片（`metadata.remoteCard`），参与路由 |

## 3. 那些必须记住的取舍

### 3.1 为什么 `rank()` 不再排除 echo 适配器

早期版本在候选排序里硬编码排除了 `type == "echo"`，理由是"回显 agent 没意义"。
这是个错误的设计：它把**产品策略写进了协议层**，还让测试无法用 echo 验证路由。

正确做法是用 `priority` 表达偏好（真实配置里 echo 是 `-10`），
策略与机制分离。

### 3.2 中文分词为什么用 n-gram 而不是最大匹配

最初的 tokenizer 是 `[\u4e00-\u9fff]{2,}`，对「帮我评审这段代码」
会切出「帮我评审这段代码」这一个超长 token，导致它匹配不上任何
技能标签（"评审"、"代码"）。

改为重叠 bigram/trigram 后，"评审"、"代码"、"安全"这些真正有区分度的
token 才能命中。零依赖，够用。

需要更高精度时，`AgentRegistry.score()` 是唯一的替换点，接口不用动。

### 3.3 为什么绝不在 `finally` 里 `yield`

异步生成器在被 `aclose()`（客户端断连、`wait_for` 超时）时会在当前
`yield` 处抛 `GeneratorExit`。如果 `finally` 里还有 `yield`，CPython 会抛
`RuntimeError: async generator ignored GeneratorExit`。

所有 SSE 端点都改成"正常路径走到末尾 yield，异常路径直接返回"的结构。

### 3.4 为什么订阅要在发快照之前

协同运行的 `collab-snapshot` 如果先于 `bus.subscribe()` 发出，
在"取快照"和"挂订阅"之间产生的事件会被永久丢失。
快速返回的 agent（比如 echo）几乎必然撞上这个窗口。

顺序必须是：subscribe → yield snapshot → 循环消费。

### 3.5 为什么 `bus.publish` 里要 `set(...)` 而不是 `list(...)`

`list | set` 在 Python 里直接抛 `TypeError`。这个 bug 会在**第一个任务
下发时**炸掉整条链路，但静态检查看不出来。写在这里提醒后来者。

### 3.6 冲突的 artifact 名字 = 流式增量

`Task.add_artifact()` 看到同名 artifact 时追加 part 而不是新建，
这样 `ctx.artifact(chunk, name="output", append=True)` 能在 A2A 对象层面
自然表达"流式输出"。读取方拼 `artifacts[0].parts[*].text` 即可得到全文。

### 3.7 多模态 Part 的宽松解析

`parse_part()` 接受裸字符串、`{"text": ...}`、完整 Part 对象，甚至
认不出来的 dict（降级为 `DataPart`）。

理由是：各厂商 CLI 输出格式五花八门，让使用者在 YAML 里写 `kind` 字段
是没必要的摩擦。宽松解析 + 不丢信息 > 严格校验。

### 3.8 Agent Card 的 `url` 必须跟随实际监听地址

`A2A_PUBLIC_URL` 的默认值是 `http://localhost:8080`。如果 `serve --port 9000`
时直接把默认值写进卡片，**对端会按照卡片里的 8080 回连，必然失败**——
而恰恰是「按卡片去发现对方」的场景最先踩这个坑。

优先级固定为：`--public-url` > `A2A_PUBLIC_URL` 环境变量 > 按实际
`host:port` 推导（通配地址 `0.0.0.0` / `::` 显示为 `localhost`）。
逻辑抽成 `cli.resolve_public_url()`，有单测覆盖五种组合。

### 3.9 Windows 下必须强制 stdout 为 UTF-8

CLI 输出里有中文和 `●◐✗├└` 这类制表/符号字符。Windows 上 `stdout` 被重定向到
文件时走**本地代码页（GBK）**，这些字符要么变成乱码，要么直接抛
`UnicodeEncodeError` 让命令崩掉。

所以在 `cli.py` 模块加载时就对 `sys.stdout/stderr` 调
`reconfigure(encoding="utf-8")`（用 `try/except` 包住，兼容没有该方法的
被包装流）。注意时机很重要：它必须在**模块导入期**完成，
放进 `main()` 里对已经被包装过的流可能已经晚了。

### 3.10 会话层为什么不算 A2A 标准，却必须存在

A2A 协议里没有「会话」这个实体：`Task` 是执行单位，跑到终态就结束了。
`contextId` 是协议里唯一能表达「这几件事属于同一上下文」的字段，
但它只是一根线——协议没规定线头该怎么拿。

于是有两种做法：

1. 每条消息都新建一个 `Task`，靠 `contextId` 串起来——协议上完全合规；
2. 在 A2A 之上再定义一层 `Conversation`，把 `contextId`、成员列表、
   消息流、已读状态都挂在它身上。

本项目选 2。理由是：**已读回执、未读红点、群成员、@ 提及这些概念
A2A 一个都没有**，硬塞进 `Task.metadata` 只会变成一团没人敢碰的字段堆。
把「IM 语义」和「任务语义」分开，两边才能各自演进。

几个实现上的决定：

- **消息与执行解耦**：`send()` 落库后立刻返回，agent 在后台跑完再把产出
  作为**新消息**追加回会话。如果照 `message/send` 那样阻塞等待，
  群聊里三个 agent 就得串行等三轮，完全没有「聊天」的感觉。
- **回执按 `(消息 × agent)` 粒度**：不用 `message.state` 这种单一状态字段，
  因为群里一条消息会同时被多人收到，各自的已读/回复进度天然不同。
- **上下文用 `contextId` 而不是只拼历史**：会话内所有 Task 共用同一个
  `contextId`（适配器可据此维持服务端上下文，Coze 就是这么做的），
  同时在 prompt 里附上最近 N 条消息作背景。两者互补——前者是协议级的
  记忆锚点，后者是任何适配器都能用的兜底。
- **自动挑人必须先用 `unavailable` 过滤**：`healthy or candidates` 这种写法
  有个坑——群里一个健康成员都没有时，它会退回全量候选，
  把消息丢给一个已经确定跑不起来的 agent。
- **失败要变成一条系统消息**：消息发出去后静默消失是协作场景里最糟的体验，
  宁可多刷一条「❌ 某某未能回复：原因」。

## 4. 尚未实现的部分（诚实的边界）

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| `pushNotificationConfig` | 未实现 | 返回 `-32004`；`capabilities.pushNotifications = false` |
| Agent Card 签名验证 | 未实现 | 跨组织级联需自行加签（建议 JWT + DID） |
| DID / 区块链审计 | 未实现 | 见 BlockA2A 相关研究，属独立课题 |
| 语义路由 | 用关键词替代 | `score()` 可替换为向量检索 |
| 多模态**输入** | 部分 | `Part` 模型支持 file/data，但各适配器目前只转发文本 |
| 会话持久化 | 仅内存 | `SocialHub` 的会话与消息存在进程内存，重启即失；长期保存需接与 `TaskStore` 同款的后端 |
| 消息撤回 / 编辑 | 未实现 | 已支持 `replyTo` 引用回复，但无撤回、无编辑 |
| `@` 中文名歧义 | 受限 | 名称前缀匹配仅在**唯一命中**时生效；`@回显` 同时匹配「回显 A」和「回显 B」时会被忽略 |

## 5. 参考

- A2A Protocol 官方规范：https://a2a-protocol.org/
- A2A 项目仓库：https://github.com/a2aproject/A2A
- MCP（互补协议）：https://modelcontextprotocol.io/
