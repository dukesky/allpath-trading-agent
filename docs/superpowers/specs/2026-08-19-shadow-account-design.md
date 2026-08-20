# 影子账户(Shadow Account)设计 — v2:双活并行

日期:2026-08-19(v2,按用户反馈重写:两账户**同时活跃**,非二选一)
状态:spec 待用户过目

## 要解决的问题

用户的真钱在没有正规 API 的券商(Robinhood)里。目标:**以真实持仓为基准
的建议引擎**——agent 按用户真实仓位分析、建议,用户手动去券商执行;同时
**保留** Alpaca 模拟盘作为策略验证沙盒。两者**并行运行、互不干扰**,绝不
触碰真实券商凭证。

## 核心架构:账户是第一维度

`ACCOUNTS = ("paper", "shadow")`。每个账户拥有**独立的一整套 pipeline**:

| 独立(按账户隔离) | 共享 |
|---|---|
| Broker(Alpaca / ShadowLedger) | LLM 配置与用量记录 |
| 策略目录 `strategies/{account}/`(用户决定:各一套) | **个人画像记忆**(profile 层) |
| 记忆的策略/个股/教训层 `memory/{account}/…`(用户决定) | 通知通道(SMTP/ntfy/Telegram 绑定) |
| 交易日志、待确认队列、规则触发状态 | 登录/令牌、Web 服务进程 |
| 反思报告、权益曲线、聊天对话与上下文 | 风控闸门代码(参数将来可分,先共享) |
| 哨兵巡检(每 tick 依次跑两账户,互相隔离) | |

**实现骨架**:`Components` 变为 per-account 束(`components.for_account(a)`
或 `AccountComponents` 字典),同一 SQLite 连接;所有账户级表加 `account`
列(迁移:存量行全部回填 `paper`——现有历史本来就是 paper 的);
`ChatService`、`Reflector`、`Sentinel` 各两实例。构造点仍唯一(`build_components`)。

## 用户已定的决定

1. **双活**:两套 pipeline 同时跑;
2. **通知都发**,标题前缀 `[Paper]` / `[Shadow]`;
3. **记忆**:profile 共享,策略/个股/教训层按账户分;
4. **策略各一套**(策略目录按账户分;聊天里让 agent 起草时落到当前账户)。

## 本 spec 补充敲定(请过目)

| 决定 | 选择 | 理由 |
|---|---|---|
| 网页账户切换 | 顶部全局切换器,cookie 记住;**整站**(Dashboard/Chat/Pending/Memory/Reports/Strategies)跟随 | 用户的心智模型:切系统 |
| Telegram 的当前账户 | 独立于网页 cookie:`/account` 命令切换(回复 inline 按钮 Paper/Shadow),默认 shadow;每条 bot 消息带账户前缀 | 手机和电脑各自有上下文 |
| 审批的账户绑定 | 每条待确认行带 `account`,批准永远作用于**该行的账户**,与当前界面选择无关;卡片/按钮/链接都显示账户 | 点错账户在结构上不可能 |
| 夜间任务 | digest/反思/整合按账户各跑一遍(顺序:paper 后 shadow,逐任务隔离);**反思仅在该账户有 active 策略时跑**(空账户不烧 Opus) | 成本自然门控 |
| shadow 下的 hard 规则 | 立即记账 + 通知 "**place this order in your brokerage now**" | 影子无真自动化,hard = 到点即提醒 |
| 成交价回填 | 默认按记账时市价;`shadow_record_fill` 工具可修正,不强制 | |
| 风控闸门 | 两账户都生效(参数暂共享) | 建议也守纪律 |

## 组件设计

### ① ShadowLedger broker(`broker/shadow.py`)

实现全部 `Broker` 方法,`name="shadow"`。SQLite 表:`shadow_positions`
(ticker, qty, avg_cost, last_price, last_price_ts)、`shadow_cash`(单行)、
`shadow_orders`、`shadow_equity_daily`。`submit_order`:取现价即时"成交"
记账(碎股支持、超卖→REJECTED、无价→拒绝);`get_account/positions`:现价
估值,行情失败用最后已知价并标注"price as of …";`get_equity_history`:
读每日落点(挂在夜间任务,另外每次 get_account upsert 当天点)。
这是记账不是交易——所有文案讲透。

### ② 账户维度落库与迁移

- `trades`、`pending_reviews`、`rule_states`、`reports`、`conversations`
  加 `account TEXT NOT NULL DEFAULT 'paper'`(`_MIGRATIONS`);存量即 paper;
