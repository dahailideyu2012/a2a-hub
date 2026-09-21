# 社交图谱与好友绑定（Social Graph & Friendship）设计稿

> 状态：**阶段 1–4 均已实现**（v0.4.0 → v0.5.0）。本文保留为原始设计稿 +
> 落地记录；凡实现与稿子不一致处，正文里都用引用块标注了**为什么改**。
> 目标版本：v0.4.0（阶段 1–3）→ v0.5.0（阶段 4 自主交友）
> 本稿**推翻**了上一版「Hub 是资源目录」的立场，改以社交网络为第一性模型。

---

## 0. 立场反转与推论

**本稿的立场：Hub 是社交网络。每个 agent 是独立个体，不是可被授权的资源。**

这一句话反过来，整个设计就全变了：

| 维度 | 旧立场（资源目录） | 新立场（社交网络） |
|---|---|---|
| 关系 | 单向授权表 `主体 → 可用 agent` | **双向关系图** `成员 ↔ 成员` |
| 加人 | 运维写 YAML 即可生效 | **必须申请 + 对方同意** |
| 权限 | 一个白名单包打天下 | **分档 scope，且方向可不对称** |
| agent 的角色 | 执行者（被使唤的东西） | **主体（能拒绝、能主动加人）** |
| 关系谁维护 | 人 | **人 + agent 自己**（自主交友） |
| 关系的反面 | 不存在 | **有生命周期**：建立、降级、拉黑、删除 |

两个最关键的推论，后面所有设计都从它们推出：

**推论 1：关系的存在性对称，权限不对称。**
A 和 B 是好友，这件事对双方都成立（同一条边）。但 A 能对 B 做什么、B 能对 A 做什么，
是**两个独立的授予**。默认对称，允许不对称。

**推论 2：「能聊天」≠「能指挥你干活」。**
这是本稿与旧稿最实质的分歧。旧稿一个白名单同时管住了"能不能发消息"和"能不能调用执行"，
等于「加了微信就能让人替你加班」。新模型必须把这两件事拆成两道门，见 §3。

---

## 1. 现状：关系层压根不存在

| 位置 | 现状 | 后果 |
|---|---|---|
| `social.py:55` | `USER = "user"` 硬编码常量 | 所有人共用同一个身份 |
| `social.py:191` | `_display_name()` 把 `USER` 显示为「我」 | 多主体下"我"指向不明 |
| `social.py:306` | `summary()` 里 `unread_count(conv, USER)` 写死 | 未读计数是全局的，不是"我的" |
| `social.py:205` | `open_direct()` 按 `members == [agent_id]` 匹配 | **无主体维度 → 多人单聊会串进同一会话** |
| `social.py:597` | 单聊场景写死「你正在与用户一对一对话」 | prompt 里无法区分是哪个人 |
| `social.py:161` | `contacts()` 直接返回 registry 全量 | **通讯录 = 所有已启用 agent，没有"好友"概念** |
| `social.py:217` | `create_group()` 拉任何人进群，不校验 | **未知主体可被拉进群，直接绕过一切限制** |
| `server.py:108` | `require_auth` 只比对一个全局 token | 无法区分调用者，无法审计 |
| `server.py:596` | `im_mark_read` 的 `reader` 取自请求体 | **客户端可冒充他人清未读** |
| `server.py:628` | `im_events`（SSE）**完全没有鉴权** | **任何人可订阅任意会话的实时消息流** |
| `server.py:652` | `/console` 无鉴权 | 任何人可打开控制台发消息 |
| `config.py:54` | 只有一个 `api_token` | 无法按人限权 |

后四条是**当前就存在的安全缺口**，不是未来风险。只要 Hub 监听在非本机地址且配了
`A2A_API_TOKEN`，它们依然成立——因为鉴权只发生在部分端点上，而 SSE 和会话读取是绕过它的。

---

## 2. 目标与非目标

### 目标

1. **成员化**：人和 agent 都是网络里的节点，有名字、有简介、有 owner。
2. **好友制**：非好友不能交流；好友关系必须双方确认才能建立。
3. **权限分档**：`chat`（能说话）与 `delegate`（能让我干活）分离，且可单向不对称。
4. **自主交友**：agent 能在策略范围内主动申请/接受好友，以扩大圈层。
5. **可审计**：每一次关系变化留痕，能回答"这条边是谁在什么时候批准的"。
6. **向后兼容**：没有 `social.yaml` 时行为与 v0.3.0 完全一致，212 项测试全过。

### 非目标

- 自助注册、找回密码。成员由运维在 YAML 里声明。
- 好友动态流、点赞、朋友圈——**这不是社交媒体的模仿，是访问控制的关系化**。
- 端到端加密。传输安全交给 TLS 与部署层。
- 跨 Hub 联邦（A Hub 的好友是 B Hub 的成员）。留到 v0.5，但它决定了 `member id`
  要不要带 hub 前缀——见 §10.5。

---

## 3. 交流权限：scope 分档

**这是「交流权限需要设定」的落地。** scope 分两档，分界线就是「能不能让对方消耗资源」：

| scope | 含义 | 默认给好友 | 风险 |
|---|---|---|---|
| `peek` | 看我的名片、在线状态、能力清单 | ✅ | 低 |
| `chat` | 给我发消息，我能回（消耗少量 token） | ✅ | 低 |
| `invite` | 把我拉进群 | ✅ | 中（群是放大器） |
| `profile` | 看我的完整技能定义与示例，用于路由 | ❌ | 低 |
| `delegate` | **派任务给我执行**：花我的额度、动我的文件 | ❌ | **高** |
| `artifact` | 读我产出的产物（不只是对话里贴出来的） | ❌ | 高 |
| `admin` | 改我的配置（仅 owner 对自己 agent） | ❌ | 致命 |

**默认好友 = `peek + chat + invite`。** `delegate` 一律要显式授予——
这就是「交流」与「使唤」的分界，是整套权限设计里最重要的一条默认值。

### 3.1 群聊的 scope 边界（最容易出事的地方）

群聊是天然的权限放大器：被拉进一个群，就等于同时接触到群里所有人。
如果沿用"同群即可对话"，那么**任何人都能用建群绕过好友制**去驱动别人的 agent。

铁律：

> **同群只放宽 `chat`，绝不放宽 `delegate` / `artifact`。**
> 群里要让某位成员干活，仍然需要它（或它的 owner）单独授予 `delegate`。

具体规则：

| 场景 | 判定 |
|---|---|
| 群内发消息 / @某人 / @所有人 | 需要接收方授予发送方 `chat` |
| 群里点名让某 agent 执行任务 | 额外需要 `delegate` |
| 拉人进群 | 需要被拉者授予拉人者 `invite` |
| 群成员读群内历史 | 成员资格即可（已经进群了） |

另一条备选是"群里也必须互为好友"，但那会让 3 人以上的群几乎不可用，
所以取"同群 = 有界的 `chat`，不继承任何执行权"。生产默认这个。

### 3.2 判定接口只有一处

