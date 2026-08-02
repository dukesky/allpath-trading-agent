# All Path Trading Agent — 设计文档 (v1)

日期：2026-07-30
状态：待用户批准

## 1. 项目定位

开源的、自部署的、基于 LLM 的**中长线**股票交易 agent 框架。它与用户对话了解偏好、共创并持续修订投资策略（含止盈/止损规则）、每日监控、按授权级别执行真实交易、发送通知，并通过多层记忆与用户共同学习成长。

**不做的事**：短线/高频量化、托管服务（第一版）、投资建议合规背书（自部署、自担责任、paper-first）。

**差异化**：现有 LLM 交易项目（TradingAgents、ai-hedge-fund）停在"给出分析决策"；执行框架（Lumibot、LEAN）没有 LLM 推理层。本项目打通"对话共创策略 → LLM 决策 → 真实券商执行 → 复盘共同成长"全链路。

## 2. 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 目标市场 | 美股优先 |
| 使用方式 | 自部署个人 agent（用户自带 LLM key 与券商账户，数据全部本地） |
| 交互入口 | Web UI（聊天 + 仪表盘）为主，邮件通知为辅 |
| 执行安全 | 分级授权：notify(仅通知) / confirm(确认后执行) / auto(额度内自动) |
| 技术栈 | Python 全栈（FastAPI + SQLite + APScheduler），前端轻量（服务端渲染/htmx） |
| MVP 范围 | Alpaca **paper trading** 跑通全闭环，验证后再开 live 开关 |
| 技术路线 | 自研轻量核心（方案 A）：薄 Broker 抽象 + 混合策略引擎；不依赖 Lumibot，但接口设计对齐它以便未来复用/迁移 |
| 包名 | `allpath_trade`（项目名仍为 All Path Trading Agent） |

安全性说明：真正安全关键的部分（认证、传输、下单）由券商官方 SDK（`alpaca-py`）承担；我们的适配层只有几百行、易审计。Credentials 只存本地环境变量/配置，永不上传。

**配置管理**：LLM key 与券商 credentials 存于本地 `.env` 文件，由统一的 SettingsStore 读写；环境变量可覆盖。两种设置入口：① Web UI 设置页；② 直接告诉 agent（agent 经 `update_settings` 工具写入，仅限券商配置等）。**LLM key 必须先在 Web UI 设置**（bootstrap：没有 key 之前 agent 无法对话），券商配置两种方式皆可。

## 3. 整体架构

```
┌─────────────────────────────────────────────────┐
│                   Web UI (聊天 + 仪表盘)          │
└──────────────────────┬──────────────────────────┘
                       │ HTTP/WebSocket
┌──────────────────────▼──────────────────────────┐
│              FastAPI 应用层                      │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Agent核心 │  │ 策略引擎  │  │  调度器        │  │
│  └────┬─────┘  └────┬─────┘  └───────┬───────┘  │
│  ┌────▼────────────▼────────────────▼───────┐   │
│  │     风控守门层 (确定性, LLM 不可绕过)       │   │
│  └────┬──────────────────────────────────────┘  │
│  ┌────▼─────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Broker层 │  │ 数据层    │  │  通知层        │  │
│  └──────────┘  └──────────┘  └───────────────┘  │
│         SQLite (策略/记忆/交易日志/对话)          │
└─────────────────────────────────────────────────┘
```

## 4. Agent 核心（系统灵魂）

### 4.1 数据的两种流向

- **Agent 主动拉取（agentic）**：数据源封装为工具，agent 在对话/复核/review 时自主调用（像 ChatGPT 联网一样），自行搜索数据验证和矫正策略。
- **调度器被动检查（deterministic）**：每日定时任务用普通代码拉价格、评估确定性规则。零 token、绝对可靠，只负责"发现触发"。

### 4.2 Agent 工具箱（第一版）

| 工具 | 用途 |
|---|---|
| `get_quote` / `get_bars` | 实时报价、历史 K 线 |
| `web_search` | 搜索新闻、财报、宏观事件 |
| `get_portfolio` / `get_account` | 查持仓、账户状态 |
| `propose_order` | 提交订单意图（必经风控守门层） |
| `read_strategy` / `update_strategy` | 读取/修订策略文档（修订需用户确认） |
| `read_memory` / `write_memory` | 读写四层记忆 |

### 4.3 三个运行循环

