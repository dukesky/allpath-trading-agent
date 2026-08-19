# 影子账户(Shadow Account)设计

日期:2026-08-19 · 状态:spec 待用户过目

## 要解决的问题

现在 agent 只服务 Alpaca 模拟盘——一个从零开始、和用户真实财务无关的沙盒。
用户真正的钱在 Robinhood 这类**没有正规 API** 的券商里。用户想要的是:
**以我真实账户的持仓为基准,agent 给我建议,我自己去券商手动执行**——
把系统从"玩沙盒"升级成"管我真钱的顾问",同时**绝不触碰真实券商的登录
凭证**(逆向工程的非官方库不安全、违反条款、随时被封)。

## 核心思路

影子账户是 `Broker` 接口的**第二个实现**:一个本地记账本。持仓由用户导入;
`submit_order` 只记账、不外发;估值用行情源;权益历史自己每日落点。
**上层(哨兵、风控闸门、审批队列、通知、反思、agent 工具、Dashboard)一行
不改**——它们从第一天起就只认 `Broker` 接口,`build_components` 是唯一构造
点。这是整个方案最重要的一句话。

## 用户决定(本 spec 替用户先定,请过目)

| 决定 | 选择 | 理由 |
|---|---|---|
| 两账户关系 | **并存,Active 二选一** | 同一时刻 agent 服务一个账户;切换不丢另一个的数据 |
| 默认 Active | **切到 shadow 后即 shadow;首次安装仍是 paper** | 真钱账户才是用户关心的;但不改变新用户的开箱路径 |
| 持仓导入 | **聊天为主,CSV 为辅** | "我 Robinhood 里有 50 股 AAPL 成本 180"最自然;CSV 给持仓多的人 |
| 成交价回填 | **允许、不强制** | 默认按 approve 时市价记;用户可事后说"那笔实际 332.5" |
| 影子账户的 hard 规则 | **照常"执行"(记账)并通知,通知明确写"请手动执行"** | 影子账户没有真自动化;hard 规则的语义变成"到点立即提醒你执行" |
| 风控闸门 | **照常生效** | 建议也该守纪律;闸门参数对影子账户同样有意义 |

## 组件设计

### ① `ShadowBroker`(新 `allpath_trade/broker/shadow.py`)

实现全部 `Broker` 方法,`name="shadow"`,`is_paper=True`(它不是真钱执行——
这个标志下游用来决定文案"paper/LIVE",影子显示为 `shadow`,见 ⑤)。

- 数据全部在 SQLite 新表(同一个 DB):
  - `shadow_positions(ticker PK, qty, avg_cost, updated_ts)`
  - `shadow_cash(id=1, cash, updated_ts)`——单行
  - `shadow_orders(id, ts, ticker, side, qty, notional, status, fill_price, filled_at, note)`
  - `shadow_equity_daily(date PK, equity, cash)`——每日收盘落点,给权益曲线
- `submit_order(intent)`:取行情当前价(`DataSource.get_quote`)→ 立即"成交":
  买入减现金加持仓(按加权平均更新成本)、卖出反之(不允许卖空/超卖→
  `OrderStatus.REJECTED` + 原因);`notional` 按现价折算股数(支持碎股——
  Robinhood 支持);返回 `Order(status=FILLED, filled_avg_price=现价,
  filled_at=now)`。**这是记账,不是交易**——文档和通知都要把这点说透。
- `get_account()`:equity = cash + Σ(qty × 现价);buying_power = cash(无杠杆)。
- `get_positions()`:每持仓估值,`unrealized_pl = (现价 − avg_cost) × qty`。
- `get_equity_history(days)`:读 `shadow_equity_daily`。每日收盘落点挂在
  现有 `run_daily_jobs`(digest 之前,独立 try/except),此外**每次
  `get_account()` 也顺手 upsert 当天点**(让当天曲线不空)。
- 行情失败:估值用**最后已知价**(持仓表存 `last_price`/`last_price_ts`),
  并在 Dashboard 标注"价格截至 …";从未有价的新持仓 → 该仓 `market_value`
  按成本计、标注。永不抛异常到上层。

### ② 持仓导入与修正

- **聊天工具**(`register_action_tools` 增,仅 active=shadow 时注册):
  `shadow_set_position(ticker, qty, avg_cost)`、`shadow_set_cash(amount)`、
  `shadow_remove_position(ticker)`、`shadow_record_fill(order_id, price)`
  (成交价回填:按新价重算该笔成交与持仓均价)。**全部走现有确认流**:
  终端阻塞 confirm;网页/Telegram 进 Pending 队列(新 kind=`shadow_edit`,
  卡片显示 before → after,批准即写)。导入本质是改"真钱账本",和改策略
  同级,必须人批。