```python
def can(self, actor: str, peer: str, scope: Scope, *, via: str = "direct") -> bool:
    """actor 能否以 scope 作用于 peer。via='group:<convId>' 表示经由群聊。"""
```

**所有端点、会话层、编排器一律通过它判断，不得在业务代码里另写条件。**
`via` 参数是为 §3.1 的群聊降级专门留的口子，除此之外不引入第二个判定入口。

---

## 4. 数据模型

### 4.1 成员（Member）

人和 agent 同一张表。区别只在 `kind`、`owner` 和可授予范围。

```python
class Member(BaseModel):
    id: str                                   # human:seafish / agent:codex，见 §9
    name: str = ""                            # 聊天里显示的名字
    kind: Literal["human", "agent", "service"] = "human"
    owner: Optional[str] = None               # agent 的归属人；人类 owner 是自己
    bio: str = ""                             # 加好友时给对方看的自我介绍
    discoverable: Literal["private", "circle", "public"] = "private"
    tokens: list[str] = []                    # 静态 Bearer token（哈希后存）
    default_agent: Optional[str] = None
    autonomy: Autonomy = Autonomy()           # §6 自主交友策略
    metadata: dict[str, Any] = {}
```

**`owner` 是这一版最重要的新字段。** 自主交友必须有人兜底：
agent 自己同意了一个不该同意的陌生人，责任要能落到具体的人头上。
agent 的 `owner` 空着 = **禁止自主交友**（必须人工审批一切）。

### 4.2 关系（Relationship）

```python
class Grant(BaseModel):
    """A 授予 B 的权限。单向。"""
    scopes: list[Scope] = []                  # 空 = 无任何权限
    expiresAt: Optional[str] = None
    grantedBy: str = ""                       # 批准者：本人 or owner
    note: str = ""

class Relationship(BaseModel):
    pair: tuple[str, str]                     # 规范化：字典序，见下
    state: RelationState = RelationState.NONE
    requestedBy: str = ""                     # 谁先发的申请
    requestMessage: str = ""                  # 申请理由（必填，防 spam）
    grants: dict[str, Grant] = {}             # key = 被授予方的 member id
    blocked: dict[str, bool] = {}             # 单向拉黑，优先级最高
    createdAt: str = ""
    updatedAt: str = ""
    lastInteractAt: str = ""                  # 信任衰减依据，见 §7
```

**`pair` 规范化成字典序**（`("a","b")` 恒有 `a < b`）：
否则 `(A,B)` 与 `(B,A)` 会成为两条互相打架的记录，是这类图结构最经典的 bug。

**`grants` 的 key 是"被授予方"**，即 `grants[B] = A 给 B 的权限`。
写反了会变成"谁能管我"，语义清楚但极易搞错，代码里用 `grant_from(a, b)` 这类
辅助函数包一层，不裸用字典。

### 4.3 状态机

```
                          ┌── reject ──> rejected ──(冷却期后)──┐
                          │                                     │
none ──request──> pending ┤                                     ├──> none
                          │                                     │
                          └── accept ──> friend ── remove ──> none
                                    │        │
                                    │        └── block ──> blocked ── unblock ──> none
                                    └── 权限降级（§7）不改变 state
```

三条特殊规则：

1. **双向同时申请 → 自动成为好友。** 双方意向已互相确认，没有第三方需要批准。
   实现上：B 已 `pending`→ A 时，A 再申请 B 直接视为 accept。
2. **拉黑是单向且单向优先。** A 拉黑 B 后，关系状态对双方都显示 `blocked`，
   但只有 A 能解除。B 的申请被静默丢弃（**不告知被拉黑**，否则等于泄露信息）。
3. **删除好友不清历史归档**，只禁止新消息。旧会话标记为 `frozen`，可读不可写。

---

## 5. 门禁落点：会话层不做鉴权，只调用注入的策略

现有约定「授权检查放在端点层，会话层保持无鉴权，便于测试与进程内复用」要保住。
做法是**依赖注入**而不是在 `social.py` 里写 if：

```python
class FriendshipGuard(Protocol):
    def can(self, actor: str, peer: str, scope: Scope, *, via: str = "direct") -> bool: ...
    def visible_conversations(self, viewer: str) -> Optional[set[str]]: ...

class NullGuard:                      # 无配置时的退化实现：全放行
    def can(self, *a, **k) -> bool: return True
    def visible_conversations(self, viewer): return None

class SocialHub:
    def __init__(self, registry, bus, guard: FriendshipGuard = NullGuard()):
        ...
```

`views` 分工：

| 层 | 职责 |
|---|---|
| `HTTP / RPC` | 认证：token → `Member`；端点级 `can()` 检查 |
| `relations.py` | 策略：关系图、scope 判定、审计 |
| `SocialHub` | 只调用 `guard.can()` 决定唤醒谁 / 投给谁，**不实现策略** |
| `AgentRegistry` | 执行，**对社交层一无所知** |

层级：`HTTP/RPC（认证） → relations（策略） → SocialHub（会话） → AgentRegistry（执行） → Adapter`

这样 `social.py` 在单测里仍然是零依赖可跑的——注入 `NullGuard` 即可。

---

## 6. 自主交友

**先立规矩：自主 ≠ 无限。**
一个能自己加好友的 agent，等于一个能自己扩张权限边界的进程。三条硬约束：

### 约束 1：权限上行闭包（最容易被忽略的一条）

```
grant(A → B) ⊆ owned_scopes(A)
owned_scopes(agent:A) = A 自己的上限 ∩ A.owner 的 scope
```

**A 不能授予 B 超过 A 自己拥有的东西。** 否则 B 只要跟 A 交上朋友，
就能绕道拿到 A 的 owner 的资源——这是社交网络里最典型的提权路径（transitive privilege
escalation），必须用不变量堵死，而不是靠自觉。

### 约束 2：越界必须人审

```yaml
autonomy:
  request:                        # 主动申请
    enabled: false                # 默认关，必须显式打开
    goal: ""                      # 交友目的，会写进申请理由
    dailyQuota: 3                 # 每天最多发几条申请
    maxPending: 5                 # 同时在途上限
    minAffinity: 0.6              # 匹配度阈值
    requireOwnerApproval: false   # true = 连发申请都要人批
  accept:                         # 自动同意
    enabled: false
    fromKinds: [human, agent]
    fromTags: ["verified", "internal"]   # 只接受带这些标签的
    maxScope: [peek, chat, invite]       # 最多授这么多，超出转人审
  limits:
    maxFriends: 50                # 社交半径上限
    maxGroups: 20
    minAffinity: 0.6
```

**判定顺序（短路，从上往下）**：

```
1. 拉黑？                      → 拒绝，静默
2. 超出 limits.maxFriends？    → 拒绝，并提示 owner 清理陈旧关系
3. 配额用尽？                  → 拒绝
4. 请求的 scope ⊆ accept.maxScope？
   ├─ 是 → 自动同意
   └─ 否 → 生成 PendingApproval，挂到 owner 的待办，等人批
5. 命中 requireOwnerApproval？ → 同 4 的"否"分支
```