- `reports` 唯一键变 `(account, date)`;
- 记忆:`memory/profile.md` 留在根(共享);其余层迁移到 `memory/paper/…`
  (一次性启动迁移,幂等,迁移前备份目录);`memory/shadow/…` 从空开始;
- 策略:`strategies/*.yaml` 启动迁移到 `strategies/paper/`;`strategies/
  shadow/` 从空开始。`.gitignore` 已覆盖(整个目录未跟踪)。

### ③ 调度:两套并行

- 哨兵每 tick:`for account in ACCOUNTS: run_pass(account)`,各自 try/except,
  各自心跳键(`sentinel_last_pass:{account}`);Dashboard 心跳行显示当前
  账户的;
- 夜间链:`for account: digest → reflection(若有 active 策略) → consolidation`,
  每步隔离;两账户共 4-6 个 LLM 调用/晚,Usage 面板照记;
- 成本提示写进文档:双账户反思 ≈ 每晚两次 Opus 调用。

### ④ 聊天与 agent

- 每账户独立 `ChatService`(各自对话历史、各自记忆上下文、各自策略目录、
  各自队列);网页按 cookie 路由到对应实例;Telegram 按 `/account` 状态路由;
- agent 系统提示注明当前账户及其性质(paper = Alpaca 沙盒真执行 /
  shadow = 镜像真钱的记账本,订单需用户手动去券商执行);
- 影子账户聊天新增账本工具:`shadow_set_position` / `shadow_set_cash` /
  `shadow_remove_position` / `shadow_record_fill`——网页/TG 走 Pending
  审批(kind=`shadow_edit`,before→after 卡片),终端阻塞确认;CSV 导入在
  Settings,同样生成 `shadow_edit` 提案;
- profile 记忆两边可读可写(同一文件);个股/教训写入落当前账户目录。

### ⑤ 通知

- 所有事件(触发/成交/入队/反思摘要/digest)主题加前缀 `[Paper]`/`[Shadow]`;
- Telegram 按钮消息同样前缀;审批动作绑定行内 account;
- shadow 的订单文案 = 建议语气 + "place this order in your brokerage now"。

### ⑥ 界面

- 全站顶部账户切换器(chip:`PAPER` 蓝边 / `SHADOW` 琥珀边,点击切换,
  cookie 记住,默认 paper 直到用户首次切换);每页标题旁重复当前账户 chip;
- Pending 卡片带账户 chip(即使在对侧视图也能从通知链接直达并正确处理);
- Settings → Brokerage:Alpaca 区(现状)+ Shadow 区(账本摘要、CSV 导入、
  Reset ledger 危险按钮);
- Dashboard 空影子账本 → 引导文案("Import your positions — tell the agent
  in Chat or upload a CSV")。

### ⑦ 安全不变量

- 零真实券商凭证;
- 账本唯一写路径:人批的 `shadow_edit` 提案 或 经风控闸门的 submit_order;
- 账户间零串扰:所有查询带 account 过滤(评审重点);审批按行内 account
  执行;记忆写入落对应目录(profile 除外,设计如此);
- 反思守卫、审批守卫、注入防护:原样,两套各自生效。

### ⑧ 降级与已知限制

- 行情失败:最后已知价 + 标注;无价拒绝记账;
- 空 shadow:哨兵照跑(仓位条件求值 0),反思跳过(无 active 策略);
- 股息/拆股/税批次:手动修正,记为限制;期权/加密不做;
- 双活使夜间 LLM 成本约翻倍(Usage 面板可见,反思有 active-策略门控)。

### ⑨ 测试要点

- ShadowLedger 账本全逻辑;迁移幂等 + 存量回填 paper + 记忆/策略目录搬迁;
- 账户串扰矩阵:A 账户触发/入队/批准/反思绝不读写 B 的表行、记忆目录、
  策略目录(评审必须逐项攻击);
- 切换器 cookie、TG /account 路由、审批跨视图按行内账户执行;
- 通知前缀全事件覆盖;shadow 文案;English-only;
- 夜间链两账户隔离(A 失败 B 照跑)、反思门控、心跳分键。

## 实现顺序建议(约 7 任务)

账户维度迁移+存量回填(②)→ ShadowLedger(①)→ Components/调度双活
(③)→ 聊天/agent 双实例 + 账本工具(④)→ 通知前缀 + 审批账户绑定(⑤)
→ 界面切换器 + Settings/引导(⑥)→ 文档与成本说明。
