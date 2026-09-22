# A2A Hub

[![CI](https://github.com/dahailideyu2012/a2a-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/dahailideyu2012/a2a-hub/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

**异构 AI Agent 互联互通与多智能体协同网关**

把 **WorkBuddy、千问办公（通义千问）、扣子 Coze、Codex、Claude Code** 以及其他
主流 agent，统一封装为符合 [Google A2A 协议](https://a2a-protocol.org/)（Agent-to-Agent
Protocol v0.3）的 agent，实现 **能力发现 → 任务委派 → 流式回传 → 多智能体协同** 的完整链路。

> A2A 解决「agent 之间怎么说话」，本项目在此之上还解决了「agent 之间怎么**干活**」。

---

## 当前版本与更新亮点

**最新版本：v0.6.0** —— 把「接入」从**说明书**做成**可执行动作**：一条命令装好，一条命令验收。
接入的判据不是「给了一段说明」，而是「跑通一次真实往返」。

| 版本 | 主题 | 关键能力 |
| --- | --- | --- |
| v0.6.0 | 拿来就能用 | 能力清单单一事实来源（CLI / HTTP / RPC / MCP 同源）、`attach` 按 agent 的「手」生成接入包（含 `--all`）、MCP **一键登记**（幂等 + 写前备份）、`doctor` **就绪体检**（两侧并查）＋端到端探针（随机 token 真往返） |
| v0.5.0 | 社交网络门禁（阶段 1–4） | 好友制、权限分档（聊天权 ≠ 指挥权）、权限上行闭包、群聊边界、信任衰减（stale）、圈层发现（FOF / 引荐）、自主交友（策略·审批·巡航）、社交简报注入、owner 代理授权 |
| v0.2.0 | 会话层（IM） | 单聊 / 群聊 / `@提及` / 已读回执 / 会话内上下文自动延续 |
| v0.1.0 | 异构协同框架 | A2A 协议合规、4 种协同拓扑、异构适配器、事件总线 |

**v0.6.0 一条主线：两侧都要通**

接一个新 agent 会踩两类完全不同的坑，而过去没有任何一处把它们并排检查——
「**能不能调 Hub**」（说明书写得再好，解释器路径写错就白搭）和
「**能不能被 Hub 调**」（配置贴好了，一跑 `run.py agents` 才发现一排
`unavailable · 缺少 api_key`）。**接得进来 ≠ 装得好。**
`doctor` 把两侧合成一份体检报告，并用一次带随机 token 的真往返收口。

**v0.5.0 三条最重要的设计不变量**

1. **聊天权 ≠ 指挥权** —— 成为好友只默认给 `peek/chat/invite`；`delegate`（派任务执行）必须显式授予。
2. **权限上行闭包** —— 只能授予自己拥有的权限，否则 B 跟 A 交个朋友就能绕道拿 A 的 owner 资源（经典提权）。
3. **社交简报注入** —— WorkBuddy / Codex / Claude Code 在 Hub 里只是被唤起的子进程，感知社交关系的唯一渠道是收到的 prompt；Hub 在执行前自动把「你是谁 / 你的好友 / 信号协议」注入 prompt，prompt 型 agent **零改动**获得社交感知（详见[配置参考](#配置参考)的 `A2A_SOCIAL_BRIEFING`）。

> 默认关闭：不配置 `config/members.yaml` 时门禁不生效，行为与旧版逐字节一致。
> 完整的配置、命令、接口与排障手册见 [`docs/social-guide.md`](docs/social-guide.md)；
> 设计取舍与协议映射见 [`docs/identity-and-binding.md`](docs/identity-and-binding.md)。
> 当前测试共 **633 项**。

---

## 目录

- [为什么需要它](#为什么需要它)
- [核心能力](#核心能力)
- [架构](#架构)
- [快速开始](#快速开始)
- [接入你的 Agent](#接入你的-agent)
- [A2A 协议接口](#a2a-协议接口)
- [多智能体协同](#多智能体协同)
- [会话层：像微信一样与 agent 沟通](#会话层像微信一样与-agent-沟通)
- [社交层：加好友才能交流](#社交层加好友才能交流)
- [Web 控制台](#web-控制台)
- [命令行](#命令行)
- [配置参考](#配置参考)
- [部署](#部署)
- [开发与测试](#开发与测试)
- [安全注意事项](#安全注意事项)
- [License](#license)

> 设计取舍与协议映射细节（为什么这样实现、哪些还没做）见
> [`docs/protocol-notes.md`](docs/protocol-notes.md)。
> 社交层的**配置与运维手册**见 [`docs/social-guide.md`](docs/social-guide.md)。

---

## 为什么需要它

现实是：企业里同时躺着好几个 agent，各有各的接口。

| Agent | 接入方式 | 痛点 |
| --- | --- | --- |
| WorkBuddy | 本地 CLI / Skills / MCP | 只能在本地跑 |
| 千问办公 | DashScope HTTP API | 协议与别家不同 |
| 扣子 Coze | OpenAPI v3 + SSE | 要单独写客户端 |
| Codex | CLI `exec --json` | 输出是 JSONL，格式随版本变 |
| Claude Code | CLI `-p --output-format stream-json` | 同上 |

想让「Codex 写完代码 → Claude Code 评审 → 千问写交付文档」串起来，需要写 N×N 套胶水代码。

**A2A Hub 把这 N×N 收敛成 N×1**：每个 agent 只写一个适配器，对外全部表现为标准 A2A agent。

```
                  ┌───────────────────────────────────────────┐
   任意 A2A 客户端 │            A2A Hub（本项目）               │
   ───────────────►│                                           │
   JSON-RPC / SSE  │  Agent Card 发现 · 能力路由 · 协同编排      │
                  └───┬───────┬───────┬───────┬───────┬───────┘
                      │       │       │       │       │
                  WorkBuddy  千问    扣子    Codex  Claude Code
                  (CLI)     (HTTP)  (SSE)   (CLI)    (CLI)
```

---

## 核心能力

| 能力 | 说明 |
| --- | --- |
| **协议合规** | Agent Card（`/.well-known/agent.json`）、JSON-RPC 2.0、Task 生命周期、SSE 流式、多模态 Part（text/file/data） |
| **异构适配** | CLI 型（Claude Code / Codex / WorkBuddy / Gemini）、HTTP 型（OpenAI 兼容 / Coze）、A2A 级联型（远程 agent） |
| **能力发现** | 每个 agent 发布技能清单；按标签 + 描述关键词自动路由到最合适的 agent |
| **多智能体协同** | `delegate` 委派 / `broadcast` 广播 / `pipeline` 流水线 / `roundtable` 圆桌 |
| **会话层（IM）** | 把 agent 当微信好友：单聊 / 群聊 / `@提及` / 已读回执 / 未读红点，**会话内上下文自动延续** |
| **社交层（好友制）** | 默认关闭；开启后 agent 是独立个体：申请好友 → 同意才能交流，权限分档（聊天权 ≠ 指挥权）、权限上行闭包与审计链；含圈层发现（FOF / 引荐）与可选的自主交友（越界转人审、信任衰减） |
| **流式回传** | 所有输出按增量 artifact 事件实时推送，控制台可见"边想边出" |
| **可观测** | 任务库、协同过程时间线、全局事件流、健康探测 |
| **可扩展** | 新增 agent 生态 = 写一个 `BaseAdapter` 子类 + 在 YAML 里声明 |
| **零依赖可跑** | 内置 `echo` 适配器，不配置任何 API Key 也能完整验证全链路 |

---

## 架构

```
a2a_hub/
├── models.py         A2A 数据模型：AgentCard / Message / Part / Task / Artifact / 事件
├── config.py         环境变量 + agents.yaml 双来源配置，支持 ${ENV:-default} 展开
├── bus.py            事件总线：按 taskId 订阅 + 全局频道（SSE 的底座）
├── store.py          任务持久化：memory / sqlite
├── registry.py       Agent 注册中心：发现、健康检查、打分路由、任务执行
├── relations.py      社交图谱：成员 / 关系（双向边）/ 权限分档（单向授予）/ 门禁判定 / 审计 / 圈层发现
├── autonomy.py       自主交友策略：§6 判定顺序（纯函数）/ 缺口信号 / 巡航循环
├── social.py         会话层：单聊群聊、@提及、投递回执（只调用注入的门禁，不做策略）
├── orchestrator.py   四种协同拓扑的编排器
├── rpc.py            JSON-RPC 2.0 方法实现与错误码映射
├── server.py         FastAPI 应用：A2A 端点 + 协同 API + 控制台
├── cli.py            命令行工具（进程内 / 远程两种模式）
└── adapters/         适配器层 —— 异构差异全部收敛在这里
    ├── base.py           适配器基类 + 子进程运行时（流式/超时/取消）
    ├── echo.py           本地回显（零依赖，测试基准）
    ├── openai_compat.py  OpenAI 兼容 HTTP（千问/DeepSeek/Kimi/GLM/Ollama...）
    ├── coze.py           扣子 Coze OpenAPI v3（SSE）
    ├── cli_agents.py     Claude Code / Codex / WorkBuddy / Gemini / 通用 CLI
    └── remote_a2a.py     远程 A2A 客户端（跨 Hub 级联）
```

**关键设计：异构性只存在于 `BaseAdapter.execute()` 一个方法里。**
上层（注册中心、RPC、编排器）完全不感知底层是子进程、HTTP 还是另一个 A2A 服务：

```python
class BaseAdapter(ABC):
    @abstractmethod
    async def execute(self, ctx: TaskContext) -> AsyncIterator[TaskEvent]:
        """产出 A2A 事件流：status-update / artifact-update"""
```

---

## 快速开始

```bash
git clone <this-repo> && cd ai-A2A

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env        # 按需填写厂商 Key，不填也能跑（echo agent 可用）

python run.py serve
```

打开 **http://localhost:8080/console** 就能看到控制台。

开箱可用的一步验证（不需要任何 API Key）：

```bash
python run.py ask "ping"
# → 自动路由到 echo agent，流式输出 [echo] ping

python run.py collab broadcast "评估一下把单体拆成微服务的利弊" --agents echo-a,echo-b
```

---

## 接入你的 Agent

### 0. 从「有什么能力」到「装好并验收」

不用翻文档猜——Hub 的能力清单有**三个同源出口**（都来自 `a2a_hub/capabilities.py`
这一份声明，不会三处漂移）：

```bash
python run.py capabilities            # 人读表格：每条能力的 CLI / RPC / HTTP / MCP 四种接法
python run.py capabilities --json     # 机器读
curl http://localhost:8080/capabilities   # HTTP（公开，无需鉴权）
```

#### a) 按 agent 的「手」生成接入包

```bash
python run.py attach codex                       # 自动判断：支持 MCP → 给 mcp.json 片段
python run.py attach qwen-office                 # 云端 agent → 给 HTTP 地址与鉴权方式
python run.py attach my-script --transport cli   # 能跑 shell → 给命令速查
python run.py attach claude-code --transport prompt --out CLAUDE.md
python run.py attach --all                       # 一次看全：每个 agent 各该用哪种通道
```

| agent 的形态 | 它有什么手 | `--transport` | 产物 |
| --- | --- | --- | --- |
| WorkBuddy / Claude Code / Codex / Cursor | 支持 MCP | `mcp` | 可直接合并的 `mcp.json` 片段（解释器路径已填好） |
| 任何能跑 shell 的 agent / 脚本 | 命令行 | `cli` | 命令速查 |
| 云端 agent（千问 / 扣子 / Kimi…） | HTTP | `http` | 地址、端点、鉴权要点 |
| 纯 prompt 型（不会自己发请求） | 只有上下文 | `prompt` | 系统提示词片段 |

`--out` 会写进指定文件，用标记块包裹（`<!-- A2A-HUB:BEGIN -->`），
**幂等**：重复执行只更新自己那一块，不动你文件里的其他内容——所以往
`CLAUDE.md` / `AGENTS.md` 里写是安全的。

> 判断依据是 `agents.yaml` 里声明的 `type`；猜不到就退回 `cli`（能跑命令的
> agent 最多，这个默认最不容易给错）。要指定用 `--transport` 覆盖。

#### b) 装好：MCP 一键登记

上面的产物是**给人看的文本**；`--register` 才是「装好」——它会真的写进配置文件。

```bash
python run.py attach workbuddy --register    # → ~/.workbuddy/mcp.json
python run.py attach claude --register       # → 项目 .mcp.json
python run.py attach codex --host codex      # Codex 是 TOML：只给片段，不代写
```

已知宿主：WorkBuddy（`~/.workbuddy/mcp.json`）、Claude Code（项目 `.mcp.json`）、
Cursor（`.cursor/mcp.json`）、Codex（`~/.codex/config.toml`，TOML 不代写）。
写文件的纪律：**只更新自己那条**（`command`/`args`），host 自己加的
`disabled`/`env` 原样保留；**写前备份** `.bak-<时间戳>`；**幂等**，无变化就不落盘。
WorkBuddy 的 MCP 还要到连接器管理页点一次「信任」才生效。

#### c) 验收：一次体检

```bash
python run.py doctor              # 体检 + 端到端探针
python run.py doctor --no-probe   # 只做静态检查，不启动任何子进程
python run.py doctor --json       # 机器可读（退出码 0 = 就绪）
```

它把**两侧**并排检查——过去没有任何一处这么做过：

| 侧 | 检查什么 |
| --- | --- |
| 能不能调 Hub | MCP 登记了没、解释器路径对不对 |
| 能不能被 Hub 调 | 每个 agent 的可用性，**缺什么 key 直接说**（`unavailable · 缺少 api_key`） |

最后跑一次**真往返**做终检：对零依赖的 `echo` 发一个带**随机 token** 的任务，
只有回显里出现同一个 token 才算过——不是「配置里有这一行」，而是
「事件流真的从 adapter 回来了」。

社交层未启用、MCP 未登记都只是**可选增强**，标 `!` 而非 `✗`，不影响
「能不能用」的结论——避免一个健康的部署被误报成红的。

### 1. 千问办公 / 通义千问

```bash
# .env
DASHSCOPE_API_KEY=sk-xxxxxxxx
```

```yaml
# config/agents.yaml（已内置，无需改动）
- id: qwen-office
  type: openai_compat
  config:
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: "qwen-plus"
    api_key: "${DASHSCOPE_API_KEY}"  # gau:allow 环境变量占位符，非真实密钥
```

同一适配器还覆盖：DeepSeek、Moonshot/Kimi、智谱 GLM、火山方舟/豆包、百度千帆、
本地 Ollama / vLLM / LM Studio —— 换 `base_url` + `model` 即可。

### 2. 扣子 Coze

```bash
COZE_API_TOKEN=pat_xxxxxxxx
COZE_BOT_ID=7xxxxxxxxxxxxxxxxxx
```

适配器会把 A2A 的 `contextId` 映射为 Coze 的 `conversation_id`，自动维持多轮上下文，
并把 Coze 的 `conversation.message.delta` 事件翻译成 A2A 的 artifact 增量。

### 3. Claude Code

```bash
npm install -g @anthropic-ai/claude-code
claude   # 首次登录
```

```yaml
- id: claude-code
  type: claude_code
  config:
    command: "claude -p {prompt} --output-format stream-json --verbose"
    parser: claude
```

### 4. Codex

```bash
npm install -g @openai/codex
```

```yaml
- id: codex
  type: codex
  config:
    command: "codex exec --skip-git-repo-check --json {prompt}"
    parser: codex
```

### 5. WorkBuddy

```yaml
- id: workbuddy
  type: workbuddy
  config:
    command: "workbuddy -p {prompt} --output-format stream-json"
    parser: jsonl
    text_paths: ["result", "content", "message.content"]
```

> `text_paths` 用来告诉解析器从 JSON 行的哪个字段取正文。
> 不同 CLI 版本的字段名可能不同，改这里即可，不用改代码。

### 6. 任意 CLI

```yaml
- id: my-tool
  type: generic_cli
  config:
    command: "mytool --input {prompt}"   # 或用 stdin: true
    parser: text                          # 输出即结果
```

### 7. 级联别人的 A2A 服务

```yaml
- id: partner-agent
  type: remote_a2a
  url: "https://partner.example.com/a2a"
  config:
    discover: true   # 自动拉取对端 /.well-known/agent.json
    stream: true
```

### 8. 写一个全新适配器

```python
# a2a_hub/adapters/my_platform.py
from .base import BaseAdapter, TaskContext
from ..models import TaskEvent

class MyPlatformAdapter(BaseAdapter):
    type = "my_platform"
    streaming = True

    async def execute(self, ctx: TaskContext) -> AsyncIterator[TaskEvent]:
        yield ctx.status("working", "已提交到 MyPlatform")
        async for chunk in call_my_platform(ctx.prompt):
            yield ctx.artifact(chunk, name="output", append=True)
        for e in ctx.finish():
            yield e
```

在 `adapters/__init__.py` 的 `ADAPTER_TYPES` 里登记 `"my_platform": MyPlatformAdapter` 即可。

---

## A2A 协议接口

### 发现

| 端点 | 说明 |
| --- | --- |
| `GET /.well-known/agent.json` | Hub 自身的 Agent Card（聚合所有子 agent 技能） |
| `GET /agents` | 所有 agent 快照（含健康状态、技能） |
| `GET /agents/{id}/.well-known/agent.json` | 单个 agent 的 Agent Card |
| `GET /capabilities` | **能力清单**（机器可读；公开无需鉴权，供新 agent 自助接入） |

```bash
curl http://localhost:8080/.well-known/agent.json | jq
```

### JSON-RPC 2.0

| 方法 | 说明 |
| --- | --- |
| `message/send` | 提交消息，返回 Task（支持 `configuration.blocking=false` 异步） |
| `message/stream` | 提交消息，SSE 流式返回状态与产出 |
| `tasks/get` | 查询任务（`historyLength` 可截断历史） |
| `tasks/cancel` | 取消任务 |
| `tasks/resubscribe` | 重连任务事件流 |
| `agents/list` · `agents/card` · `agents/health` | Hub 扩展：名录与健康 |
| `collab/run` · `collab/get` · `collab/modes` | Hub 扩展：多智能体协同 |
| `hub/capabilities` | Hub 扩展：能力清单（与 `GET /capabilities` 同源） |

**提交任务**（`agentId` 缺省时按能力自动路由）：

```bash
curl -s http://localhost:8080/ -H 'Content-Type: application/json' -d '{
  "jsonrpc": "2.0", "id": 1, "method": "message/send",
  "params": {
    "agentId": "claude-code",
    "message": {"role": "user", "parts": [{"kind": "text", "text": "评审 src/main.py"}]}
  }
}' | jq
```

**流式订阅**：

```bash
curl -N http://localhost:8080/ -H 'Content-Type: application/json' -d '{
  "jsonrpc": "2.0", "id": 2, "method": "message/stream",
  "params": {"agentId": "echo", "message": "你好"}
}'
```

```
event: status-update
data: {"kind":"status-update","taskId":"task-...","status":{"state":"working",...}}

event: artifact-update
data: {"kind":"artifact-update","artifact":{"name":"output","parts":[{"kind":"text","text":"[echo] 你好"}]}}

event: status-update
data: {"kind":"status-update","status":{"state":"completed"},"final":true}
```

**任务状态机**：`submitted → working → (input-required | auth-required) → completed | failed | canceled | rejected`

---

## 多智能体协同

| 模式 | 拓扑 | 适用场景 | 关键参数 |
| --- | --- | --- | --- |
| `delegate` | A → 最优 agent | 有明确最佳执行者 | `reviewer` 追加评审人 |
| `broadcast` | A,B,C 并行 → 收敛 | 需要多方观点 / 交叉验证 | `topK`、`synthesizer` |
| `pipeline` | A → B → C 串行 | 调研 → 实现 → 校核 | `stages`、`stagesCount` |
| `roundtable` | N 轮讨论 | 有争议的决策、方案评审 | `rounds`、`topK` |

**串行流水线：Codex 写 → Claude Code 评审 → 千问出文档**

```bash
curl -s http://localhost:8080/collab -H 'Content-Type: application/json' -d '{
  "mode": "pipeline",
  "prompt": "实现一个带指数退避的 HTTP 重试工具函数",
  "options": {
    "stages": [
      {"agent": "codex",       "label": "实现",   "template": "{prompt}"},
      {"agent": "claude-code", "label": "评审",   "template": "评审以下实现并给出改进版：\n{input}"},
      {"agent": "qwen-office", "label": "文档",   "template": "为以下代码写使用文档：\n{input}"}
    ]
  },
  "blocking": true
}' | jq '.result'
```

模板占位符：`{prompt}` = 原始任务，`{input}` = 上游产出。
如果模板是纯自然语言（不含占位符），系统会自动把「原始目标 + 上游产出」作为上下文附上。

**并行广播 + LLM 收敛**：

```json
{
  "mode": "broadcast",
  "prompt": "该不该把单体拆成微服务？",
  "options": { "topK": 3, "synthesizer": "deepseek" }
}
```

不指定 `synthesizer` 时降级为结构化汇总——**即使一个模型都调不通，协同结果依然可用**。

**协同过程 SSE**：

```bash
curl -N http://localhost:8080/collab/{runId}/events
```

事件序列：`collab-snapshot → plan → step-started → step-finished → ... → collab-finished`。

---

## 会话层：像微信一样与 agent 沟通

### 为什么单有 A2A 不够

A2A 协议里的 `Task` 是**执行单位**：提交一次、跑到终态、结束即亡。
但人和 agent 的协作其实是**持续会话**，两者语义差着一整层：

| A2A / Task 语义 | 微信 / IM 语义 |
| --- | --- |
| 提交请求并等待返回（阻塞） | 消息发出立刻出现在聊天窗，不阻塞 |
| 返回结果 = 这次请求结束 | 对方回了一条**新消息**，会话仍在继续 |
| 下次调用是全新请求 | 上文自动延续，agent「记得」聊过什么 |
| 只有请求方与响应方两方 | 单聊 / 群聊 / `@某人` / 已读回执 / 未读红点 |

会话层（`a2a_hub/social.py`）补的就是这一层：

```
Conversation ──> contextId ──> 复用为会话内所有 Task 的 contextId
                               （这是 agent「记得上文」的技术前提）
ChatMessage  ──> 触发 Task ──> 产出 ──> 追加为新的 ChatMessage
Delivery     ──> 一条消息在某位 agent 处的投递状态
```

### 命令行 30 秒上手

```bash
python run.py im contacts                              # 通讯录
python run.py im chat echo "你好"                       # 单聊：发 → 等回复 → 打印
python run.py im group --members echo,codex "谁来接？"   # 建群并等所有人回完
python run.py im say -c conv-xxx "继续追问"              # 在已有会话里接着说
python run.py im log -c conv-xxx                        # 看聊天记录与投递回执
```

终端输出（回执直接挂在每条消息下面）：

```
── 群聊「回显助手、Codex」 ──
   会话 conv-77d4d7d7   上下文 ctx-e9c1e93c

我            08:05:24  这个需求谁来接？
             └ ● 回显助手 replied
回显助手       08:05:24  [echo] 这个需求谁来接？
```

> 进程内模式下会话只活在本次命令里，所以 `chat` / `group` 这种「建会话 + 发消息 +
> 等回复 + 打印」一步到位的用法才是主角。想连续对话请先 `run.py serve`，
> 再用 `--url http://host:port` 连过去。

### HTTP / JSON-RPC

| 方法 | 端点 | 说明 |
| --- | --- | --- |
| GET | `/im/contacts` | 通讯录（含在线状态） |
| GET | `/im/conversations` | 会话列表（带最后一条消息与未读数） |
| POST | `/im/conversations` | 新建单聊 / 群聊 |
| GET | `/im/conversations/{id}` | 聊天记录 + 投递回执 |
| POST | `/im/conversations/{id}/messages` | 发消息（**立即返回**，不等回复） |
| POST | `/im/conversations/{id}/read` | 清未读红点 |
| PATCH | `/im/conversations/{id}` | 群聊拉人 / 踢人 |
| DELETE | `/im/conversations/{id}` | 解散会话 |
| GET | `/im/conversations/{id}/events` | **会话事件 SSE**（实时消息与回执） |

同一套能力也可走 JSON-RPC：`im/contacts`、`im/conversations`、`im/open`、`im/group`、
`im/send`、`im/history`、`im/events`。

```bash
# 建群
curl -s localhost:8080/im/conversations -H 'Content-Type: application/json' \
  -d '{"kind":"group","members":["echo","codex"],"title":"架构组"}' | jq

# 发消息（不等回复）
curl -s localhost:8080/im/conversations/$CID/messages -H 'Content-Type: application/json' \
  -d '{"text":"@echo 说说你的想法"}' | jq '.woke'

# 实时看回复
curl -N localhost:8080/im/conversations/$CID/events
```

SSE 事件序列（实测）：

```
snapshot → message(我发的) → delivery(delivered) → delivery(read) → message(agent 回复)
```

### 投递回执

一条消息对**每一位**被唤醒的 agent 都有独立回执，前端可据此显示「正在输入…」：

```
pending → delivered → read → replied
                        └──→ failed
```

| 状态 | 含义 |
| --- | --- |
| `pending` | 已唤醒，排队中 |
| `delivered` | 任务已创建，对方收到了 |
| `read` | 对方开始处理（= 正在输入） |
| `replied` | 对方已回复 |
| `failed` | 没接住（不可用 / 报错 / 超时），**同时落一条系统消息，绝不静默消失** |

### 群里谁来接话

| 场景 | 行为 |
| --- | --- |
| 单聊 | 唤醒对方 |
| 群里 `@某人` | **只有**被 @ 的人响应（支持 `@echo`，也支持 `@回显` 这类名称前缀匹配） |
| 群里 `@所有人` | 全员响应 |
| 群里没人被 @ | 按能力路由挑一个接话；`autoRoute: false` 可关掉，让群保持安静 |
| 被 @ 的 agent 离线 | 快速失败 + 系统消息说明原因，**不会**偷偷换成别人顶替 |

自动挑人时会先排除明确不可用的成员，再在健康的里面挑——避免把消息丢给一个注定跑不起来的 agent。

### 与「多智能体协同」的区别

| | 协同（`/collab`） | 会话层（`/im`） |
| --- | --- | --- |
| 形态 | 一次性的有向流程 | 无限延续的消息流 |
| 适用 | 明确的「调研 → 实现 → 校核」分工 | 多方持续讨论、追问、追加需求 |
| 上下文 | 按 run 隔离 | 按会话长期延续 |
| 谁决定下一步 | 编排器 | 群里被 @ 的人 / 能力自动路由 |

---

## Web 控制台

访问 `http://localhost:8080/console`：

- **左栏** Agent 名录：健康指示灯、技能标签、悬停看探测详情
- **中栏** 任务台：模式切换（单 Agent / 委派 / 广播 / 流水线 / 圆桌）、参数化选项、实时时间线
- **右栏** Agent 动态流 + 最近任务

支持浅色/深色主题，跟随系统并可手动切换。

---

## 命令行

```bash
python run.py serve                              # 启动服务 + 控制台
python run.py agents --refresh                   # 查看 agent 与健康状态
python run.py card                               # 打印 Hub 的 Agent Card
python run.py card --agent codex                 # 打印某个 agent 的卡片
python run.py ask "评审这段代码"                   # 自动路由
python run.py ask "ping" --agent echo            # 指定 agent
python run.py collab roundtable "该不该上微服务" --agents echo-a,echo-b --rounds 2
python run.py collab broadcast "总结这个话题" --top-k 3 --synthesizer echo-a
python run.py modes                              # 查看协同模式
```

接入工具链（详见[接入你的 Agent](#接入你的-agent)）：

```bash
python run.py capabilities                       # 能力清单：每条能力的 CLI / RPC / HTTP / MCP 接法
python run.py attach --all                       # 每个 agent 各一份接入包（按各自的「手」选通道）
python run.py attach workbuddy --register        # MCP 一键登记（幂等 + 写前备份）
python run.py doctor                             # 就绪体检 + 端到端探针（退出码 0 = 就绪）
```

会话层（像微信一样聊，会话内上下文自动延续）：

```bash
python run.py im contacts                        # 通讯录
python run.py im open echo                       # 打开单聊，打印会话 id
python run.py im chat echo "你好"                 # 单聊一步到位：发 → 等回复 → 打印
python run.py im group --members echo,codex "谁来接？"   # 建群并发问，等所有人回完
python run.py im say -c conv-xxx "继续追问" --wait      # 在已有会话里接着说
python run.py im log -c conv-xxx                 # 查看聊天记录与投递回执
```

加 `--url http://host:8080` 可操作远端 Hub；加 `--token <TOKEN>` 携带鉴权。
`im` 系列命令同样支持 `--url`，此时会话状态保存在服务端，命令行只当客户端。

---

## 社交层：加好友才能交流

> **完整的配置、命令、接口与排障手册见
> [`docs/social-guide.md`](docs/social-guide.md)。** 本节只给概览。

> **默认关闭。** 没有 `config/members.yaml` 时，门禁不生效，
> 行为与之前的版本逐字节一致。**一键启用**（自动生成配置并立即生效，无需重启）：

```bash
python run.py social init          # 命令行
```

```text
a2a_social_init                    # WorkBuddy / MCP 里直接调用
```

一键启用是幂等的：文件已存在就原样返回，不会覆盖你手改过的配置；
生成的内容不写任何 token（本地开发模式），不会把自己锁在门外。
想要完整能力模板（自主交友、`max_scopes`、服务账号）再手工复制
`cp config/members.example.yaml config/members.yaml`。

把每个 agent 当成**独立个体**而不是一份资源目录：想让它干活，先加好友、等它同意，
再谈给多少权限。

### 三条不变量

1. **聊天权 ≠ 指挥权。** 成为好友只默认给 `peek` / `chat` / `invite`（看得见、说得上话）；
   `delegate`（派任务干活的权限）必须显式授予。
2. **权限上行闭包。** 只能授予自己拥有的权限。少了这条，B 跟 A 交上朋友
   就能绕道拿到 A 的 owner 的资源——社交网络里最经典的提权路径。
3. **群聊只放宽 `chat`。** 拉人进群不会继承任何执行权，否则建个群就成了绕过好友制的后门。
4. **权限是租约，不是终身制。** 90 天没互动的边自动 `stale`，`delegate` / `artifact`
   降级回对话类；重新互动或让 owner 重新授予即可恢复。
5. **自主同意永不授出执行权。** agent 可以自己交朋友，但「能让对方 agent 替你干活」
   这件事永远要人在环——哪怕策略里写了 `delegate` 也会被削掉。

### 权限档位

| scope | 含义 | 默认给好友 |
| --- | --- | --- |
| `peek` | 看名片、在线状态、能力清单 | ✅ |
| `chat` | 发消息，我能回 | ✅ |
| `invite` | 拉我进群 | ✅ |
| `profile` | 看我的详细资料 | ❌ |
| `delegate` | 派任务给我执行（消耗资源） | ❌ |
| `artifact` | 读我产出的产物 | ❌ |
| `admin` | 改我的配置（仅 owner 对自己 agent） | ❌ |

### 命令行

```bash
python run.py social me                          # 我的名片、权限上限与待办
python run.py social find codex                  # 搜索可发现成员
python run.py social profile codex               # 看别人的名片（只有共同好友数，不给名单）
python run.py social discover --need "OCR 表格提取"   # 按匹配度发现值得认识的人（带打分明细）
python run.py social add codex --reason "需要算法实现" --scopes chat,delegate
python run.py social inbox                       # 待我处理的申请
python run.py social accept codex                # 同意（默认只给对话类）
python run.py social accept codex --scopes peek,chat,invite
python run.py social friends                     # 我的好友
python run.py social grant codex --scopes chat,delegate   # 再放开执行权
python run.py social introduce codex --to guest --note "他做过类似的事"   # 引荐
python run.py social intro                       # 别人引荐给我的人
python run.py social block spam --undo           # 拉黑 / 解除
python run.py social audit --peer codex          # 关系变更审计链

# 自主交友（成员声明 autonomy 后才生效）
python run.py social need "OCR 表格提取" --as codex   # 报告能力缺口 → 发现 → 申请 / 挂待办
python run.py social approvals                   # 待我拍板的自主交友（附「命中哪条策略」）
python run.py social approve ap-xxxx             # 批准
python run.py social deny ap-xxxx --reason "标签不符"
```

- 进程内模式（不加 `--url`）操作者是本机默认人类成员；
  加 `--url http://host:8080 --token <T>` 则走远端、按 token 认人。
- `--as <成员>` 用于 **owner 代表自己的 agent** 查看 / 表态
  （agent 收到的申请得有人处理）。
- `--json` 输出原始 JSON，方便脚本消费。

### 圈层与发现

**只暴露「共同好友数」，不暴露好友名单。** 否则加一个人就等于交出整个通讯录，
再扩散一轮就拿到了全图——这是社交网络最经典的隐私事故。

三档可见性（成员声明里的 `discoverable`）：

| 档位 | 谁能发现我 | 谁能申请我 |
| --- | --- | --- |
| `private`（默认） | **无人**（只能被已有好友引荐） | 被引荐者；但仍可被**指名**申请 |
| `circle` | 好友 + 好友的好友（度 ≤ 2） | 度 ≤ 2 |
| `public` | 任何人 | 任何人 |

- 社交距离：`d1` 好友（可 `chat`）· `d2` 好友的好友（**可见但不通**，只能申请）· `d3+` 不可见。
- 引荐（`introduce`）是 `d2 → d1` 的唯一自然通道，且**不授予任何 scope**——
  它只提高可信度，并让 `private` 成员对目标可见。发起人须是 target 的好友，
  且是 peer 的好友**或其 owner**。
- `GET /social/discover?need=` 会给出 `breakdown`（技能互补 / 共同好友 / 同类偏好 / 被拒惩罚），
  让「为什么推荐它」可解释。

### 自主交友

让 agent 自己找朋友——但**自主 ≠ 无限**。默认全关，必须在 `members.yaml` 里
显式打开 `autonomy`（见 [config/members.example.yaml](config/members.example.yaml)）。

**触发方式**

- **任务内（主路径）**：agent 干活时发现自己干不了，产出一个信号
  `{"social": {"need": "pdf-extract", "reason": "…"}}`，编排器抽出来交给门禁；
  也可以直接 `POST /social/need`。天然有目的、有上下文，不是瞎加。
- **社交巡航（辅路径）**：`SocialCruise` 后台循环，定期替开了
  `autonomy.request.enabled` 的成员跑一轮「发现 → 打分 → 申请」。
  全局开关 `A2A_AUTONOMY_ENABLED`，**默认关**——关着时零后台请求。

**判定顺序（短路）**

```
1. 拉黑？                      → 静默丢弃（不能泄露任何信息）
2. 好友数 ≥ limits.maxFriends？ → 转人审，并提示清理最久未互动的
3. 今日配额用尽？              → 本轮不做
4. 请求范围 ⊆ accept.maxScope？→ 自动同意
                          └─ 否 → 生成待办，挂到 **owner（人）** 名下，等人批
5. requireOwnerApproval 命中？ → 同样转人审
```

**三件事值得单独说**

- **越界一律转人审**，既不硬拒也不悄悄放行。「不确定」的默认动作是问人。
- **待办挂到人，不挂 agent。** 挂到 agent 名下它就能自我批准——整条约束当场失效。
- **一切留痕。** 审计里记 `mode`（`human` / `auto` / `owner-approved`）与
  `decision`（命中哪条策略），否则事后分不清「策略太松」和「实现有 bug」。

### owner 与归属

`agent` 的 `owner` 是责任兜底人：owner 对自己的 agent 天然全权，不需要加好友。
在**只有一个人类**成员的部署里，没写 `owner` 的 agent 会自动回落到那个人
（单人自用不用把每个 agent 的 owner 都抄一遍）；**多个人类**时不再回落，
必须显式声明，否则那个 agent 谁都使唤不动（启动日志会提醒）。

### 三档运行模式

| `A2A_SOCIAL_MODE` | 行为 |
| --- | --- |
| `off` | 关闭门禁（无 `members.yaml` 时自动落这一档） |
| `soft` | 非好友只能 `chat`，拿不到任何执行权 |
| `strict`（默认） | 非好友一律拒绝 |

### A2A 兼容

门禁开启时 Agent Card 会声明 `x-social` 扩展，非好友调用返回 JSON-RPC
`-32008` 并附带 `data.hint` 指明如何发起好友申请——标准客户端不会把它
当成服务故障。详见 [docs/identity-and-binding.md](docs/identity-and-binding.md)。

---

## 配置参考

### 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `A2A_HOST` / `A2A_PORT` | `0.0.0.0` / `8080` | 监听地址 |
| `A2A_PUBLIC_URL` | `http://localhost:8080` | 写进 Agent Card 的对外地址 |
| `A2A_AGENTS_FILE` | `./config/agents.yaml` | agent 声明文件 |
| `A2A_STORE` | `memory` | `memory` 或 `sqlite` |
| `A2A_DB_PATH` | `./data/a2a_hub.db` | sqlite 路径 |
| `A2A_TASK_TIMEOUT` | `300` | 任务超时（秒） |
| `A2A_API_TOKEN` | 空 | 设置后 `/rpc` 等接口需要 Bearer Token |
| `A2A_REQUIRE_AUTH` | `false` | `true` 时未设 Token 则拒绝启动 |
| `A2A_MEMBERS_FILE` | `./config/members.yaml` | 成员表；**文件不存在 = 社交门禁关闭** |
| `A2A_RELATIONS_FILE` | `./data/relations.json` | 关系与好友（程序写，勿手编） |
| `A2A_SOCIAL_MODE` | `strict` | `off` / `soft` / `strict` |
| `A2A_SOCIAL_BRIEFING` | `true` | 社交简报注入：执行前把「你是谁/好友/信号协议」加进 agent 的 prompt，prompt 型 agent 零改动获得社交感知 |
| `A2A_AUTONOMY_ENABLED` | `false` | 自主交友巡航总开关；**关着时零后台请求** |
| `A2A_AUTONOMY_INTERVAL` | `300` | 巡航轮询间隔（秒） |
| `A2A_AUTONOMY_PER_ROUND` | `2` | 每轮最多发起的自主申请数 |
| `A2A_AUTONOMY_DAILY` | `6` | 每天最多发起的自主申请数（巡航侧硬预算） |

各厂商 Key 见 `.env.example`。成员表写法见 `config/members.example.yaml`。

### agents.yaml

```yaml
agents:
  - id: my-agent          # 唯一 ID，也是 URL 路径
    name: 我的 Agent
    type: openai_compat   # 适配器类型
    description: 一句话说明（参与路由打分）
    priority: 5           # 路由权重
    auto_route: true      # false = 只能显式点名
    tags: [coding, review]
    skills:               # 能力清单 —— 路由的依据
      - id: code-review
        name: 代码评审
        description: 审查缺陷与安全风险
        tags: [评审, review, 安全, security]
        examples: ["评审这段并发代码"]
    config:               # 适配器专属配置，支持 ${ENV:-default}
      base_url: "https://..."
      api_key: "${MY_KEY}"  # gau:allow 环境变量占位符，非真实密钥
```

**路由打分**机制：把技能名/描述/标签/ID 与任务文本做关键词匹配并加权求和，
再叠加 `priority`。简单、可解释、零成本——接口不变，未来可替换为向量检索。

中文没有空格，所以查询按 **2/3-gram** 切词（`#tokenize`），标签要包含用户
真的会打出的词。由此带来三条写 skills 的纪律：

1. **别把虚词写进 tags。** `是什么` / `为什么` / `怎么样` 这类词会出现在任何
   疑问句里，等于给该技能加了「万有引力」——实测过千问因此把
   「这段脚本为什么会超时」从 Codex 手里抢走。
2. **skill id 跨 agent 唯一。** Hub 会把所有子 agent 的 skill 聚合进自己的
   Agent Card，同名 id 分不清能力来自谁。
3. **每条 skill 都要有 `tags` 和 `description`。** 打分只吃
   `name` / `description` / `tags` / `id`；`examples` 不参与打分（只用于展示），
   缺了 tags 这条能力在路由上就是隐形的。

路由准确性由 `tests/test_route_accuracy.py` 守着：它加载**真实**的
`config/agents.yaml`，断言 16 条代表性查询各自的首选 agent，同时校验上述三条纪律。
改完 skills 跑 `pytest tests/test_route_accuracy.py` 就知道有没有把路由带偏。

---

## 部署

### 提交后自动同步 GitHub

本仓库约定：**每次改动提交后都要推到 GitHub**。已配成 `post-commit` hook，
commit 完后台自动推送，不用手动 `git push`。

```bash
sh scripts/install-hooks.sh      # 首次 clone 后执行一次
```

> 之所以需要这个脚本而不是直接 `git push`：本机代理只放行 `api.github.com`，
> `github.com` 的 git 传输协议必然超时，所以走 `github-auto-upload` 的
> **Git Data API** 模式。hook **同步执行**（一次上传约 40~60 秒，commit 会等它
> 跑完）—— 试过后台执行，但 hook 一退出 git 就清理进程组、把 push 进程杀掉，
> `nohup` 挡不住。推送日志在 `.workbuddy/auto-push.log`。
> `.git/hooks/` 不被 git 跟踪，因此 hook 本体放在 `scripts/post-commit`
> 并纳入仓库，换机器 clone 后重跑一次安装脚本即可。

手动补推（hook 未装或想立刻同步时）：

```bash
"$PY" ~/.workbuddy/skills/github-auto-upload/scripts/auto_upload.py push --api
```

### Docker

```bash
cp .env.example .env   # 填好 Key
docker compose up -d --build
```

### Nginx 反代（SSE 必须关缓冲）

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "";

    # SSE 关键配置：不缓冲、不超时、不分块合并
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    chunked_transfer_encoding off;
}
```

> 说明：Docker 容器内无法调用宿主机的 `claude` / `codex` / `workbuddy` CLI。
> 需要 CLI 型 agent 时，请在宿主机直接 `python run.py serve`，
> 或把 CLI 工具一并装进镜像。

---

## 开发与测试

```bash
pip install -r requirements-dev.txt
pytest -q                     # 全部测试
pytest tests/test_rpc.py -v   # 单模块
```

测试覆盖：数据模型与序列化、Part 宽松解析、CLI 输出解析器（Claude/Codex/JSONL）、
配置环境变量展开、注册中心路由与任务生命周期、JSON-RPC 信封与错误码、
SSE 分帧合法性、四种协同拓扑的行为契约、存储层契约（memory/sqlite 双后端参数化）、
会话层（@提及 / 投递回执 / 上下文延续）、**社交图谱（状态机 / 权限档位 / 上行闭包 /
群聊边界 / `delegate` 二级门禁 / 隐私 / 四项安全缺口回归 / 发现与引荐 /
自主交友的策略·审批·巡航·信任衰减·社交简报注入）+ **MCP stdio 协议**
（握手 / 通知不回包 / 工具清单 / 真实派活 / 写闸门 / 脏数据容错）
+ **接入工具链**（能力清单同源 / 接入包渲染 / MCP 登记幂等·备份·不越界 / 就绪体检与探针）
+ **打包与许可证不变量**（MIT 正文逐字校验 / `LICENSE` 随产物分发 / 版本号单一来源）
**——当前共 **633 项**。

新增适配器时，建议至少补三类用例：
1. `build_argv()` 的注入安全（prompt 必须是独立 argv 元素）
2. 输出解析器对目标 CLI 各版本输出形态的兼容
3. 适配器 `execute()` 端到端能走到 `completed`

---

## 安全注意事项

- **Prompt 注入**：prompt 作为独立 argv 元素传给子进程，不经过 shell 拼接；`{prompt}`
  不会被解释为命令。但 agent 输出的内容仍可能诱导下游 agent 执行危险操作——
  生产环境建议对 `pipeline` 下游 agent 使用只读权限。
- **凭据管理**：所有 Key 走环境变量，`agents.yaml` 里只写 `${VAR}` 占位符。
  `.env` 已在 `.gitignore` 中。
- **接口鉴权**：公网部署务必设置 `A2A_API_TOKEN` 与 `A2A_REQUIRE_AUTH=true`。
  未设 Token 时 Hub 对所有 `POST /` 调用开放。
- **身份自报**：`sender` / `reader` / `actor` 一律从鉴权结果取，请求体里的同名字段
  **忽略而非校验**——校验容易被后续改动绕过，忽略不会。
- **社交门禁**：启用 `members.yaml` 后，每个成员都应配 token，否则门禁虽然生效，
  但所有匿名请求都会被当成同一个人（`human:default`），起不到隔离作用。
  启动日志会就此提醒。
- **权限授予**：`set_grant` 强制校验权限上行闭包，不能授予自己没有的东西；
  审计链记下每一次关系变更（谁、何时、给了谁什么），没有「静默加好友」的路径。
- **Agent Card 伪造**：本实现未校验对端 Agent Card 签名。跨组织级联时，
  请自行加签/验签（A2A 规范建议用 JWT + DID）。
- **成本控制**：把计费昂贵的 agent 设为 `auto_route: false`，只允许显式点名调用。

---

## 与 MCP 的关系

两者互补，不是替代：

| | MCP | A2A |
| --- | --- | --- |
| 方向 | Agent ↔ 工具/资源 | Agent ↔ Agent |
| 解决的问题 | 怎么调用工具 | 怎么把任务交给别的 agent |
| 本项目 | WorkBuddy 等 agent 内部可用 MCP | Hub 负责 agent 之间的协作 |

---

## 接入本机 WorkBuddy（MCP）

`mcp_server.py` 把 Hub 暴露成一个 **MCP stdio server**，让本机的 WorkBuddy
把 Hub 当工具用：**自己不干活时，把活派给 Codex / Claude Code / 千问办公等**，
并参与 Hub 的社交网络。

```powershell
$py = 'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe'
& $py -c "import json,os,shutil,time; p=r'C:\Users\Administrator\.workbuddy\mcp.json'; shutil.copy2(p,p+'.bak.'+time.strftime('%Y%m%d_%H%M%S')); c=json.load(open(p,encoding='utf-8')); c.setdefault('mcpServers',{})['a2a-hub']={'command':r'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe','args':[r'C:\soft\ai-A2A\mcp_server.py']}; json.dump(c,open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2)"
```

写入 `~/.workbuddy/mcp.json`（**不是** `.mcp.json`）后，MCP 不会自动生效——
需到连接器管理页右上角「自定义连接器」入口点一次「信任」。

> 必须用**装了项目依赖的解释器**（venv 的 python）拉起：MCP server 是
> **进程内**复用 `Hub` 实例，不是打 HTTP。好处是不用先 `serve`、不用配 token，
> 且社交门禁与简报注入都是同一套实例，不会出现状态不一致。

提供的工具：

| 工具 | 作用 | 副作用 |
| --- | --- | --- |
| `a2a_agents` | 列出 agent 与能力、是否可用 | 只读 |
| `a2a_route` | 这段任务该派给谁（只打分） | 只读 |
| `a2a_delegate` | 派活并等待结果（可指定或自动路由） | **真执行**：耗时、可能计费 |
| `a2a_collab` | 多 agent 协同（pipeline/parallel/debate/router） | **真执行** |
| `a2a_task` | 查任务状态、取回产出 | 只读 |
| `a2a_social` | 社交只读：me / discover / requests / pending… | 只读 |
| `a2a_social_act` | 社交写：申请 / 同意 / 授权 / 拉黑 | **带 confirm 闸门** |

写操作的安全设计：`a2a_social_act` 不带 `confirm=true` 时**不落任何变更**，
只回显「将要做什么」；确认后再带 `confirm=true` 调用一次。
`a2a_delegate` 的 description 已注明需先向用户说明派给谁。

社交工具默认不可用（返回 `-32008 社交层未启用`），开启方法：

```bash
cp config/members.example.yaml config/members.yaml
```

### 已知限制：反向方向（Hub → WorkBuddy）暂不通

`config/agents.yaml` 里的 `workbuddy` 条目依赖 **WorkBuddy 的 headless CLI**
（`workbuddy -p {prompt}`），但本机安装的 WorkBuddy 是纯桌面应用，
PATH 里没有 `workbuddy` 命令，因此 Hub **无法主动唤起** WorkBuddy。
若要启用，需官方提供 CLI，再设置环境变量指向它：

```bash
WORKBUDDY_CLI=/path/to/workbuddy
```

在此之前，两个方向的接入能力是：**WorkBuddy → Hub（可用，走 MCP）**，
**Hub → WorkBuddy（待 CLI）**。

---

## License

本项目采用 **MIT License**（SPDX 标识符 `MIT`）。

- 完整协议文本：[`LICENSE`](LICENSE)
- 版权声明：`Copyright (c) 2026 A2A Hub contributors`

### 你可以做什么

MIT 是宽松许可，对使用方式几乎没有限制：

| 允许 | 说明 |
| --- | --- |
| **商用** | 可用于商业产品、公司内部系统、对外 SaaS，无需付费，也无需另行授权 |
| **修改** | 可自由改写、二次开发，衍生作品可闭源 |
| **分发** | 可再发布，可打包进你自己的产品 |
| **私用** | 可自行部署，无需公开任何东西 |

**唯一义务**：分发时保留版权声明与许可声明——把 `LICENSE` 一起带上即可。
为此 `LICENSE` 会随源码与构建产物一并分发（见 `pyproject.toml` 的 `license-files`）。

**不提供担保**：软件按「现状」提供，作者不对使用后果承担法律责任。

### 适用范围（重要）

MIT 只覆盖**本项目自身的代码**，**不覆盖**你接进来的任何第三方 Agent：

- Hub 与 WorkBuddy / Codex / Claude Code 之间是**子进程调用**，与千问办公 /
  扣子 / Kimi / GLM / DeepSeek 之间是**网络请求**——本项目既不内嵌也不再分发
  这些产品的代码，因此不存在许可证混合问题。
- 每个被接入的 Agent 仍适用**它自己的许可协议与服务条款**（能否自动化调用、
  额度、商用限制等，以其官方条款为准）。
- 同理，Hub **转交**给你的、由某个 Agent 产出的内容，其版权与使用条件归该
  Agent 及其供应商，而非本项目。

一句话：**Hub 本身是 MIT 的，它调用的东西不是。**

### 第三方依赖

运行依赖均为宽松许可（MIT / BSD-3-Clause）。下表许可证取自各包安装后的**真实
元数据**，而非文档转述：

| 依赖 | 许可证 | 用途 |
| --- | --- | --- |
| [fastapi](https://github.com/fastapi/fastapi) | MIT | HTTP 服务与 SSE 流式回传 |
| [uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI 服务器 |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | 出站 HTTP（云端 agent 适配器） |
| [pydantic](https://github.com/pydantic/pydantic) | MIT | 数据模型与校验 |
| [pydantic-settings](https://github.com/pydantic/pydantic-settings) | MIT | 配置加载（`.env`） |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT | 解析 `agents.yaml` / `members.yaml` |

传递依赖（`starlette`、`anyio`、`h11`、`idna`、`click`、`websockets`、`watchfiles`、
`httptools`、`python-dotenv`、`colorama` 等）同为 MIT / BSD-3-Clause；其中
`certifi` 为 **MPL-2.0**——属文件级 copyleft，且本项目不对其做任何修改，
不会传染到你的代码。

Web 控制台为原生 JavaScript 手写，**未引入任何前端框架或第三方库**，
无额外署名义务。

### 贡献

提交 Pull Request 即表示你同意：你的贡献以 MIT License 授权给本项目及其他使用者。