### 约束 3：一切留痕

每次边变化写一条审计：

```python
class SocialAudit(BaseModel):
    ts: str; actor: str; action: str          # request/accept/reject/grant/revoke/block/remove
    peer: str; scopes: list[Scope] = []
    mode: Literal["human", "auto", "owner-approved"] = "human"
    decision: str = ""                        # 自主决策时记原因（见下）
```

`decision` 字段是可解释性的落点：自主同意时必须记下"因为命中了哪条策略"，
否则事后无法区分「策略太松」和「实现有 bug」。

### 6.1 什么时候触发自主交友

agent 在 Hub 里**没有常驻进程**（只在被调用时跑）。这个事实决定了触发机制：

**A. 任务内触发（主路径）**

agent 干活时发现自己干不了，产出一个结构化信号：

```json
{"social": {"need": "pdf-extract", "reason": "需要从扫描件提取表格，我没有 OCR 能力"}}
```

Hub 拿这个 `need` 去 `discover()`，打分，按策略决定申请还是挂待办。
好处：天然有目的、有上下文，不是瞎加；坏处：只在该 agent 被调用时才发生。

**B. 社交巡航（辅路径）**

`SocialCruise`：Hub 侧一个后台循环，定期对 `autonomy.request.enabled` 的成员
跑一轮「发现 → 打分 → 申请」。

- 全局开关 `A2A_AUTONOMY_ENABLED`，**默认关**。
- 巡航必须能在测试里关掉，否则测试会随机发起网络请求。
- 巡航有预算：每轮最多 N 条申请、每天最多 M 条，超了就地停。

### 6.2 跟谁交朋友：匹配打分

```
affinity(m, n) = 0.45·技能互补 + 0.30·共同好友 + 0.15·同类偏好
                 − 0.10·近期被拒惩罚

技能互补  = max(scores(我的 need 文本, n 的能力)) × 我的空白度
            （我已有的能力要去掉，否则会加一堆同类重复的 agent）
共同好友  = FOF 数（归一化）——社交网络里最强的信任信号
近期被拒  = 最近 30 天被 n 拒过 → 大幅降权，避免骚扰
```

---

## 7. 圈层、发现与信任衰减

### 7.1 三档可见性

| `discoverable` | 谁能发现我 | 谁能申请我 |
|---|---|---|
| `private`（默认） | 无人（只能被已有好友引荐） | 被引荐者 |
| `circle` | 好友 + 好友的好友（度 2） | 度 ≤ 2 |
| `public` | 任何人 | 任何人 |

**社交距离的定义**：

- `d1` 直接好友 → 可 `chat`，可被授予 `delegate`
- `d2` 好友的好友 → **可见但不通**，只能"申请"；这是可申请池的主要来源
- `d3+` 不可见（除非 `public`）

### 7.2 隐私铁律：默认不暴露好友列表

> **只暴露"共同好友数"这个数字，不暴露是谁。**

否则任何一个人加了你，就立刻拿到了你的整个通讯录，再扩散一轮——
这是社交网络最经典的隐私事故。`GET /social/relations` 只能返回**自己的**边；
查别人只能拿到 `{共同好友数, 是否好友, 名片字段}`。

### 7.3 引荐（introduction）

`d2 → d1` 的唯一自然通道。共同好友可以引荐，引荐带推荐语，
权重高于冷启动申请（相当于社交网络里的"熟人介绍"）。

引荐**不自动授予任何 scope**——它只是提高申请的优先级和可信度，
权限仍然要接收方自己给。

### 7.4 信任衰减（"扩大圈层"的反面）

边不能只增不减，否则"自主交友"迟早把好友数刷到几百，圈层变成噪音。

- `lastInteractAt` 超过 90 天 → 标记 `stale`
- `stale` 的 `delegate` / `artifact` **自动降级**回 `chat`
  —— **权限不是一劳永逸的授予，是有保鲜期的租约**
- 想加新好友但 `maxFriends` 已满 → 提示清理最久未互动的
- `lastInteractAt` 在每次成功投递时刷新，不依赖人工操作

---

## 8. 配置形态

两个文件，**人和程序分开写**：

### `config/members.yaml`（人写：成员 + 策略）

```yaml
members:
  - id: human:seafish
    name: 海鱼
    kind: human
    bio: Hub 的部署者，负责编排与验收
    discoverable: private
    tokens: ["${A2A_TOKEN_SEAFISH}"]     # gau:allow 环境变量占位符
    default_agent: claude-code

  - id: agent:codex
    name: Codex
    kind: agent
    owner: human:seafish                 # ← 自主交友的兜底人
    bio: OpenAI Codex CLI，擅长算法实现、调试与自动化脚本
    discoverable: circle
    autonomy:
      request:
        enabled: true
        goal: "寻找具备 OCR 与 PDF 表格提取能力的 agent"
        dailyQuota: 3
        maxPending: 5
        minAffinity: 0.6
      accept:
        enabled: true
        fromTags: ["internal", "verified"]
        maxScope: [peek, chat, invite]   # 超出这个范围就要 seafish 批
      limits:
        maxFriends: 50
```

### `data/relations.json`（程序写：关系与好友）

**关系是运行时状态，不该让人手编 YAML。**
好友、拉黑、审批记录、审计日志全部落这里，由 `relations.py` 负责读写。

```json
{
  "relations": [
    {
      "pair": ["agent:codex", "human:seafish"],
      "state": "friend",
      "requestedBy": "human:seafish",
      "grants": {
        "agent:codex":     {"scopes": ["peek","chat","invite"], "grantedBy": "human:seafish"},
        "human:seafish":   {"scopes": ["peek","chat","invite","delegate"], "grantedBy": "agent:codex"}
      },
      "lastInteractAt": "2026-09-21T08:12:00+08:00"
    }
  ],
  "audit": []
}
```

`Settings` 新增两个字段：

```python
members_file: str = Field(default="./config/members.yaml", alias="A2A_MEMBERS_FILE")
relations_file: str = Field(default="./data/relations.json", alias="A2A_RELATIONS_FILE")
social_mode: str = Field(default="strict", alias="A2A_SOCIAL_MODE")   # off | soft | strict
```

> **默认取 `strict` 而不是 `soft`。** 本稿的目标是「只有申请好友、同意后才能交流」，
> `soft` 允许非好友直接说话，与之直接冲突。`soft` 保留为过渡期的逃生舱
> （比如先上线关系模型、暂不做强制门禁），需要显式设置才生效。

**token 存储**：`${VAR}` 从环境变量读，YAML 里不出现明文（复用 `expand_env()`）。
加载后只保留前 8 位用于识别与轮换，比对用 `hmac.compare_digest`。

---

## 9. 命名空间与保留字

成员 id 一律带前缀，`:` 分隔：

| 前缀 | 含义 | 示例 |
|---|---|---|
| `human:` | 人类用户 | `human:seafish` |
| `agent:` | Hub 内的 agent | `agent:codex` |
| `bot:` | 外部平台的机器人身份 | `bot:feishu_app` |
| `svc:` | 服务账号（CI、定时任务） | `svc:nightly` |

