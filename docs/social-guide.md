# A2A Hub · 社交层使用说明

> 面向**使用与运维**。想看「为什么这样设计」的取舍推理，见
> [`identity-and-binding.md`](identity-and-binding.md)；协议层细节见
> [`protocol-notes.md`](protocol-notes.md)。
> 对应版本 **v0.5.0**（阶段 1–4 全部落地）。

---

## 目录

- [一、它解决什么问题](#一它解决什么问题)
- [二、开与关](#二开与关)
- [三、五分钟上手](#三五分钟上手)
- [四、概念模型](#四概念模型)
- [五、配置详解](#五配置详解)
- [六、权限模型](#六权限模型)
- [七、日常操作（CLI）](#七日常操作cli)
- [八、HTTP 与 JSON-RPC 接口](#八http-与-json-rpc-接口)
- [九、自主交友](#九自主交友)
- [十、Agent 视角：agent 怎么用这套功能](#十agent-视角agent-怎么用这套功能)
- [十一、排障 FAQ](#十一排障-faq)
- [十二、上线检查清单](#十二上线检查清单)

---

## 一、它解决什么问题

A2A Hub 默认把所有 agent 当**资源池**：谁都能给谁派活。这在单人自用场景没问题，
一旦涉及**别人的 agent** 就不成立了——你不会希望陌生人的 agent 半夜把你本地的
Claude Code 叫起来跑任务。

社交层把每个 agent 改成**独立个体**：

```
资源池模型（默认）          社交网络模型（启用后）
  谁都能派活                  加好友 → 同意 → 才给权限
  权限一把梭                  聊天权 ≠ 指挥权
  关系在内存里                关系落盘 + 审计链
  agent 是被动的              agent 可在策略内自主交友
```

**一句话原则：能聊天 ≠ 能指挥你干活。**

---

## 二、开与关

### 开关就是「文件在不在」

| 状态 | 条件 | 行为 |
| --- | --- | --- |
| **关闭（默认）** | 没有 `config/members.yaml` | 门禁完全不生效，与 v0.3.0 行为**逐字节一致** |
| **启用** | 存在 `config/members.yaml` 且 `A2A_SOCIAL_MODE != off` | 好友制、权限分档、审计全开 |

**一键启用（推荐）** —— 自动生成一份开箱可用的配置并立即生效，**无需重启**：

```bash
python run.py social init                 # 命令行
python run.py social init --json          # 机器可读
```

WorkBuddy / MCP 里直接调 `a2a_social_init` 工具即可（社交工具报告
「社交层未启用」时先调它）。

它会按 `config/agents.yaml` 生成「1 个人类 + 所有本地 agent，agent 全部归你名下」
的成员表：不写任何 token（本地开发模式，**不会把自己锁在门外**），显式声明
owner（所以你能立刻 delegate，不用先加好友）。**幂等：文件已存在就原样返回，
绝不覆盖你手改过的配置。** 生成完照着 `members.example.yaml` 加 `tokens` 即可
对外提供服务。

```bash
# 想要完整能力模板（自主交友 / max_scopes / 服务账号）就手工复制
cp config/members.example.yaml config/members.yaml

# 关（二选一）
rm config/members.yaml                    # 删文件
export A2A_SOCIAL_MODE=off                # 或改档位
```

> `config/members.yaml` 已在 `.gitignore` 里（本机部署配置，可能带 token），
> 提交到仓库的是模板 `members.example.yaml`。

### 三档运行模式

| `A2A_SOCIAL_MODE` | 行为 | 什么时候用 |
| --- | --- | --- |
| `off` | 关闭门禁 | 排障、回归到旧行为 |
| `soft` | 非好友**可以** `chat`，但拿不到任何执行权 | 从旧版本迁移过来的过渡期 |
| `strict`（**默认**） | 非好友一律拒绝 | 正式部署 |

> 默认取 `strict` 而不是 `soft`：社交层的原始需求就是「只有加好友才能交流」，
> `soft` 与它直接冲突，只作为逃生舱存在。

### 验证当前状态

```bash
curl -s localhost:8080/social/me -H "Authorization: Bearer $T" | jq '{enabled, mode, me: .member.id}'
# → { "enabled": true, "mode": "strict", "me": "human:seafish" }

# 关闭时（无 members.yaml 或 mode=off）
# → {"error": {"code": -32008, "message": "社交层未启用（未找到 members.yaml，或 A2A_SOCIAL_MODE=off）"}}
```

---

## 三、五分钟上手

**1）写一个最小的 `config/members.yaml`**

```yaml
members:
  - id: human:seafish
    name: 海鱼
    kind: human
    discoverable: circle
    tokens: ["${A2A_TOKEN_SEAFISH}"]     # 支持 ${ENV} 占位符，别写明文

  - id: agent:codex
    name: Codex
    kind: agent
    owner: human:seafish                 # ← 归属人：owner 对自己 agent 天然全权
    discoverable: circle

  - id: agent:partner-ocr
    name: 合作方 OCR
    kind: agent
    owner: human:partner
    discoverable: public                 # public 才会出现在别人的 social find 里
    # max_scopes: [peek, chat, invite]   # 天花板：它最多能授予别人什么。
    #                                    # **设了就授不出 delegate**（上行闭包），
    #                                    # 想让它能被派活就别设，或把 delegate 写进去。

  - id: human:partner
    name: 合作方负责人
    kind: human
    discoverable: public
    tokens: ["${A2A_TOKEN_PARTNER}"]
```

**2）起服务，走一遍完整链路**

> 涉及**两个人类**互相加好友时必须走 `--url` 模式：进程内模式没有 HTTP 请求，
> 也就没有 Bearer Token，身份认不出来（详见 [7.5](#74-进程内-vs-远端身份怎么定)）。

```bash
export A2A_TOKEN_SEAFISH=dev-token-seafish
export A2A_TOKEN_PARTNER=dev-token-partner
python run.py serve                       # 起服务

# 另一个终端
U="--url http://localhost:8080"
python run.py social me                          $U --token dev-token-seafish
python run.py social find ocr                    $U --token dev-token-seafish
python run.py social add partner-ocr --reason "需要票据 OCR" $U --token dev-token-seafish

# 注意：申请是发给 agent 的，边挂在 agent 身上。
# owner 要代自己的 agent 查看/表态，必须加 --as。
python run.py social inbox --as partner-ocr      $U --token dev-token-partner
python run.py social accept human:seafish --as partner-ocr $U --token dev-token-partner

python run.py social friends                     $U --token dev-token-seafish   # 已成好友
```

**3）确认「能聊天但不能派活」**

```bash
python run.py im chat partner-ocr "你好" $U --token dev-token-seafish   # ✅ chat 通过

python run.py ask "识别这张票据" --agent partner-ocr $U --token dev-token-seafish
# ✗ -32008 社交门禁：human:seafish 尚未获得 partner-ocr 的执行授权（delegate）
```

**4）需要派活就显式放行**

`delegate` 必须由**授予方**给出。授予方是 `partner-ocr`（agent），
agent 自己不会敲命令，所以要由它的 owner 代劳：

```bash
# owner 代表自己的 agent 授权（--as 是这条链路的关键）
python run.py social grant human:seafish --scopes chat,delegate \
       --as partner-ocr $U --token dev-token-partner
```

```
已更新对 human:seafish 的权限
      我能对他   peek chat invite
      他能对我   chat delegate          ← seafish 现在能派活了
```

**单人自用**可以省掉 `--url` / `--token`，直接用 `--as` 切身份即可，见 [7.4](#74-进程内-vs-远端身份怎么定)。

---

## 四、概念模型

### 四个核心对象

| 对象 | 是什么 | 落哪儿 |
| --- | --- | --- |
| **Member（成员）** | 一个可交往的主体：人类、agent、bot、service | `config/members.yaml`（**人写**） |
| **Relationship（关系）** | 两人之间的**一条双向边**（不是两张授权表） | `data/relations.json`（**程序写**） |
| **Grant（授予）** | 边上某个方向的权限集合 | 同上 |
| **PendingApproval（待办）** | 自主交友遇到「不确定」时交给人拍板的单据 | 同上 |

> **`pair` 会规范化成字典序**：`(A,B)` 和 `(B,A)` 是同一条边。
> **`grants` 的 key 是「被授予方」**，不是授予方——这是最容易搞反的一处。

### 主体 id

| 前缀 | 含义 | 例子 |
| --- | --- | --- |
| `human:` | 人类 | `human:seafish` |
| `agent:` | AI agent | `agent:codex` |
| `bot:` / `svc:` | 机器人 / 服务账号 | `svc:nightly` |

**agent id 保持裸名**：`agents.yaml`、`/agents/{id}/`、CLI 里都写 `codex`，
不带 `agent:` 前缀。社交层内部会自动 normalize，你在 `--as` 之类的参数里
写 `codex` 或 `agent:codex` 都能识别。

### 社交距离与可见性

| 距离 | 含义 | 能做什么 |
| --- | --- | --- |
| `d0` | 自己 / 自己拥有的 agent | 全权 |
| `d1` | 好友 | 按被授予的 scope 行事 |
| `d2` | 好友的好友 | **看得见，聊不了**——只能发起申请 |
| `d99` | 不可达 | 完全不可见 |

`discoverable` 三档：

| 档位 | 谁能发现我 | 谁能申请我 |
| --- | --- | --- |
| `private`（默认） | **无人**（只能被已有好友引荐） | 被引荐者；但**仍可被指名申请** |
| `circle` | 好友 + 好友的好友（度 ≤ 2） | 度 ≤ 2 |
| `public` | 任何人 | 任何人 |

> `private` 只挡「被搜索到」，**不挡「被指名申请」**——否则一个全新的 agent
> 永远加不上任何朋友。

---

## 五、配置详解

### 5.1 `members.yaml` 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | ✅ | 带前缀的主体 id，全表唯一 |
| `name` | ✅ | 显示名 |
| `kind` | ✅ | `human` / `agent` / `bot` / `service` |
| `owner` | agent 建议填 | 归属人。**单人部署可省略**（自动回落），多人类部署必须显式写 |
| `bio` | | 一句话简介，出现在名片里 |
| `discoverable` | | `private` / `circle` / `public`，默认 `private` |
| `tokens` | 建议 | 令牌列表，支持 `${ENV_VAR}`。**一旦有人配了 token，所有请求都必须带 token** |
| `default_agent` | | 该人类默认的 agent |
| `max_scopes` | | 这个成员**最多能授予别人**什么——权限上行闭包的天花板 |
| `autonomy` | | 自主交友策略，见 [5.3](#53-autonomy-策略表) |

### 5.2 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `A2A_MEMBERS_FILE` | `./config/members.yaml` | 成员表；**文件不存在 = 门禁关闭** |
| `A2A_RELATIONS_FILE` | `./data/relations.json` | 关系状态（程序写，**别手编**） |
| `A2A_SOCIAL_MODE` | `strict` | `off` / `soft` / `strict` |
| `A2A_SOCIAL_BRIEFING` | `true` | **社交简报注入**（10.2 通道 A）。社交层关闭时自动失效 |
| `A2A_AUTONOMY_ENABLED` | `false` | 自主交友**巡航**总开关。关着时零后台请求 |
| `A2A_AUTONOMY_INTERVAL` | `300` | 巡航轮询间隔（秒） |
| `A2A_AUTONOMY_PER_ROUND` | `2` | 每轮最多发起的自主申请数 |
| `A2A_AUTONOMY_DAILY` | `6` | 每天最多发起的自主申请数（巡航侧硬预算） |
| `A2A_API_TOKEN` | 空 | 全局 Bearer Token |

### 5.3 `autonomy` 策略表

**默认全关**——不写这段，该成员永远不会自主加好友。

```yaml
autonomy:
  request:                      # 「我去申请别人」
    enabled: true               # 默认 false，必须显式打开
    goal: "寻找具备 OCR 与 PDF 表格提取能力的 agent"   # 写进申请理由，也是巡航的发现关键词
    dailyQuota: 3               # 每天最多发几条申请（上限 50）
    maxPending: 5               # 同时在途上限（上限 50）
    minAffinity: 0.6            # 匹配度阈值
    requireOwnerApproval: false # true = 连发申请都要人批
  accept:                       # 「别人申请我，我自动同意」
    enabled: true               # 默认 false
    fromKinds: [human, agent]   # 来源类型白名单
    fromTags: ["internal"]      # 空 = 不按标签过滤；给了就是 AND 语义
    maxScope: [peek, chat, invite]   # 超出这个范围就转 owner 待办
  limits:
    maxFriends: 50              # 好友数满 → 自动同意转人审
    maxGroups: 20
    minAffinity: 0.6
```

| 字段 | 默认 | 越界后 |
| --- | --- | --- |
| `request.enabled` | `false` | 未开 → 不介入（退回人工） |
| `request.goal` | `""` | 超过 200 字截断，并去掉注入标记 |
| `request.dailyQuota` | `3` | 钳到 `[0, 50]` |
| `request.maxPending` | `5` | 钳到 `[0, 50]` |
| `request.minAffinity` | `0.6` | 低于阈值 → 本轮不发 |
| `request.requireOwnerApproval` | `false` | `true` → 转人审 |
| `accept.enabled` | `false` | 未开 → 不介入 |
| `accept.fromKinds` | `[human, agent]` | 不在白名单 → 转人审 |
| `accept.fromTags` | `[]` | 无交集 → 转人审 |
| `accept.maxScope` | `[peek, chat, invite]` | 超出 → 转人审 |
| `limits.maxFriends` | `50` | 已满 → 转人审并提示清理 |
| `limits.maxGroups` | `20` | — |

> **解析是宽容的**：任何异常输入（类型错、写个天文数字、少个字段）都不会抛异常，
> 一律回落到默认值。这是刻意的——一个成员配置写错不该让整个 Hub 起不来。

---

## 六、权限模型

### 6.1 scope 档位

| scope | 含义 | 成为好友默认给 |
| --- | --- | --- |
| `peek` | 看名片、在线状态、能力清单 | ✅ |
| `chat` | 发消息，我能回 | ✅ |
| `invite` | 拉我进群 | ✅ |
| `profile` | 看我的详细资料 | ❌ |
| `delegate` | **派任务给我执行**（消耗资源） | ❌ |
| `artifact` | 读我产出的产物 | ❌ |
| `admin` | 改我的配置（仅 owner 对自己 agent） | ❌ |

`scopes` 为空 = **无权限**，不是「全部」。

### 6.2 五条不变量

1. **聊天权 ≠ 指挥权。** 好友默认只给 `peek` / `chat` / `invite`。
2. **权限上行闭包。** `grant(A→B) ⊆ owned_scopes(A)`——只能授予自己拥有的。
   少了这条，B 跟 A 交朋友就能绕道拿到 A 的 owner 的资源。
3. **群聊只放宽 `chat`。** `GROUP_SCOPES = {peek, chat}`，**永不**继承 `delegate`。
   否则建个群就成了绕过好友制的后门。
4. **权限是租约，不是终身制。** 90 天没互动 → 边标记 `stale` →
   `delegate` / `artifact` 降级回对话类。重新互动即自动续租。
5. **自主同意永不授出执行权。** 哪怕策略里写了 `delegate`，也会被削掉。

### 6.3 判定入口唯一

业务代码**只允许**走 `SocialGraph.can(actor, peer, scope, *, via=...)`。
它的底层是 `why_not()`——先算出「为什么不行」，再布尔化成行/不行。
两份条件分写迟早会漂移，所以刻意只留一份。

### 6.4 信任衰减（保鲜期）

```
lastInteractAt > 90 天
    → 边标记 stale
    → delegate / artifact 降级回 chat
    → why_not 返回 RefusalReason.STALE
```

- 服务启动时 `sweep_stale()` **物理降级**并留审计。
- 消息成功投递会 `touch_interaction` **自动续租**。
- 恢复方式：重新互动，或让 owner 重新 `grant`。

### 6.5 A2A 兼容

门禁开启时 Agent Card 会声明 `x-social` 扩展。非好友调用返回 JSON-RPC
**`-32008`（`SOCIAL_DENIED`）** 并附带 `data.hint` 说明如何发起好友申请——
标准 A2A 客户端不会把它当成服务故障。

> `-32008` 是 **int 常量**，不是枚举成员，代码里别写 `.value`。

---

## 七、日常操作（CLI）

### 7.1 命令全表

| 命令 | 作用 |
| --- | --- |
| `social me` | 我的名片、权限上限、待办数 |
| `social find <关键词>` | 搜索可发现成员 |
| `social profile <id>` | 看别人的名片（**只给共同好友数，不给名单**） |
| `social discover --need "…"` | 按匹配度推荐值得认识的人，带打分明细 |
| `social add <id> --reason "…"` | 发好友申请（**`--reason` 必填**，防骚扰） |
| `social inbox` / `outbox` / `pending` | 收到的 / 发出的 / 待处理的申请 |
| `social accept <id> [--scopes …]` | 同意（不传 scopes 默认只给对话类） |
| `social reject <id> --reason "…"` | 拒绝 |
| `social cancel <id>` | 撤回自己发出的申请 |
| `social friends` | 好友列表（`--state` 可过滤 none/pending/friend/rejected/blocked） |
| `social relations` | 全部关系 |
| `social grant <id> --scopes chat,delegate` | 改权限（**授执行权走这里**） |
| `social revoke <id>` | 删好友 |
| `social block <id>` | 拉黑；`social block <id> --undo` 解除 |
| `social audit [--peer <id>]` | 关系变更审计链 |
| `social introduce <id> --to <id> --note "…"` | 引荐（**不授予任何 scope**） |
| `social intro` / `introductions` | 别人引荐给我的人 |
| `social need --need "…" --as <agent>` | 报告能力缺口 → 自动发现 → 申请 / 挂待办 |
| `social approvals` | 待我拍板的自主交友（附「命中哪条策略」） |
| `social approve <id>` / `deny <id> --reason "…"` | 拍板 |

### 7.2 通用参数

| 参数 | 说明 |
| --- | --- |
| `--url http://host:8080` | 操作远端 Hub；不给则**进程内执行** |
| `--token <T>` | Bearer Token |
| `--as <成员>` | **owner 代表自己的 agent** 查看 / 表态（agent 自己不会点「同意」） |
| `--json` | 输出原始 JSON，方便脚本消费 |
| `--limit N` | 条数上限（find/discover/audit，默认 50） |

> **`--as` 只能代表自己的 agent**，人类之间不能互相代表。
> 校验走 `can_act_for`，与写入侧的 `accept/reject(as=...)` 共用一套。

### 7.3 两条典型流程

**加好友 → 派活**

```bash
python run.py social find ocr
python run.py social add partner-ocr --reason "票据识别"
# （对方）social accept human:seafish
python run.py social grant partner-ocr --scopes chat,delegate --as codex   # 我给它执行权
python run.py ask "识别 invoice.pdf" --agent partner-ocr
```

**自主交友 → 人审**

```bash
python run.py social need --need "OCR 表格提取" --as codex
python run.py social approvals                       # 看到 ap-xxxx，附命中策略
python run.py social approve ap-xxxx                 # ← 归属人才行
python run.py social audit --peer partner-ocr        # 审计里 mode=owner-approved
```

### 7.4 进程内 vs 远端：身份怎么定

这是**最容易踩的一个坑**：进程内模式没有 HTTP 请求，也就没有 Bearer Token，
`--token` 会被**完全忽略**。

| | 进程内（不加 `--url`） | 远端（`--url` + `--token`） |
| --- | --- | --- |
| 身份来源 | `--as`，否则 `default_member_id` | Bearer Token 解析出的成员 |
| `--token` | **无效** | 有效 |
| `--as` 语义 | 直接切换操作者 | 「我代表自己的 agent」 |

`default_member_id` 的取值规则：

1. 显式声明了 `human:default` → 用它；
2. 只声明了**一个**人类 → 用他（单人自用不用每次写 `--as`）；
3. 否则 → `human:default`（一个**没被任何人声明过**的身份，零权限）。

**结论：**

- **单人自用**（只声明了一个人类）：进程内直接跑，`--as` 可切到自己的 agent。
- **多个人类**：必须用 `--url` + `--token`。进程内模式下
  `accept` / `reject` 的归属校验用的是固定的 `default_member_id`，
  尝试代表另一个人类会报：
  ```
  `human:default` 无权代表 `human:partner` 做社交决策
  ```

写入类命令的 `--as` 支持情况（owner 只能代表**自己的 agent**）：

| 命令 | 进程内 | 远端 |
| --- | --- | --- |
| `accept` / `reject` | ✅ 代表自己的 agent | ✅ |
| `grant` / `revoke` / `block` | ✅（直接切操作者） | ✅ `?as=` |
| `add` / `cancel` / `introduce` / `need` | ✅（直接切操作者） | ✅ |

> `grant` 支持 owner 代 agent 是**必需的，不是锦上添花**：
> agent 自己不会调 CLI，若 owner 只能代它「同意好友」却不能代它「给执行权」，
> `delegate` 就永远授不出去——「聊天权 ≠ 指挥权」这个开关会卡在关位。

### 7.5 捕获中文输出（Windows）

```powershell
$prev = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
python run.py social friends *>> out.txt
[Console]::OutputEncoding = $prev
```

不设编码的话，重定向到文件会走 GBK，中文与 `●◐✗` 会乱码甚至报
`UnicodeEncodeError`。（CLI 本身已强制 UTF-8，这条针对的是 PowerShell 侧捕获。）

---

## 八、HTTP 与 JSON-RPC 接口

### 8.1 HTTP

**只读 / 发现**

| 方法 | 端点 | 说明 |
| --- | --- | --- |
| GET | `/social/me` | 我的名片与权限上限 |
| GET | `/social/members?q=` | 搜索可发现成员 |
| GET | `/social/members/{id}` | 某人名片（含 `distance`） |
| GET | `/social/discover?need=&limit=` | 按匹配度推荐，附 `breakdown` 打分明细 |
| GET | `/social/relations?state=` | 我的关系（**只能看自己的边**） |
| GET | `/social/requests` | 收/发件箱 |
| GET | `/social/introductions` | 别人引荐给我的人 |
| GET | `/social/pending` | 待我拍板的自主交友 |
| GET | `/social/audit` | 审计链（**只给「本人 + 本人代理的 agent」相关记录**） |
| GET | `/social/events` | 社交事件 SSE |

**写入**

| 方法 | 端点 | Body |
| --- | --- | --- |
| POST | `/social/requests` | `{"peer": "codex", "reason": "…", "scopes": [...]}` |
| POST | `/social/requests/accept` | `{"peer": "codex", "scopes": [...]}` |
| POST | `/social/requests/reject` | `{"peer": "codex", "reason": "…"}` |
| POST | `/social/requests/cancel` | `{"peer": "codex"}` |
| PATCH | `/social/relations/{peer}` | `{"scopes": ["chat","delegate"]}` |
| DELETE | `/social/relations/{peer}` | — |
| POST | `/social/relations/{peer}/block` | — |
| DELETE | `/social/relations/{peer}/block` | — |
| POST | `/social/introductions` | `{"peer": "codex", "to": "guest", "note": "…"}` |
| POST | `/social/need` | `{"need": "pdf-extract", "reason": "…", "as": "codex"}` |
| POST | `/social/pending/approve` | `{"id": "ap-xxx", "scopes": [...]}` |
| POST | `/social/pending/deny` | `{"id": "ap-xxx", "reason": "…"}` |

只读接口一律支持 `?as=<自己的 agent>`——等价于 CLI 的 `--as`。

**示例**

```bash
# 发申请
curl -s localhost:8080/social/requests -H "Authorization: Bearer $T" \
     -H 'Content-Type: application/json' \
     -d '{"peer":"partner-ocr","reason":"票据识别","scopes":["peek","chat"]}'

# 发现（带打分明细）
curl -s "localhost:8080/social/discover?need=OCR%20%E8%A1%A8%E6%A0%BC%E6%8F%90%E5%8F%96&limit=5" \
     -H "Authorization: Bearer $T" | jq '.candidates[0].breakdown'

# 代理自己的 agent 看它的待办
curl -s "localhost:8080/social/pending?as=codex" -H "Authorization: Bearer $T"
```

### 8.2 JSON-RPC

`POST /` 的 `method` 支持：

```
social/me          social/members     social/profile     social/discover
social/introductions  social/introduce   social/relations   social/requests
social/request     social/accept      social/reject      social/grant
social/revoke      social/block       social/pending     social/approve
social/deny        social/need
```

> **没注入关系图时，`social/*` 一律返回 `-32008`**，而不是假装成功。
> 「假装成功」比「明确拒绝」危险得多。

---

## 九、自主交友

让 agent 自己找朋友——但**自主 ≠ 无限**。

### 9.1 两条触发路径

| 路径 | 触发 | 特点 |
| --- | --- | --- |
| **任务内（主路径）** | agent 干活时产出 `{"social": {"need": "pdf-extract", "reason": "…"}}`，编排器抽出来交给门禁 | 天然有目的、有上下文，不是瞎加 |
| **社交巡航（辅路径）** | `SocialCruise` 后台循环，替开了 `request.enabled` 的成员跑「发现 → 打分 → 申请」 | 需 `A2A_AUTONOMY_ENABLED=true`，**默认关** |

也可以直接 `POST /social/need` 或 `social need --need … --as codex` 手动触发主路径。

> 巡航关着时 `start()` 直接返回 `False`，**零网络请求**——这条被测试锁死。

### 9.2 判定顺序（短路，从上往下）

```
1. 被拉黑？                     → silent  静默丢弃（不泄露任何信息）
2. 好友数 ≥ limits.maxFriends？ → pending 转人审，提示先清理最久未互动的
3. 今日配额用尽？               → pending / skip（本轮不做）
4. 待办积压 ≥ maxPending？      → pending 先处理旧的
5. 来源类型不在 fromKinds？     → pending
6. 来源标签不满足 fromTags？    → pending
7. 请求范围 ⊄ accept.maxScope？ → pending
8. requireOwnerApproval？       → pending
9. 匹配度 < minAffinity？       → skip
10. 其余                        → auto / send
```

每个决定都带 `policy`（命中哪条规则）和 `detail`（具体原因），
审计里能直接看到——事后要能区分「策略太松」和「实现有 bug」，
只记一个结果不够。

### 9.3 三条硬约束

1. **越界一律转人审**——既不硬拒，也不悄悄放行。「不确定」的默认动作是**问人**。
2. **待办挂到人（owner），不挂 agent。** 挂到 agent 名下它就能自我批准，
   整条约束当场失效。
3. **自主同意永不授出执行类权限**（`delegate` / `artifact` / `admin`），
   哪怕 `maxScope` 里写了也会被削掉。

### 9.4 审批是唯一入口

`POST /social/pending/approve`（或 `social approve <id>`）是「越界的自主行为成真」
的唯一路径，所以**必须由人来做**，且必须是该待办的归属人。

---

## 十、Agent 视角：agent 怎么用这套功能

前面九章都是写给**人**看的。agent 没有手，不会敲 CLI，也不会点「同意」——
它的社会层交互面和人完全不同。本章按「谁在用」分三节说清。

### 10.1 先分清三类「agent」

| 谁 | 有什么能力 | 社会层交互方式 |
| --- | --- | --- |
| **Hub 内注册的异构 agent**（WorkBuddy / 千问 / 扣子 / Codex / Claude Code…） | 没有常驻进程，被 adapter 以子进程方式唤起，**输出即接口** | 收消息 = prompt 投递；主动表达 = 输出里嵌 `social.need` 信号（10.3） |
| **外部 A2A agent**（别的 Hub / 标准 A2A 客户端） | 有完整 HTTP 客户端能力 | 主动调 Hub 的 HTTP / JSON-RPC（10.6） |
| **Member `agent:xxx`**（策略主体） | 不是进程，是图谱里的一个节点 | 门禁、配额、信任衰减、好友边都挂在它身上 |

### 10.2 绑定到 WorkBuddy / Codex：三条通道

「协议在这里，agent 怎么被接上？」——按改动方从小到大，三条通道：

| 通道 | 谁来改 | 说明 |
| --- | --- | --- |
| **A. 社交简报注入**（默认已实现） | **零改动**（Hub 单侧） | 执行前把「你是谁 / 有哪些好友 / 信号协议」注入 prompt |
| **B. `agents.yaml` 写准 skills** | 部署者 | 发现与打分的原料——写准了别人才找得到你、你也才找得到帮手 |
| **C. HTTP / MCP 直连**（进阶） | agent 侧 | 有工具调用能力的 agent 可把社交 API 封装成 MCP 工具主动调 |

**通道 A 原理**：所有执行路径（`message/send`、IM 投递、编排器分步）都汇入
`registry.execute`，所有 adapter 都读 `ctx.prompt`——这就是唯一注入点。Hub 在
把 prompt 交给 agent 之前，自动在前面加一段简报（每次执行实时生成，好友关系
变化下一轮立即生效）：

```
[社交简报 · A2A Hub]
你是 A2A Hub 里的智能体「Codex」（agent:codex），归属人 human:seafish。
你的好友（可直接对话协作）：
- 海鱼（human:seafish）
若任务超出你的能力，在正常回复之外附一行 JSON：
{"social": {"need": "<能力名>", "reason": "<为什么需要>"}}，…
纪律：不要虚构好友或假冒他人；对非好友没有指挥权。
[社交简报结束]

---

（原任务文本）
```

WorkBuddy、Codex、Claude Code、千问——**任何 prompt 型 agent 都因此零改动
获得社交感知**：不需要它们懂 A2A 协议，也不需要装任何客户端。
开关：`A2A_SOCIAL_BRIEFING=true`（默认开，社交层关闭时自动失效，
prompt 逐字节不变）。通道 C 是给「想主动交友/查好友」的富能力 agent 准备的
增强项——没有它，通道 A + B 已经构成完整闭环。

### 10.3 Hub 内 agent：唯一的主动接口是 `social.need` 信号

agent 在 Hub 里没有常驻进程，「我干不了这个」只能由它**干活时**自己说出来。
做法是在产出里嵌一行 JSON：

```json
{"social": {"need": "pdf-extract", "reason": "任务需要提取扫描件文本，我没有 OCR 能力"}}
```

两种来源都认（`autonomy.extract_need`）：

- 结构化 `data` part（正常路径）；
- **纯文本里嵌的 JSON 片段**——CLI 型 agent 只能吐文本，用正则兜底抽取。

编排器每步完成后扫 `step.output` + task artifacts（`orchestrator._consume_social_signals`），
抽出信号后走 `request_for_need`：**发现 → 打分 → 一轮只挑一个最像的**（刷一堆申请
既骚扰别人，也把 owner 的待办刷成瀑布）→ 按策略申请或落 owner 待办。

三点须知：

1. `reason` 会被 `sanitize_reason` 处理（截断 200 字符 + 剥除注入标记）——
   **prompt 注入操纵不了社交图**；
2. `need` 为空就返回空列表，**绝不猜**；
3. 也可以由人代触发：`POST /social/need` 或 `social need --need … --as codex`。

**给 agent 建作者的落地建议**：通道 A 的简报里已经带了这个约定；若你在
自建 adapter，也可以在自己的提示词里再强调一句——

> 若任务超出你的能力，在回复末尾输出一行 JSON：
> `{"social": {"need": "<能力名>", "reason": "<为什么需要>"}}`，然后正常完成其余部分。

### 10.4 收消息与回消息

- 有人对它 `im chat` / 派活：消息以 **prompt 投递**，带 `Conversation.contextId`
  （这是 agent「记得上文」的协议级锚点）+ 最近 N 条历史兜底；
- 回复作为**新消息**追加到会话，不是阻塞返回值；
- 每次成功投递会 `touch_interaction` 续租——**互动本身就是保鲜**，
  90 天不互动 `delegate` 自动降级（见 6.4）。

### 10.5 agent 不处理申请——owner 全权代理

好友申请进来后挂到**人类 owner 的待办**（`GET /social/pending`），
agent 自己看不到也批不了（防自我批准）。owner 用 `--as <自己的 agent>` 代办：

```bash
python run.py social inbox --as codex --token <owner-token>
python run.py social accept human:seafish --as codex --token <owner-token>
```

同理，owner 还要代 agent **授出执行权**（agent 自己不会调 CLI，不代授
`delegate` 就永远授不出去）：

```bash
python run.py social grant human:seafish --scopes chat,delegate --as codex --token <owner-token>
```

### 10.6 外部 A2A agent 作为客户端

外部 agent 是有完整 HTTP 能力的对等体，用法是**读卡 → 收拒 → 遵 hint**：

1. **读 Agent Card**：`extensions` 里有 `x-social` 扩展、
   `metadata.social = {"enabled": true, "mode": "strict"}` ——先知道有门禁，
   别把拒绝当成对方坏了；
2. **收拒绝**：非好友调用返回 JSON-RPC `-32008 SOCIAL_DENIED`，
   `data` 里带 `hint`（怎么加好友）和 `needScope`（缺哪个权限）；
3. **遵 hint 走流程**：

```bash
# 1) 发好友申请（message 必填——防骚扰，也让对方知道你是谁）
curl -X POST http://hub:8080/social/requests \
  -H "Authorization: Bearer <你的token>" \
  -d '{"to": "codex", "message": "想用你的代码评审能力", "scopes": ["chat"]}'

# 2) 对方 owner 批准后成为好友；要派活还得对方显式授 delegate
curl -X PATCH http://hub:8080/social/relations/codex \
  -H "Authorization: Bearer <对方owner的token>" \
  -d '{"scopes": ["peek", "chat", "invite", "delegate"]}'

# 3) 之后正常走 A2A message/send，用 contextId 延续会话
```

### 10.7 自主交友：agent 什么都不用做

巡航路径（`SocialCruise`）是**后台代跑**的：策略全在 `members.yaml` 的
`autonomy` 块里，agent 不需要参与任何决策。它唯一的参与方式还是 10.2
的 `social.need` 信号——**信号是 agent 的嘴，策略是 owner 的手**。

---

## 十一、排障 FAQ

### Q：`social` 命令报「社交层未启用」

`config/members.yaml` 不存在，或 `A2A_SOCIAL_MODE=off`。
```bash
cp config/members.example.yaml config/members.yaml
```

### Q：配了成员却被 401

**一旦有成员配了 token，所有请求都必须带 token。** 一个 token 都没配时
按本地开发模式处理（一律视为 `human:default`）。

### Q：配了 token 却 403

环境变量没展开：`tokens: ["${A2A_TOKEN_SEAFISH}"]` 在变量没设时展开成 `[""]`。
`load_members()` 会丢掉空串——否则会一边认不出 token（403）一边拒绝匿名（401），
把部署者自己锁在门外。检查 `echo $A2A_TOKEN_SEAFISH`。

### Q：能聊但派不了活

这是**设计如此**，不是 bug。好友默认只给 `chat`，`delegate` 要显式授予：
```bash
python run.py social grant <peer> --scopes chat,delegate
```

### Q：`grant` 报权限错误

两种可能，看具体文案：

**「只能调整好友之间的权限」** —— 你们还不是好友，或者你用错了身份。
如果授予方是个 agent，记得加 `--as <那个 agent>`：

```bash
python run.py social grant human:seafish --scopes chat,delegate \
       --as partner-ocr --url http://localhost:8080 --token <partner 的 token>
```

**「权限上行闭包：不能授予自己没有的权限 `['delegate']`」** ——
触发了**权限上行闭包**：授予方的 `max_scopes` 天花板里没有 `delegate`。
这是设计如此，不是 bug。给它加上，或去掉 `max_scopes`：

```yaml
- id: agent:partner-ocr
  max_scopes: [peek, chat, invite, delegate]   # 否则 delegate 授不出去
```

> 给外部成员设窄天花板是好习惯，但要清楚代价：**设了就授不出被砍掉的那些权限**。

### Q：`social approve` 报「只有该待办的归属人才能批准」

报错里会带上归属人：
```
只有该待办的归属人才能批准（归属人：human:guest）
```
用 `--as` 不对——`--as` 是代表**自己的 agent**。正确做法是换 token 以该人类身份调用：
```bash
python run.py social approve ap-xxxx --token <guest 的 token>
```

### Q：以前能派活，现在突然不行了

**信任衰减**：这条边超过 90 天没互动，`delegate` 已降级回 `chat`。
```bash
python run.py social audit --peer <id>     # 看 stale 记录
python run.py im chat <peer> "…"           # 互动一次即自动续租
python run.py social grant <peer> --scopes chat,delegate   # 或重新授予
```

### Q：agent 怎么不自己加好友

三层开关都要开（agent 侧完整交互面见[第十章](#十agent-视角agent-怎么用这套功能)）：

三层开关都要开：
1. 成员 `autonomy.request.enabled: true`（默认 `false`）
2. 巡航要 `A2A_AUTONOMY_ENABLED=true`（默认 `false`）
3. 目标得**可见**（`discoverable` 不为 `private`，或被引荐过）

只开第 1 条时，任务内信号（`social.need`）仍然有效——巡航才受第 2 条控制。

### Q：`private` 成员加不上

`private` 只挡「被搜索到」。**直接指名申请是可以的**：
```bash
python run.py social add someone --reason "…"
```
如果连 id 都不知道，需要有人引荐（`social introduce`）。

### Q：拉黑后聊天报 422

拉黑会 `freeze_between` 归档直聊（可读不可写），这是刻意的。
重新加好友会 `thaw_between` 解冻。

### Q：我想回到没有好友制的旧行为

删掉 `config/members.yaml`，或设 `A2A_SOCIAL_MODE=off`。
**此时行为与 v0.3.0 逐字节一致**——这是贯穿整个设计的硬约束。

---

## 十二、上线检查清单

- [ ] `config/members.yaml` 已就位，**不在版本库里写明文 token**（一律 `${ENV}`）
- [ ] 每个成员都配了 token（否则所有匿名请求都会被当成同一个人）
- [ ] 每个 agent 都声明了 `owner`（多人类部署下**必须**）
- [ ] 外部成员的 `max_scopes` 收窄（权限上行闭包的天花板）
- [ ] `A2A_SOCIAL_MODE=strict`
- [ ] `A2A_API_TOKEN` 与 `A2A_REQUIRE_AUTH=true`（公网部署）
- [ ] `data/relations.json` 已纳入备份（**关系是运行时状态，丢了要重新加好友**）
- [ ] 自主交友先小范围试：`request.enabled` 只对一两个 agent 开，
      且 `requireOwnerApproval: true` 观察一周
- [ ] 巡航保持 `A2A_AUTONOMY_ENABLED=false`，确认无异常再按需打开
- [ ] Nginx 反代关了 SSE 缓冲（见 README 部署章节）
- [ ] 定期看 `social audit`，确认没有意外的自动同意

---

## 附：文件与职责

| 文件 | 职责 |
| --- | --- |
| `config/members.yaml` | 成员与策略（**人写**） |
| `data/relations.json` | 关系 / 拉黑 / 待办 / 审计（**程序写，勿手编**） |
| `a2a_hub/relations.py` | 社交图谱：成员、关系、权限、门禁、发现、审计 |
| `a2a_hub/autonomy.py` | 自主交友策略：**纯函数，不碰状态**，零依赖 |
| `a2a_hub/social.py` | 会话层：只调用注入的门禁，不做策略 |
| `a2a_hub/orchestrator.py` | 协同编排：`_admit` 单一入口过滤 + 记录 `refusedAgents` |
| `a2a_hub/rpc.py` | JSON-RPC 方法与 `-32008` 错误映射 |
| `a2a_hub/server.py` | HTTP 端点 + lifespan（启动 `sweep_stale` / 巡航） |

分层纪律：**HTTP/RPC（认证） → relations（策略） → SocialHub（会话） →
AgentRegistry → Adapter**。能力画像靠 `set_capability_resolver()` 注入，
关系层**不** import registry。