1. **对话循环**（用户发起，随时）：Web UI 聊天。上下文 = 用户画像 + 活跃策略 + 持仓 + 近期交易日志（按相关性组装，不全量注入）。场景：onboarding 访谈、讨论股票、共创/修订策略。策略变更流程：agent 起草 diff → 用户确认 → 版本 +1 生效。
2. **哨兵循环**（盘中每 1 小时一次，间隔为可调参数 `sentinel_interval_minutes`，默认 60；廉价）：拉价格 → 评估规则 → 无触发仅记日志（零 LLM 成本）；有触发 → 硬规则直接进执行链路；软规则唤醒 agent 联网复核 → 按授权级别行动。
3. **反思循环**（每日 + 每次交易后）：
   - 每日深度 review（收盘后）：逐策略检验投资论点与证伪条件（联网搜索）、检视组合风险、发送 review 报告、提出修订建议。
   - 交易后复盘：记录完整决策链，定期归纳"哪类判断准/不准"写入记忆。

### 4.4 执行链路（发现 / 决定 / 执行 三段分离）

```
[发现触发]           [决定怎么做]            [执行下单]
确定性代码       →   硬规则: 无 LLM 直通  →   确定性代码
(规则求值器)         软规则: agent 复核       (风控守门 → alpaca-py 官方 SDK)
```

- **硬规则**（如止损）：触发即执行，全程无 LLM，保证"该止损时一定止损"；事后通知 + agent 复盘。
- **软规则**（如逢低加仓）：触发后 agent 复核（是黄金坑还是基本面塌了？），再按授权级别执行/通知。
- LLM 永不直接触碰下单接口，只能产出订单意图；所有订单必经风控守门层。
- 典型配置：止损用硬规则保命，建仓/加仓用软规则保聪明。规则软硬由用户与 agent 共创时决定。

### 4.5 记忆系统（四层）

1. **用户画像**（缓变）：风险偏好、资金状况、目标、行业偏好、决策习惯（如"拿不住盈利单"）。
2. **策略记忆**（版本化）：每策略全历史文档——论点、规则、每次修订原因；废弃策略保留作学习素材。
3. **个股档案（stock dossier）**：每个交易/关注过的 ticker 一份持续演化档案：基本认知（业务、长期看法、估值锚点）、行为特征（如"NVDA 财报日平均波动 ±8%"）、交易史（每次进出与判断对错）、个股专属教训。关注越久 agent 对该股越熟。
4. **经验教训**（复盘沉淀）：结构化 lesson 条目，新决策时按相关性检索注入。

**交叉更新原则**：记忆写入工具不绑定特定循环。任何循环（对话/哨兵复核/周度 review/交易复盘）结束时统一执行"记忆沉淀"步骤，agent 自主判断本次经历应更新哪些层（一次深度复盘可能同时更新个股档案 + 修订策略建议 + 沉淀教训 + 刷新画像）。

存储：结构化数据（画像字段、规则、交易日志）→ SQLite；prose 记忆（论点、档案、教训）→ markdown 文档，agent 经工具读写。

### 4.6 LLM 层与成本

多 provider 抽象，第一版支持三家：**Claude（Anthropic 原生 API）/ OpenAI / OpenRouter**。实现上为两种客户端：Anthropic 原生 + OpenAI 兼容协议（OpenAI 与 OpenRouter 共用，仅 `base_url` 与 key 不同），配置里按 `provider + model + api_key` 声明。**开发与测试默认用 OpenRouter**（一个 key 可切换多家模型，便于对比）。策略讨论与每日 review 用强模型，哨兵触发复核可配中档模型，规则评估零 LLM。预估：哨兵零成本，每日 review $1–3/次（取决于策略数量与模型选择）。

## 5. 策略文档格式

用户与 agent 讨论的产物，同时是调度器触发后 agent 执行的策略依据。人机双读的结构化文档：

```yaml
# strategy: aapl-long-2026
name: "AAPL 长线持有"
status: active            # draft / active / paused / archived
version: 3                # 修订 +1，历史全保留
authorization: confirm    # notify / confirm / auto

thesis: |
  看好 Apple 服务业务增长与 AI 端侧落地，目标持有 12-18 个月。
  核心假设：服务收入年增 >12%；若假设被证伪则退出。

position:
  ticker: AAPL
  # 比例制与金额制皆可，允许混用
  target_weight: 15%      # 或 target_value: $15000
  max_weight: 20%         # 或 max_value: $20000

rules:
  - id: stop-loss
    type: hard
    condition: "price < 185"
    action: "sell 100%"
  - id: take-profit-1
    type: soft
    condition: "price > 260"
    action: "sell 50%"          # 亦支持金额制: "sell $5000"
  - id: add-on-dip
    type: soft
    condition: "price < 205 and position_weight < target_weight"
    action: "buy $3000"         # 亦支持: "buy to target_weight"

review:
  cadence: daily
  invalidation: "服务收入连续两季增速 <10%，或 AI 战略明显落后"
```