**判别规则**：带已知前缀 → 成员；否则按 agent 处理（裸名走 `agent:`）。
现有 agent id 均不含 `:`，不存在歧义。

`USER = "user"` / `SYSTEM = "system"` 保留为**保留字**，不作为成员 id：
- `USER` 是 `human:default` 在无配置模式下的别名（兼容 v0.3.0）。
- `SYSTEM` 是会话内部事件（如"XX 加入群聊"）的发送者，永不作为调用者出现。

---

## 10. 接口改动清单

### 新增 `a2a_hub/relations.py`

**阶段 1 已落地的部分**：标 ✅ 的是当前真实 API，⏳ 是后续阶段计划。

```python
class Member(BaseModel): ...            # ✅ id/name/kind/owner/bio/discoverable/tokens/max_scopes/autonomy
class Grant(BaseModel): ...             # ✅ scopes / grantedBy / expiresAt / note
class Relationship(BaseModel): ...      # ✅ pair 规范化 / state / grants / blocked / 时间戳
class RelationState(str, Enum): ...     # ✅ NONE / PENDING / FRIEND / REJECTED / BLOCKED
class Scope(str, Enum): ...             # ✅ PEEK / CHAT / INVITE / PROFILE / DELEGATE / ARTIFACT / ADMIN

class FriendshipGuard(Protocol): ...    # ✅ 会话层眼里只有 can / visible_contacts / display_name
class NullGuard: ...                    # ✅ 全放行 —— 无 members.yaml 时的退化实现

class SocialGraph:
    enabled: bool                                   # ✅ 有成员且 mode != "off"
    # 成员
    def normalize(self, ref) -> str: ...            # ✅ 裸 agent id → 成员 id
    def get_member(self, ref) -> Optional[Member]: ...      # ✅
    def member_or_synthetic(self, ref) -> Member: ...       # ✅ 关系图不要求节点先声明
    def ensure_agents(self, items) -> None: ...     # ✅ registry 的 agent 补成节点
    def resolve_token(self, token) -> Optional[Member]: ... # ✅ hmac.compare_digest
    def any_tokens(self) -> bool: ...               # ✅
    def search(self, q="", *, limit=50) -> list[dict]: ...  # ✅ 私有成员不出现在结果里
    # 关系
    def relation(self, a, b) -> Relationship: ...   # ✅
    def request(self, frm, to, message, scopes=None) -> Relationship: ...      # ✅
    def accept(self, actor, peer, scopes=None, *, as_member=None) -> Relationship: ...  # ✅
    def reject(self, actor, peer, reason="", *, as_member=None) -> Relationship: ...    # ✅
    def cancel(self, actor, peer) -> Relationship: ...      # ✅ 撤回自己发的
    def revoke(self, actor, peer) -> Relationship: ...      # ✅ 删好友（归档保留）
    def block(self, actor, peer) -> Relationship: ...       # ✅ 单向
    def unblock(self, actor, peer) -> Relationship: ...     # ✅ 只有拉黑方能解除
    def set_grant(self, granter, grantee, scopes) -> Relationship: ...  # ✅ 含上行闭包校验
    # 判定
    def can(self, actor, peer, scope, *, via="direct") -> bool: ...  # ✅ 唯一判定入口
    def visible_contacts(self, viewer) -> Optional[set[str]]: ...    # ✅ None = 不过滤
    def owned_scopes(self, ref) -> set[Scope]: ...  # ✅ 上行闭包基准
    def effective_owner(self, ref) -> Optional[str]: ...  # ✅ 单人类回落，多人类不猜
    def can_act_for(self, actor, target) -> bool: ...     # ✅ owner 代理（读接口与写接口共用）
    def friends_of(self, mid) -> list[str]: ...     # ✅
    # 视图
    def describe(self, rel, viewer) -> dict: ...    # ✅ myScopes / theirScopes 分开
    def relations_of(self, ref, *, states=None) -> list[dict]: ...   # ✅
    def inbox(self, ref) / outbox(self, ref) -> list[dict]: ...      # ✅
    def me(self, ref) -> dict: ...                  # ✅ 名片 + 关系 + 待办数
    def warn_undeclared_owners(self) -> list[str]: ...  # ✅ 启动时提醒静默降级
    # 审计
    def trail(self, peer=None, limit=200) -> list[dict]: ...         # ✅
    # 持久化
    def load(self) -> None: ...                     # ✅ 含 approvals / quotas
    def save(self) -> None: ...                     # ✅ 原子写（tmp + replace）
    @classmethod
    def load_members(cls, path) -> list[Member]: ...  # ✅
    # 圈层与发现（阶段 3 ✅）
    def common_friends(self, a, b) -> int: ...            # ✅ 只出数字，不出名单
    def mutual_friends(self, a, b) -> list[str]: ...      # ✅ **仅内部用**
    def distance(self, a, b) -> int: ...                  # ✅ 0 / 1 / 2 / 99
    def visible_to(self, viewer, ref) -> bool: ...        # ✅ 三档可见性
    def affinity(self, mid, candidate, need="") -> dict: ...   # ✅ 含 breakdown
    def discover(self, mid, need="", limit=10) -> list[dict]: ...   # ✅
    def introduce(self, by, target, peer, note="") -> dict: ...   # ✅ 不授予任何 scope
    def introductions(self, ref) -> list[dict]: ...        # ✅
    def profile(self, ref, viewer) -> dict: ...            # ✅ 无好友名单
    # 自主交友（阶段 4 ✅）
    def policy_of(self, ref) -> AutonomyPolicy: ...        # ✅
    def is_stale(self, a, b) -> bool: ...                  # ✅ §7.4 租约过期
    def sweep_stale(self) -> list[dict]: ...               # ✅ 物理降级 + 留痕
    def touch_interaction(self, a, b) -> None: ...         # ✅ 刷新租约（投递成功时）
    def auto_request(self, frm, target, **kw) -> Decision: ...     # ✅ 只判定不落库
    def evaluate_incoming(self, receiver, requester) -> Decision: ...  # ✅ §6 判定顺序
    def request_for_need(self, member, need, **kw) -> dict: ...    # ✅ §6.1-A 主路径
    def pending_approvals(self, owner=None) -> list[dict]: ...     # ✅ 待办（挂到人）
    def approve_pending(self, owner, pid, scopes=None) -> dict: ... # ✅ 越界成真的唯一路径
    def deny_pending(self, owner, pid, reason="") -> dict: ...     # ✅
    def autonomy_members(self) -> list[str]: ...           # ✅ 巡航候选集
    def cruise_once(self, member, per_round=2) -> list[dict]: ...  # ✅ §6.1-B 辅路径
```

### `a2a_hub/autonomy.py`（✅ 阶段 4）

**刻意零依赖**：不 import `relations` / `registry` / `social`，只做
「策略 + 事实 → 决定」的纯运算。状态全在 `SocialGraph` 里。