- **CSV 导入**:Settings → Brokerage(shadow 区)上传,列 `ticker,qty,avg_cost`
  (+可选 `cash` 行);解析预览 → 确认 → 作为一条 `shadow_edit` 提案入队
  (同一条审批路径,避免两套写逻辑)。
- 每次写账本都落一条 `shadow_orders`(side=`import`/`adjust`)留痕,反思和
  复盘看得到"哪天用户手动改了账本"。

### ③ Active 账户切换

- `Settings.active_broker: str = "paper"`(`paper` | `shadow`),Brokerage tab
  一个单选 + 说明;保存即 `holder.rebuild()`,`build_components` 据此构造
  `AlpacaBroker` 或 `ShadowBroker`(**都构造**——影子账本的读路径和 Alpaca
  的行情路径并不互斥;但 `components.broker` 只指向 active 的那个)。
- **切换时的安全措施**:Pending 队列里未处理的订单提案是针对旧账户的——
  切换时这些 pending 订单行自动标记 `superseded`(note:"account switched
  to shadow")并通知用户;策略文件不变(策略是意图,两边通用),但规则的
  触发状态(armed/triggered)按账户隔离:`rule_states` 加 `broker_name`
  维度,否则 paper 里触发过的止损在 shadow 里永远不响。
- 哨兵、反思、digest 全部对 active 账户工作;journal 的 `trades` 行加
  `broker_name` 列(迁移,默认回填 `alpaca`),Dashboard/反思只看 active 的。

### ④ 通知文案

影子账户下所有订单通知文案变为**建议语气并要求动作**:
- hard 规则执行 → "📌 Shadow: recorded BUY 4.5 TSLA @ $332.01 — **place this
  order in your brokerage now**"(ntfy/邮件/Telegram 同款);
- soft 规则 → 现有 Approve/Reject 按钮/链接,批准后同上文案;
- 反思种子简报与 agent 系统提示里注明 "active account is a SHADOW ledger
  mirroring the user's real brokerage; orders are recorded, not routed —
  the user executes them manually"。

### ⑤ 界面

- Dashboard 顶部一个账户 chip:`PAPER · Alpaca` / `SHADOW · mirrors your
  brokerage`(点击跳 Settings 切换);权益卡、持仓表、曲线都是 active 账户的;
  影子模式下每行持仓多一列"价格截至"(行情失败时)。
- Strategies 页无变化(策略通用);Pending 页的 `shadow_edit` 卡片(before→after
  表);Reports/反思自然跟随。
- Settings → Brokerage:Active account 单选;shadow 区:当前账本摘要(现金、
  N 个持仓)、CSV 导入、"Reset shadow ledger"(危险按钮,二次确认,清空表)。

### ⑥ 安全不变量

- **零真实券商凭证**:影子账户不连任何外部账户,代码里不出现任何券商登录;
- 账本写入只经两条路:人工批准的 `shadow_edit` 提案,或经风控闸门的
  `submit_order`(它本身只在 hard 规则/人工批准后被调用)——与现有"agent 无
  直接写能力"模型完全一致;
- 切换账户不跨账户泄漏:pending 订单作废、rule_states 隔离、journal 打标;
- 风控闸门、审批队列、通知失败隔离、反思守卫:全部原样。

### ⑦ 降级

- 行情源挂 → 最后已知价估值 + 标注,`submit_order` 在无价时 **拒绝**(不能
  按未知价记账)并通知;
- 账本为空(刚切换未导入)→ Dashboard 显示引导:"Import your positions —
  tell the agent in Chat or upload a CSV";哨兵照跑(策略的仓位条件对空账本
  求值为 0);
- DB 写失败 → `submit_order` 抛 `ExecutionError`(现有执行器捕获路径)。

### ⑧ 非目标

- 连接任何真实券商 API(后续若有正规 API 的券商,是 Broker 再加一个适配器,
  与本 spec 正交);
- 两账户同时被 agent 服务/对比;
- 税务批次、股息、拆股自动处理(用户通过 `shadow_set_position` 手动修正,
  记录为已知限制);
- 期权/加密资产。

### ⑨ 测试要点

- `ShadowBroker` 纯账本逻辑:买/卖/碎股/超卖拒绝/加权均价/估值/权益落点/
  无价拒绝/最后已知价回退;
- 切换:pending 作废 + 通知、rule_states 隔离、journal 打标、rebuild 后
  components.broker 类型正确;
- 导入工具:终端 confirm / 网页入队 / 卡片 before→after / 批准写账本 / CSV
  解析预览;
- 文案:影子模式通知含"place this order"、chip、agent 系统提示注明;
- 哨兵/反思/digest 在 shadow 下端到端(ScriptedLLM);English-only。

## 实现顺序建议

账本表 + `ShadowBroker`(①)→ journal/rule_states 按账户隔离 + active 切换
(③)→ 导入工具与 `shadow_edit` 审批(②)→ 通知文案 + 系统提示(④)→
Dashboard/Settings 界面(⑤)→ 文档。