- `condition` 为受限表达式（词汇表：价格、涨跌幅、仓位、均线等），由确定性解析器求值——非 LLM 猜测，非任意代码。
- `action` 支持比例制（`sell 50%`）与金额制（`buy $3000`）。
- `thesis` / `invalidation` 为 prose，归 LLM 在 review 时联网检验。

## 5.1 Phase 2 详细设计：策略引擎 + 哨兵循环（2026-07-30 讨论定稿）

**交互原则（全项目适用）**：用户的主要操作入口是 Web UI；terminal 是辅助入口；email 只做通知、不承载操作（邮件中不放操作链接）。Phase 2 的 CLI 处置命令是 Phase 5 UI 上线前的过渡，因此队列等能力一律实现为 service 层 API，CLI 只是薄壳。

### 存储
- `strategies/*.yaml` 每策略一文件（当前版本，人可读可直接编辑），策略 id = 文件名；
- SQLite `strategy_versions` 表存每次生效版本的全文快照（版本号、时间、修订原因）。
- 加载时全量校验（条件/动作可解析、字段合法），坏策略在加载时报错而非触发时。

### 规则求值器（确定性，零 LLM）
- 词汇表 v1：`price`、`position_weight`、`position_qty`、`avg_entry_price`、`pnl_pct`（相对建仓均价盈亏%）；
- 运算符：`< > <= >= ==`、`and/or/not`、括号、数字字面量；
- 实现：Python `ast.parse` + 节点白名单（仅比较/布尔/数字/白名单变量），无任意代码执行面；
- Action 语法：`sell 50%` / `sell all` / `sell $5000` / `buy $3000` / `buy to target_weight`，结合当前价格与持仓换算为 OrderIntent。
- 更丰富的表达式（均线/年线/K线函数等）列入 docs/TODO.md 的 Phase 2.5。

### 触发语义
- **一次性**：规则触发后 `state: armed → triggered`，不再评估；重新武装需显式操作（用户，或 Phase 3 后 agent 修订策略时）。
- 处置矩阵 = 规则类型 × 策略授权级别：

| 策略授权 | 硬规则触发 | 软规则触发 |
|---|---|---|
| `auto` | 立即经 Executor 执行 + 通知 | 入待处理队列 + 通知 |
| `confirm` | 入待处理队列 + 通知 | 入待处理队列 + 通知 |
| `notify` | 仅通知 | 仅通知 |

### 待处理队列（UI 一等公民）
- SQLite `pending_reviews`：触发详情、触发时快照（价格/持仓/换算好的订单意图）、状态（pending/approved/rejected/expired）、处置记录；
- `ReviewQueue` service API（list/approve/reject），approve 经 Executor 执行；CLI、未来的 Web UI、Phase 3 的 agent 都调这套 API。

### 运行形态
- `allpath-trade run`：常驻进程（APScheduler），盘中每 1 小时哨兵（`sentinel_interval_minutes` 参数可调，默认 60），ET 9:30–16:00 工作日判断（节假日历法在 TODO）；每日反思任务位留待 Phase 6；
- `allpath-trade check`：单次手动哨兵；`allpath-trade strategies`：列策略与规则状态；`allpath-trade rearm`：重新武装；`allpath-trade reviews list/approve/reject`：过渡期队列处置。
- 通知层：SMTP 邮件（未配置时降级为日志），Phase 2 一并搭好。

## 5.2 Phase 3 详细设计：Agent 核心（2026-07-31 讨论定稿）

### LLM 层（allpath_trade/llm/）
- 统一接口 `LLMClient.complete(messages, tools) -> text | tool_calls`；两个实现：**OpenAICompatClient**（openai SDK + base_url，覆盖 OpenRouter 与 OpenAI 直连）、**AnthropicClient**（anthropic SDK 原生）。
- 配置：`LLM_PROVIDER=openrouter|openai|anthropic` + key + 三档模型 `CHAT_MODEL`（对话/策略共创，强模型）/ `REVIEW_MODEL`（哨兵复核，可用中档）/ `MEMORY_MODEL`（记忆提炼，最强档——写错会长期污染上下文）。测试默认 OpenRouter。