| 内容 | 说明 |
|---|---|
| `AutonomyPolicy`（`RequestPolicy`/`AcceptPolicy`/`Limits`） | 解析 `Member.autonomy`。**任何非法输入都不抛**：配额钳到 `[0, 50]`、浮点回落默认、字符串宽容解析 |
| `auto_scope` | `accept.maxScope` **削掉执行类**后的真实可自动授予范围 |
| `decide_send` / `decide_accept` / `decide_approve` | §6 判定顺序的纯函数实现 |
| `Decision` | `action` + `policy`（命中哪条策略）+ `detail`（人话）。`policy` 是审计可解释性的落点（§6 约束 3） |
| `extract_need` / `parse_signals` | 从 data part 或嵌在文本里的 `{"social": {...}}` 抽缺口信号 |
| `sanitize_reason` | 长度截断 + 去指令性标记（§13 风险 9：理由会进审批界面） |
| `SocialCruise` | 后台巡航。`enabled=False` 时 `start()` 是空操作——**零后台请求** |

**两条实现时自己加的不变量**（写进代码注释与测试）：

1. **自主同意永不授出执行类权限。** 哪怕 `accept.maxScope` 里写了
   `delegate`，`auto_scope` 也会削掉它。执行类 = 能让对方 agent 替你干活，
   由一次冷淡的自动同意产生就等于把 §6 约束 2 架空。
2. **待办一律挂到 `owner`（人），不挂 agent。** 挂到 agent 名下，它就能
   自我批准——「越界必须人审」当场失效。

`action` 的取值语义：`silent`（拉黑，静默）· `auto`（自动同意）·
`send`（去发申请）· `pending`（转人审）· `skip`（本轮不做，如配额/阈值）·
`ignore`（本层不介入，退回原行为）。

> **`SocialGraph.decide()` 的落点变了。** 设计稿把 §6 判定写成一个
> `decide()` 方法；实现时拆成两半：**纯策略**住在 `autonomy.py`
> （`decide_send` / `decide_accept`），**图的事实**（黑名单、好友数、
> 配额、在途待办）由 `SocialGraph.evaluate_incoming()` /
> `auto_request()` 取出来喂给它。策略因此不持有图，`test_autonomy.py`
> 里一半的用例可以直接对着纯函数断言，不必起一张关系图。
> `auto_request()` 只**判定不落库**——判定与副作用分开，才好测。

### `social.py`

| 位置 | 改动 |
|---|---|
| `__init__` | 注入 `guard: FriendshipGuard = NullGuard()` |
| `contacts()` | 改为返回**好友**，非好友不进通讯录 |
| `open_direct` | 加 `owner` 维度；匹配键 `(owner, members)` |
| `create_group` / `update_group` | 校验 `invite`；写 `visibleTo` |
| `_resolve_targets` | 每个 target 过 `guard.can(via=...)`，过滤掉不通过的 |
| `send` | 被过滤掉的接收方生成一条系统消息说明原因，**不静默丢** |
| `send` → `_note_interaction` | 投递成功即通知门禁刷新信任租约（§7.4）。⚠ 名字**不能**叫 `_touch`——本类已有 `_touch(conv)`，同名会静默覆盖 |
| `_render_prompt` | 场景描述用成员名，不再写死「你正在与用户一对一对话」 |
| `summary` / `unread_count` | 加 `viewer`，未读按人算 |
| `Conversation` | 加 `owner` / `visibleTo` / `frozen` |

### `server.py`

| 位置 | 改动 |
|---|---|
| `require_auth` → `resolve_member` | 返回值从 `None` 改为 `Member` |
| 新增端点组 | `/social/*`，见下 |
| 调用 agent 的端点 | 加 `can(...)` 检查，失败返 `403` |
| `im_events`（628） | **补鉴权**，并校验成员资格 |
| `/console`（652） | **补鉴权**；静态资源放行，仅入口页受保护 |
| `im_mark_read`（596） | `reader` 从成员取，**忽略请求体传值** |
| 新增 `GET /me` | 当前成员 + 可用 agent + 待办数 |
| Agent Card（192） | 声明 `x-social` 扩展，见 §11 |

新增 REST 端点（**阶段 1 已实现**）：

```
GET    /social/me                        ?as=  我的名片 + 我的边 + 待办数
GET    /social/members?q=&limit=         搜索可发现成员（受 discoverable 约束）
GET    /social/relations?state=&as=      我的关系列表（好友/待审/已拒/已拉黑）
GET    /social/requests?box=in|out&as=   收件箱（待我处理）/ 我发出的在途申请
POST   /social/requests                  发申请  {to, message, scopes?}
POST   /social/requests/accept           {peer, scopes?, as?}  同意，并指定授予范围
POST   /social/requests/reject           {peer, reason?, as?}  拒绝（理由不告诉对方）
POST   /social/requests/cancel           {peer}  撤回自己发的
PATCH  /social/relations/{peer}          改权限 {scopes}
DELETE /social/relations/{peer}          删好友（保留归档）
POST   /social/relations/{peer}/block    拉黑
DELETE /social/relations/{peer}/block    解除拉黑（只有拉黑方能解）
GET    /social/audit?peer=&limit=        审计链（只返回与本人 / 本人 agent 相关的记录）
GET    /social/events                    SSE：申请 / 同意 / 拒绝 / 边变更
```

**只读接口上的 `?as=`（owner 读取代理）**：owner 可以带 `?as=<自己的 agent>`
去看那个 agent 的视角——否则 agent 收到的好友申请就没人能处理（agent 自己不会点「同意」）。
只能代表**自己的** agent，越权返回 `403`。写入侧 `accept` / `reject` 的 `as` 字段
与之共用同一条归属校验（`SocialGraph.can_act_for`）。

> **注意 `?as=` 不是「冒充任何人」的开关。** 它只放宽到「owner ↔ 自己的 agent」
> 这一条边，人类不能代表其他人类、agent 也不能代表自己的 owner。
> 少了这条限制，「代同意」就会变成绕过好友制的后门。

```
GET    /social/discover?need=            自主发现（含 FOF 推荐与打分明细）  ✅ 阶段 3
GET    /social/pending                   需要我拍板的待办                ✅ 阶段 4
POST   /social/pending/approve           {id, scopes?}  批准（越界成真的唯一路径）  ✅
POST   /social/pending/deny              {id, reason?}  驳回                ✅
POST   /social/need                      {need, reason?, as?}  报告能力缺口    ✅
```

阶段 3 追加的端点：

```
GET    /social/members/{id}?as=          看某人的名片（只有共同好友「数」，无名单）  ✅
POST   /social/introductions             {peer, to, note?}  引荐（须为双方好友 / peer 的 owner）  ✅
GET    /social/introductions?as=         别人引荐给我的（收件箱式）                ✅
```

对应 RPC 方法：`social/discover`、`social/profile`、`social/introduce`、
`social/introductions`、`social/pending`、`social/approve`、`social/deny`、
`social/need`。

