# A2A Hub

**异构 AI Agent 互联互通与多智能体协同网关**

把 **WorkBuddy、千问办公（通义千问）、扣子 Coze、Codex、Claude Code** 以及其他
主流 agent，统一封装为符合 [Google A2A 协议](https://a2a-protocol.org/)（Agent-to-Agent
Protocol v0.3）的 agent，实现 **能力发现 → 任务委派 → 流式回传 → 多智能体协同** 的完整链路。

> A2A 解决「agent 之间怎么说话」，本项目在此之上还解决了「agent 之间怎么**干活**」。

---

## 目录

- [为什么需要它](#为什么需要它)
- [核心能力](#核心能力)
- [架构](#架构)
- [快速开始](#快速开始)
- [接入你的 Agent](#接入你的-agent)
- [A2A 协议接口](#a2a-协议接口)
- [多智能体协同](#多智能体协同)
- [Web 控制台](#web-控制台)
- [命令行](#命令行)
- [配置参考](#配置参考)
- [部署](#部署)
- [开发与测试](#开发与测试)
- [安全注意事项](#安全注意事项)

> 设计取舍与协议映射细节（为什么这样实现、哪些还没做）见
> [`docs/protocol-notes.md`](docs/protocol-notes.md)。

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

加 `--url http://host:8080` 可操作远端 Hub；加 `--token <TOKEN>` 携带鉴权。

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

各厂商 Key 见 `.env.example`。

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

---

## 部署

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
SSE 分帧合法性、四种协同拓扑的行为契约、存储层契约（memory/sqlite 双后端参数化）。

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

## License

MIT