### Agent 工具循环（allpath_trade/agent/）
- 自研 tool loop（不引重框架）：LLM 工具调用 → 执行 → 回填 → 循环，轮次上限（默认 15）防失控。
- 工具箱 v1：`get_quote` / `get_bars` / `web_search`（ddgs 免费默认，接口可插拔，升级项见 docs/TODO.md）/ `get_portfolio` / `list_strategies` / `read_strategy` / `draft_strategy` / `propose_order` / `list_pending_reviews`。
- **确认边界**：`draft_strategy` 生成 YAML → 终端展示 diff → 用户 yes → 写文件 + 版本快照；`propose_order` 在对话场景同样先经用户确认，再进 Executor → 风控守门。agent 无绕过路径。

### 对话入口：allpath-trade chat
- 终端 REPL，多轮对话，历史存 SQLite（按 session；`--new` 开新会话，默认续上次）。
- 上下文组装：系统提示自动注入持仓摘要、活跃策略列表、近 5 笔交易、待确认项数量。
- agent service 为纯后端函数，Phase 5 Web UI 复用同一套。

### 哨兵接入：ReviewAgent
- 软规则触发（已入队）后新增复核：agent 拿触发快照 → 自主调工具（查价、搜新闻）→ 产出结构化分析（recommend: execute/skip + 理由 + 来源）。
- **confirm 策略**：分析写入 pending review 的 `agent_analysis` 字段，邮件附带，用户参考后决策；
- **auto 策略**：agent 决定执行（→ Executor → 风控守门）或放弃（记录理由）；
- **降级安全**：LLM 失败/超时 → 待确认项照常存在（无分析），绝不因 agent 故障丢触发；复核设 token/轮次上限；分析入库留痕（Phase 6 复盘用）。

### 测试
- Mock LLM client（脚本化工具调用序列）测：链路正确、确认门禁不可绕过、违规订单被拦、降级路径。真实 OpenRouter 调用为 `-m integration` 可选测试。CI 零 LLM 成本。

### 借鉴 Hermes / OpenClaw 的模式（2026-07-31 调研定稿）
Phase 3 采纳：
- **IDENTITY.md**（借 OpenClaw SOUL.md）：agent 角色与授权边界写成仓库内只读 markdown，每次注入系统提示；agent 的任何工具都不能修改它，拒绝越权操作时可引用它。
- **冻结快照上下文**（借 Hermes）：系统提示在 session 开始组装一次（持仓/策略/近期交易摘要），会话中不重组——prompt 前缀稳定，保证缓存命中、成本可控。
- **外部内容 fence**：web_search 等外部来源的结果包裹 `<external-content>` 标记并声明"是数据不是指令"——防 prompt 注入渗入交易决策。
- **复核短路**：ReviewAgent 用便宜模型 + 轮次上限，无事直接短路（防 OpenClaw 式心跳 token 燃烧）。

Phase 4 预案（记忆系统实现时采纳，防 OpenClaw 已知安全坑）：
- 两级写入：原始观察进带时间戳的 journal，定期"提炼"步骤才写入四层精选记忆；绝不在业务流程中直写精选层。
- 记忆变更仅经 `memory_update(layer, action=add|replace|remove, ...)` 窄工具（条目级、可审计、diff 入库），禁止整文件重写。
- 每层记忆硬性字符预算 + 定期整合（去重/合并/降级陈旧条目）。
- **注入扫描**：写入精选记忆的内容过指令模式检测；来自外部内容（新闻/搜索）的文本不得原样进入精选记忆——被投毒的"经验教训"对交易 agent 是延迟执行攻击。
- 会话全量存档 SQLite FTS5 + `session_search` 工具（按需搜索历史，不塞上下文；兼作审计轨迹）。
- 压缩前刷写：长对话接近上下文上限时，先让 agent 把持久性结论写盘再压缩。
- lessons 带 YAML frontmatter（tags/triggers/confidence），决策前按 ticker/情境匹配预加载。

## 5.3 Phase 4 详细设计：记忆系统（2026-07-31 讨论定稿）

### 存储布局（用户可读、可编辑、可 git）
```
memory/
├── user_profile.md          # 用户画像（预算 2000 字符）
├── strategies/<id>.md       # 策略记忆（每文件 2000）
├── stocks/<TICKER>.md       # 个股档案（每文件 3000）
└── lessons/<slug>.md        # 经验教训（每文件 2000，YAML frontmatter: tags/tickers/confidence）
```
- 条目 = 以 `- ` 开头的段落（空行分隔），条目级增删改；SQLite `memory_log` 记录每次变更 diff（审计）。
- 注入系统提示时按预算截断（文件本身不动），带 "(truncated — use session_search)" 标记。