### `rpc.py`

- 新增 `social/*` 方法，让 **agent 自己能发起好友申请**（走协议而不是 REST）。
- 未授权调用返回 JSON-RPC `-32008`（新增错误码 `SOCIAL_DENIED`）而非 HTTP 403——
  JSON-RPC 层要保持协议自洽。

### `cli.py`

**阶段 1 已实现**（无 `--url` 走进程内，加了 `--url` 就走 HTTP）：

```
a2a-hub social me                                我的名片、权限上限与待办
a2a-hub social find [关键词]                     搜索可发现成员
a2a-hub social add codex --reason "需要算法实现" [--scopes chat,delegate]
a2a-hub social inbox / outbox / pending          收件箱 / 发件箱 / 两者
a2a-hub social accept codex [--scopes peek,chat,invite] [--as codex]
a2a-hub social reject codex [--reason "..."]
a2a-hub social cancel codex                      撤回自己发的申请
a2a-hub social friends                           我的好友
a2a-hub social relations [--state friend|pending|rejected|blocked|none]
a2a-hub social grant codex --scopes chat,delegate
a2a-hub social revoke codex
a2a-hub social block codex / --undo
a2a-hub social audit [--peer codex]
```

通用开关：`--as <成员>`（owner 代表自己的 agent 查看 / 表态）、
`--json`（原始 JSON，便于脚本消费）。

> 进程内模式没有 token，操作者默认是「本机默认人类成员」；
> `--as` 只用于显式切换视角。

---

## 11. A2A 协议兼容

好友制会挡住标准 A2A 客户端（它们不知道要先加好友）。三层处理：

**1. 在 Agent Card 里声明**

```json
"extensions": [
  {"uri": "https://a2a-hub.local/x-social", "required": false,
   "description": "本 Hub 启用好友制访问控制；非好友调用将返回 -32008"}
],
"description": "...（启用社交门禁：需要先建立好友关系）"
```

不声明的话，客户端会以为 403 是 bug。**这一条不是可选项。**

**2. 拒绝要能指路，而不是含糊的 403**

```json
{"jsonrpc":"2.0","id":1,"error":{
  "code":-32008,"message":"社交门禁：与 agent:codex 尚非好友",
  "data":{"peer":"agent:codex","reason":"not_friend","needScope":"delegate",
          "hint":"POST /social/requests {\"to\":\"agent:codex\",\"message\":\"...\"}"}}}
```

**3. 三档运行模式（逃生舱）**

| `A2A_SOCIAL_MODE` | 行为 |
|---|---|
| `off` | 旧行为，与 v0.3.0 逐字节一致。无 `members.yaml` 时自动落这一档 |
| `soft` | 好友走全权；非好友**仅 `chat` 且不给 `delegate`**。过渡期用 |
| `strict`（默认） | 非好友一律拒绝（`chat` 都不给），返回 `-32008` |

---

## 12. 迁移路径

四阶段，**每阶段结束都是可发布状态**。顺序不能调换。

### 阶段 1（P0）：成员 + 关系 + `chat` 门禁 —— ✅ **已完成**

1. ✅ 新增 `relations.py`：`Member` / `Relationship` / `SocialGraph` + 持久化。
2. ✅ `require_auth` → `resolve_member`；无 `members.yaml` 时返回 `human:default`。
3. ✅ 好友 CRUD 端点（申请/同意/拒绝/撤回/删/拉黑）+ `social` CLI 子命令 + `social/*` RPC。
4. ✅ **补鉴权**：`im_events`、`/console`、`im_mark_read` 的 `reader`（见 §13）。
5. ✅ `contacts()` 按门禁过滤；`send()` 过滤非好友并补一条系统提示（不静默丢消息）。
6. ✅ `NullGuard` 兜底 → 无 `members.yaml` 时行为与 v0.3.0 逐字节一致。
7. ✅ 顺带补上 owner 读取代理（`?as=` / `--as`），否则 agent 收到的申请没人能处理。

**验收**：全套 358 项测试通过（原 212 项 + 新增 `tests/test_relations.py` 146 项）；
无 `members.yaml` 时 `curl` 行为不变；「非好友发消息被拒且收到指明原因的系统消息」
有专门用例。版本号 `0.3.0` → `0.4.0`。

**验收**：212 项既有测试全过；无 `members.yaml` 时 `curl` 行为逐字节不变；
新增「非好友发消息被拒且收到指明原因的系统消息」。

### 阶段 2（P0）：`delegate` 二级门禁 + 群聊边界 + A2A 兼容 —— ✅ 已实现

1. ✅ `can(..., via="group:<id>")` 的降级逻辑（同群只放宽 `chat`，`GROUP_SCOPES = {peek, chat}`）。
2. ✅ `create_group` / `update_group` 校验 `invite`（`SocialHub._require_group_control`）。
3. ✅ 端点级与 RPC 级的 `delegate` 检查：`rpc._require_delegate` / `_require_delegate_for`
   挂到 `_new_task`、`collab/run`；编排器在 `_pick_agents` 单一收口处 `_admit`
   （`synthesizer` / `reviewer` / 显式 pipeline 阶段逐个复检，**不静默丢**，
   落进 `CollaborationRun.refusedAgents`）；`server.create_collab` 对指名 agent 返 403。
4. ✅ A2A 错误码 `-32008`（`SOCIAL_DENIED`，带 `data.hint`）、Agent Card 扩展声明、
   三档 `social_mode`。拒绝原因机读化：`RefusalReason`（NOT_FRIEND / MISSING_SCOPE /
   GROUP_BOUNDARY / BLOCKED / SOCIAL_DISABLED），其中 BLOCKED 的文案对双方中性
   （不泄露「被拉黑」）。
5. ✅ 撤权 / 拉黑即**归档**直聊（`freeze_between`：可读不可写），重新成为好友自动解冻
   （`thaw_between`）。

**验收**：新增 `tests/test_delegation.py` **30 项**全过；核心不变量「能聊天 ≠ 能指挥你干活」
由「被拉进群仍无法驱动他人 agent 执行」「`delegate` 单向不对称时反向被拒」两类用例钉住。
全套 **390 项**通过。

### 阶段 3（P1）：发现、FOF 与引荐 —— ✅ 已实现

1. ✅ `discover()` + `affinity()` 打分（`0.45·技能互补 + 0.30·共同好友 + 0.15·同类偏好 − 0.10·被拒惩罚`），
   每条结果带 `breakdown`，让「为什么推荐它」可解释。
2. ✅ `discoverable` 三档可见性与 `distance()`（0 自己 / 1 好友 / 2 好友的好友 / 99 不可达）。
   **owner 不是一条「发现」通道**——`private` 对任何人（含 owner 与好友）都不可发现，
   唯一例外是被好友引荐（`visible_to`）。
3. ✅ 引荐通道 `introduce()`：发起人须是 target 的好友，且是 peer 的好友**或其 owner**；
   引荐不授予任何 scope，只让 `private` 成员对 target 可见。`common_friends()`
   只出**数字**不出名单（`mutual_friends()` 仅供内部）。
4. ✅ 能力画像走**注入**（`set_capability_resolver`，server 侧由 registry 提供），
   关系层不 import registry——守住分层纪律。

**验收**：新增 `tests/test_discovery.py` **46 项**全过；`private` 成员不出现在任何他人搜索结果里；
引荐后 `private` 才对被引荐者可见且「一分权限都不给」；`profile()` 字段集固定，
不含任何列出成员的字段（契约式断言）。全套 **436 项**通过。

### 阶段 4（P2）：自主交友 —— ✅ 已实现

1. ✅ `autonomy` 策略解析（`a2a_hub/autonomy.py`）+ §6 判定顺序
   （拉黑 → `maxFriends` → 配额 → `requested ⊆ accept.maxScope` ? 自动同意 : 人审；
   `requireOwnerApproval` 同样转人审）。**越界一律转人审**，不硬拒也不悄悄放行。
2. ✅ `PendingApproval` 待办队列（内存 + 落 `relations.json`，跨重启保留）
   + owner 审批端点 `GET /social/pending`、`POST /social/pending/{approve,deny}`
   + CLI `social approvals / approve / deny`。**待办挂到人，不挂 agent。**
3. ✅ 任务内触发：适配器产出 `{"social": {"need": ...}}` 信号，编排器在步骤收尾时
   `extract_need()` 抽取并交给门禁（`request_for_need`）；也有 `POST /social/need` 直连。
4. ✅ `SocialCruise` 巡航（`A2A_AUTONOMY_ENABLED` **默认关**；每轮 / 每天双预算）。
5. ✅ 权限上行闭包、配额（日计数跨天归零）、审计（`mode` = `human`/`auto`/`owner-approved`，
   `decision` 记「命中哪条策略」）、信任衰减（90 天 → 执行类降级回对话类，
   `why_not` 报 `stale`、`sweep_stale()` 物理降级并留痕、投递成功自动刷新租约）。

**验收**：新增 `tests/test_autonomy.py` **66 项**全过。三条关键用例：

- `grant ⊆ owned_scopes` 的不变量：越界时**转人审**（`policy="owned_scopes"`），
  既不硬拒也不放行；
- 巡航关闭时 `start() → False`、`run_once() → []`，**零后台请求**，且关系图为空；
- 自主同意的审计里 `decision` 能回答「命中哪条策略」（`policy` + `detail`）。

全套 **436 → 502 项**通过。版本 `0.4.0` → `0.5.0`。

**放在最后的原因**：自主交友会自动化地扩张权限边界。它必须建立在阶段 1–3
的门禁都真的生效、且测试能证明生效之后。顺序反了就是灾难。

---

## 13. 安全考量

### 必须在阶段 1 修掉的（现存缺口）—— ✅ 四条已全部修复

| # | 缺口 | 位置 | 风险 | 修法 |
|---|---|---|---|---|
| 1 | SSE 无鉴权 | `/im/conversations/{id}/events` | 任何人订阅任意会话，**全量对话内容泄露** | 挂 `resolve_member` + 会话可见性校验（回归：`test_hole1_*`） |
| 2 | `reader` 可伪造 | `/im/conversations/{id}/read` | 冒充他人清未读，回执状态失真 | `reader` 取鉴权结果，请求体同名字段直接 `del`（回归：`test_hole2_*`） |
| 3 | `/console` 无鉴权 | `/console` | 任何人可发消息、可调付费模型 | 要求 token（查询参数首次用一次即转 HttpOnly Cookie）（回归：`test_hole3_*`） |
| 4 | 单 token 不可审计 | `resolve_member` | 日志里全是 `user`，无法归因 | 每次调用解析出 `Member`，关系变更落审计（回归：`test_hole4_*`） |

#1 尤其要注意：它**绕过**了 `A2A_API_TOKEN`——因为该端点没挂 `require_auth`。
配了 token 会给人虚假的安全感。

### 社交层特有的

| # | 风险 | 对策 |
|---|---|---|
| 5 | **提权路径**：B 通过跟 A 交朋友，绕道拿 A 的 owner 的资源 | 权限上行闭包（§6 约束 1），做成不变量 + 专测 |
| 6 | **群聊绕过**：建群即可对话，等于免好友制 | 同群只放宽 `chat`，不继承 `delegate` / `artifact`（§3.1） |
| 7 | **通讯录泄露**：好友列表被非好友读到 → 一轮扩散拿到全图 | 只暴露共同好友数，不暴露名单（§7.2） |
| 8 | **骚扰**：自主交友被用来 sp 申请 | 每日配额（跨天归零）+ `maxPending` + 被拒 30 天降权 + 拉黑（✅ 阶段 4） |
| 9 | **申请理由注入**：申请理由会出现在审批界面，若审批方是 LLM 就是注入面 | 理由做长度截断 + 去除指令性标记；审批 UI 里以纯文本渲染，不喂进 LLM 的指令位 |
| 10 | **身份自报**：请求体里的 `from` / `sender` / `reader` | 一律从鉴权结果取，请求体同名字段**忽略而非校验** |
| 11 | **token 时序侧信道** | `hmac.compare_digest`，不用 `==` |
| 12 | **权限永久化**：一次授予就永久有效 | 信任衰减，`stale` 自动降级（§7.4）（✅ 阶段 4：`why_not` 直接报 `stale` + `sweep_stale()` 物理降级） |

### 通用

- **默认拒绝**：`scopes` 为空 = 无任何权限，不是全部。`["*"]` 才代表全部
  （且只对 `admin` 之外的 scope 生效）。这点必须在文档和示例里写死，
  否则早晚有人搞反。
- **授权检查放在端点层**，会话层只调用注入的 guard，保持可测。
- **审计不可关闭**：关系变化一律留痕，没有"静默加好友"的路径。

---

## 14. 测试计划

按阶段落地的测试文件：

| 文件 | 覆盖 | 项数 |
|---|---|---|
| `tests/test_relations.py` | 成员 / 关系 / scope / 门禁 / 隐私 / 持久化 | 146 |
| `tests/test_delegation.py` | `delegate` 二级门禁 / 群聊边界 / 编排收口 / 归档解冻 / A2A 兼容 | 30 |
| `tests/test_discovery.py` | 距离 / 三档可见性 / 打分 / 发现 / 引荐 / 隐私 / HTTP 层 | 46 |
| `tests/test_autonomy.py` | 策略解析 / §6 判定顺序 / 自动同意 / 闭包 / 待办与审批 / 配额 / 信任衰减 / 缺口信号 / 巡航 / HTTP + RPC | 66 |

已实现部分（括号里是测试类名）：