### 写入纪律（防注入，OpenClaw 教训）
- 唯一写入口：`memory_update(layer, key, action=add|replace|remove, match, text)` 窄工具；layer=profile|strategy|stock|lesson；key 过 id 正则校验；IDENTITY.md 永不可写。
- **注入扫描**：写入前过指令模式检测（"ignore previous"、"system:"、fence 标记等），命中即拒；单条目长度上限 500 字符；外部内容（搜索结果）不得原样写入——必须是 agent 自己的总结。
- 两级写入：原始观察进 SQLite `observations`（哨兵触发、复核分析、对话备注自动落库；交易已有 trades 表），只有提炼步骤才写精选层。

### 提炼（consolidation）
- **每日完整提炼**（收盘后，挂 Phase 2 预留的调度位，强模型）：读当日 trades/触发/复核分析/observations + 当前记忆文件 → 经 memory_update 提出条目级变更（每条仍过扫描）。
- **对话后轻提炼**（chat 退出时，同样走 MEMORY_MODEL）：只读本次对话，只沉淀用户明确表达的偏好/决定。
- 提炼失败静默降级（观察still在库，下次再炼）；`allpath-trade memory consolidate` 可手动触发。

### 记忆进上下文
- chat 系统提示（冻结快照）注入：用户画像全文 + 持仓/活跃策略相关的个股档案 + 相关 lessons（按 ticker/tags 匹配），各按预算截断。
- ReviewAgent 提示注入：该 ticker 的个股档案 + 匹配 lessons——复核质量随记忆积累提升。

### 会话搜索
- `conversation_turns` + `observations` 建 SQLite FTS5 索引；新工具 `session_search(query)` 返回命中消息及上下文窗口——历史按需搜索，不塞上下文；兼作审计。

### CLI
- `allpath-trade memory show [layer] [key]`（查看）、`allpath-trade memory consolidate`（手动提炼）。

## 6. 风控守门层

所有订单意图的必经之路，纯确定性代码，LLM 不可绕过。检查项（用户可配）：单笔金额上限、单股仓位上限、当日交易次数上限、账户现金下限、授权级别匹配、live/paper 开关。任何检查不过 → 拒单 + 记录 + 通知。

## 7. 其余模块（标准工程）

- **Broker 层**：薄接口 `get_account / get_positions / get_orders / submit_order / cancel_order` + 能力标志；第一个适配器 Alpaca（paper 起步，live 为显式开关）。
- **数据层**：yfinance 起步（日线 + 报价）；后续 Alpaca data、Tiingo（EOD）、Finnhub（新闻/情绪）。
- **通知层**：SMTP 邮件 + Web UI 站内消息；事件：触发、成交、拒单、周报、待确认请求。
- **调度器**：APScheduler；任务：哨兵循环（盘中每 1 小时，`sentinel_interval_minutes` 可调）、每日 review（收盘后）。
- **Web UI**：聊天窗 + 持仓/策略面板 + 交易与决策日志 + 待确认队列。服务端渲染/htmx，够用不上重框架。

## 8. 项目结构

```
allpath-trading-agent/
├── allpath_trade/
│   ├── agent/          # LLM 核心：对话、工具、上下文组装、provider 抽象
│   ├── strategy/       # 策略文档模型、规则解析/求值、版本管理
│   ├── memory/         # 四层记忆读写与检索
│   ├── broker/         # Broker 抽象 + alpaca 适配器
│   ├── data/           # 行情/新闻工具
│   ├── risk/           # 风控守门层
│   ├── scheduler/      # 三循环调度
│   ├── notify/         # 邮件 + 站内通知
│   ├── server/         # FastAPI 路由 + WebSocket
│   └── web/            # 轻量前端
├── tests/
└── docs/
```

## 9. 测试方案

- **单元测试全覆盖**：规则求值器、风控守门、策略版本管理（钱的最后防线）。
- **集成测试**：Broker 层对 Alpaca paper 沙盒。
- **Agent 测试**：mock LLM provider，验证工具调用链路正确（不测 LLM 说什么，测工具能否正确调用、违规订单能否被风控拦截）。

## 10. MVP 交付顺序

1. Broker + 数据 + 风控（地基）
2. 策略引擎 + 哨兵循环
3. Agent 对话 + 工具
4. 记忆系统
5. Web UI + 邮件
6. 反思循环

每步独立可验证。