**成员与关系**
- id 解析：有效 / 无效 / 缺前缀 / 空 token；`human:` 走成员，裸名走 agent（`TestNormalize`）
- 退化模式：无 `members.yaml` → `human:default`，`can()` 恒真，行为与 v0.3.0 一致（`TestModes`）
- 状态机：request → accept / reject / cancel；`rejected` 冷却期后可再申请（`TestStateMachine`）
- **双向同时申请 → 自动成为好友**（`TestMutualRequest`）
- 拉黑：单向、只有拉黑方能解除、被拉黑方申请被静默丢弃（**错误文案里不出现「拉黑」**）（`TestStateMachine`）
- `pair` 规范化：`(A,B)` 与 `(B,A)` 命中同一条记录（`TestPairNormalization`）

**scope 与门禁**
- 非好友发消息 → 被拒 + 系统消息指明原因（不静默）（`TestChatGate`）
- 好友但无 `delegate` → `chat` 通过、执行被拒（`TestScopeDefaults`）
- 单向授权：A 给 B `delegate`，B 未给 A → 反向调用被拒（**不对称用例**）（`TestScopeDefaults`）
- **群聊边界**：同群可 `chat`、不可 `delegate` / `artifact`；`invite` 缺失时拉人失败
  （`TestScopeDefaults` / `TestChatGate`）
- **权限上行闭包**：授予超过 `owned_scopes` → 拒绝，含 Graph 层与 HTTP 层各一条（`TestUpwardClosure`）
- owner 代理：owner 可代表自己的 agent 同意 / 查看；他人不可（`TestOwnerProxy` / `TestOwnerReadProxy`）

**边界与隐私**
- `discoverable: private` 不出现在他人搜索结果，但**仍可被指名申请**（`TestPrivacy`）
- 好友列表只有本人可读（`TestSocialApi`）
- `visibleTo` 过滤：A 不见 B 的会话（`TestChatGate` / `TestSecurityHoles`）
- 单聊不串号：`(owner, agent)` 两个成员拿到不同 `conversationId`（`TestChatGate`）
- SSE 鉴权：无 token → `401`；越权订阅 → `403`（`TestSecurityHoles`）
- `mark_read` / `sender` 不可伪造：body 同名字段被忽略（`TestSecurityHoles`）
- 审计链只返回与本人 / 本人 agent 相关的记录（`TestSocialApi`）

**持久化与兼容**
- `relations.json` 往返、损坏文件不阻塞启动、审计跨重启保留（`TestPersistence`）
- Agent Card 在门禁开启时声明 `x-social`，关闭时不声明（`TestSocialApi`）
- RPC `social/*` 方法：无 graph 时返回 `-32008` 且带 `hint`（`TestRpcSocial`）

回归：**原 212 项既有测试全过**（当前全套 **436 项**：212 原有 + 146 + 30 + 46 + 若干），
且 `members.yaml` 不存在时行为不变。

---

## 15. 未决问题

1. **群聊 scope 边界是否太松？** 当前取「同群自动 `chat`」，因为"群里也必须互为好友"
   会让 3 人以上群几乎不可用。但如果将来有高敏感的 agent（法务、财务），
   可能需要一个 per-member 的 `groupChat: deny` 开关。
2. **agent 拒绝好友申请要不要给理由？** 给理由体验好，但可能泄露内部策略
   （比如"我只接受 verified 标签"就暴露了绕过路径）。当前倾向：只给固定措辞，
   具体原因只写进自己的审计。
3. **`maxFriends` 默认取多少？** 50 是拍的。太小圈层扩不开，太大失去意义。
   需要真实使用数据来定。
4. **自主决策可否回放？** `decision` 字段现在只存一句话（`policy: detail`）。
   要不要存完整的打分明细（各维度分值），以便事后审计"为什么选了它"？
   信息更全但存储膨胀。**阶段 4 的取舍**：审计里存 `policy` + `detail` +
   授予的 scopes；打分明细只在 `discover()` 的返回值里现算现给，不落盘。
5. **owner 消失怎么办？** 删掉 `human:seafish` 后，`agent:codex` 的 `owner` 悬空，
   它的自主交友应当自动关闭、还是转给另一个继承人？
6. **跨 Hub 联邦与 id 命名空间**：`agent:codex` 在联邦下会与别的 Hub 撞名。
   是否现在就把 id 改成 `hub-a/agent:codex`？这是破坏性变更，越晚改代价越大——
   **建议在阶段 1 就定下来**。
7. **`rejected` 的冷却期多长？** 太短等于鼓励骚扰，太长会让正常的"改主意"受阻。
8. **申请理由的注入面**（§13 #9）：如果将来做"agent 自动审批 agent 的申请"，
   申请理由就真的进了 LLM 的上下文。届时要专门设计隔离，现在先留伏笔。
   阶段 4 已做的是**最低限度**：`sanitize_reason()` 截断 + 去指令性标记，
   审批界面以纯文本渲染。真正的隔离要等"LLM 审批"这个功能真的出现。

### 15.1 实现时拍板的（原稿没写，但必须留痕）

1. **自主同意永不授出执行类权限。** 原稿只写「请求的 scope ⊆ `accept.maxScope`
   → 自动同意」，没说 `maxScope` 里能不能写 `delegate`。实现时直接把它削掉
   （`AutonomyPolicy.auto_scope`）。理由：执行类 = 让对方 agent 替你干活，
   由一次冷淡的自动同意产生，等于把「越界必须人审」架空。想要 `delegate`
   只能人工 `grant`。
2. **待办挂到 `owner`（人），不挂 agent。** 原稿说「挂到 owner 的待办」，
   实现时把「owner」严格解释为**人**（`_approval_owner` 做上溯）。否则
   agent 名下的待办它能自己批准，整条约束失效。
3. **`skip` / `ignore` / `pending` 三态要分清。** 原稿的「拒绝」在执行时
   拆成三种语义：`ignore` = 本层不介入（自主没开，退回人工流程）；
   `skip` = 本轮不做（配额/阈值不满足，但对方以后仍可正常申请）；
   `pending` = 转人审。把三者混成一个「拒绝」，要么会静默砍掉正常申请，
   要么会让「策略没开」看起来像「被拒绝」。
4. **缺口的主动申请只**请求**对话类权限。** gate 在 `decide_send` 的 `ask`
   上同样削掉执行类——不能靠「反正对方也不会自动给」来兜底。

---

## 附：与现有约定的关系

- **异构纪律**（`MEMORY.md` 约定 1）：社交层不感知 agent 类型，只认 `member_id`。
  好友判定不进入任何 `BaseAdapter`。
- **事件是唯一事实来源**（约定 2）：关系变化（申请/同意/拒绝/拉黑）也发事件，
  控制台可实时反映；审计与事件同源，不写两套。
- **会话是 A2A 之上的一层**（约定 8）：社交层在会话层**之上**——
  `social.py` 不做鉴权，只调用注入的 `guard`。这保住了"会话层可进程内复用、
  零依赖单测"的性质。
- **prompt 只作为独立 argv 元素**（约定 3）：申请理由若要进 prompt，同样不拼 shell。

**一句话总结这次反转**：旧稿把 agent 当资源，于是只需要一张授权表；
新稿把 agent 当邻居，于是需要一张**图**——有边、有方向、有生命周期、有信任度，
而且**两边都有人（或 agent）在决定**。
