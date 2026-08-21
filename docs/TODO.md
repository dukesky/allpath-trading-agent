# Future TODO

未来增强项的集中记录。每项标注建议的落地阶段；进入某 Phase 的 plan 时应把对应项吸收进去。

## Phase 2.5 — 规则表达式扩展

- [ ] `ma(N)` 移动均线（如 `price < ma(50)`），含 `ma(250)` 年线
- [ ] K线/OHLC 历史函数：`high(N)`、`low(N)`（近 N 日最高/最低）、`low(52w)` 等
- [ ] `drawdown_from_peak` 持仓期高点回撤%（需要维护持仓期峰值状态）
- [ ] `pct_change(N)` 近 N 日涨跌幅
- [ ] 成交量变量（如 `volume`、`avg_volume(N)`）
- [ ] 规则冷却期 / 边沿触发选项（作为"一次性触发"语义的可选补充，如网格式加仓）

## 调度与数据

- [ ] 美股节假日历法（当前仅按 ET 工作日 9:30–16:00 判断，节假日空跑无害但不精确）
- [ ] 数据源冗余：Tiingo（EOD）、Finnhub（新闻/情绪）、Alpaca data，yfinance 故障时自动切换
- [ ] **搜索升级**：web_search 从 DuckDuckGo（免费默认）扩展到更高质量的付费信息源——Tavily / Brave Search API（通用搜索，需 key）、Finnhub / Polygon news（金融专用新闻流）；接口已设计为可插拔，用户配了 key 即自动启用

## 券商

- [ ] Interactive Brokers 适配器（`ib_async`，配 Docker ib-gateway 文档）
- [ ] Tradier 适配器（免费沙盒，适合 CI）
- [ ] 限价单支持（当前仅市价单）
- [x] 成交明细（filled_qty/filled_avg_price）入 journal schema —— **Phase 6 已落地**（`store/journal.py` 写入/回填，`store/db.py` 迁移新增两列）

## 模型与校验

- [ ] `OrderIntent` 拒绝空/纯空白 ticker —— **Phase 3 前必修**（LLM JSON 输入正是这类脏输入的来源）
- [ ] 风控守门：equity ≤ 0 时仓位权重检查静默跳过的显式化处理

## 其他

- [ ] 组合级风控（多策略同时触发时的总敞口控制）
- [ ] 策略间资金分配冲突检测（多个策略的 target 合计超过 100%）
- [ ] 回测能力（用同一套规则求值器跑历史数据验证策略）

## Phase 3 终审遗留（小项）
- [ ] sentinel `_agent_review`：post-claim 阶段出现非 ExecutionError 异常时（如 journal 写失败）行留在 approved 且无通知——与 execution.py 的日志时序 seam 一并处理
- [ ] store/db `_migrate` 的 except OperationalError 会吞掉 "database is locked"——改为先查 PRAGMA table_info 或匹配错误消息
- [ ] draft_strategy 保存的 YAML 含 `id:` 字段（loader 会用文件名覆盖，无害但与手写文件风格不一致）
- [ ] chat REPL：assistant 空文本时打印空行 `agent> `

## Phase 4 终审遗留（小项）
- [ ] consolidator 每日日期跟踪为进程内状态，重启后当日重跑（与 marker 过滤配合后已是无害 no-op，仍值得持久化）
- [ ] 一字母 ticker 的 lessons 匹配已用词边界修复；更长期可给 lessons 加 frontmatter tickers 字段做精确匹配
- [ ] observations.recent() 大积压时取最旧 200 条——积压场景应改为取最新
- [ ] 上下文个股档案包含非 active 策略的 ticker（轻微膨胀，预算兜底）

## Phase 5 遗留
- [x] Web chat 的 draft_strategy 审批卡片——目前 `order_sink` 只覆盖 propose_order，
      strategy 草稿在 web 模式下完全无法保存（只如实告知用户改用终端
      `allpath-trade chat`），需要仿照 Pending 队列给 strategy 保存也做一条
      排队 + 批准的路径，而不是直接落盘（终审 Finding 3）——**chat-strategy-proposals
      分支已落地**：`draft_strategy` 在 web/Telegram 模式下现在复用 Phase 6 的
      reflection revision 流水线（`pending_reviews`，kind `strategy_revision`，
      `source="chat"`），新策略与已有策略的修订都排队等批准，applier 按 source
      分支放行 authorization/status 改动（reflection 仍冻结，chat 不冻结，因为
      后者代表用户自己的意图）；同一策略的第二份聊天草稿会自动 supersede 前一份；
      审批卡片显示提议者、新策略徽标与 auto/status 倒退警告。终端 `allpath-trade
      chat` 的阻塞式确认体验未改动。详见 `docs/superpowers/plans/
      2026-08-12-chat-strategy-proposals.md` 与 CHANGELOG。
- [ ] `QueueingOrderSink.propose`（`web/order_sink.py`）在 web 模式下把
      `propose_order` 排进 Pending 队列时不发通知——这是本轮 chat-strategy-proposals
      分支给策略提案新增通知管线时确认过的既有行为，不是这次改动引入的回归：
      在此之前订单提案也从未发过通知。建议后续复用同一条
      `notify.events.review_queued` + `approve_link` 管线，让排队的订单
      提案也能像现在的策略提案一样带通知与批准链接发出。
- [ ] 策略 YAML 在线编辑（当前只读，修改走聊天让 agent 起草）
- [ ] SSE 实时推送工具活动（当前为回合结束后整体刷新，见 `_chat_messages.html` 里的说明）
- [ ] `serve` 的 HTTPS / 反向代理部署文档
- [x] 独立守护进程 `allpath-trade run` 不发送每日摘要邮件——**ops-hardening round 已落地**，
      详见下面 Phase 6 遗留区块里同一条目的落地说明（两条重复记录了同一个问题，一并解决）
- [ ] 通知正文里插值的文本（规则 condition、执行 detail、agent 的 recommendation）未做 URL 清理——其中若混入裸链接，邮件客户端可能自动转成可点击链接，与"通知不含链接"的设计承诺相悖
- [ ] 一个永久挂起的 broker 会耗尽 `_broker_pool`（`dashboard.py` 的 4-worker 专用线程池）——耗尽之后仪表盘会一直显示"unavailable"，即使 broker 后来恢复也不会自愈，直到进程重启；且因为 `ThreadPoolExecutor` 的 worker 是非 daemon 线程，一次挂起的调用还会拖慢 `serve` 的干净关闭。根治办法是给 broker 的 HTTP 客户端加 socket 级别的超时，而不是只在应用层 `.result(timeout=...)`
- [ ] compaction 的 flush 钩子（`on_before_compact` 绑定到 `Consolidator.run_post_chat`）是在触发它的那个回合的 turn lock 内、同步跑一次记忆层 LLM 调用——长对话里某一回合会出现明显的延迟尖峰，理想情况应该异步/后台执行，不阻塞当前回合的响应

## Phase 5.5 遗留
- [ ] 每日沉淀（daily consolidation）读取网页对话的排水速率固定为每天最多 150 条过滤后的
      turn 行（`TURN_LINES_CAP`，`allpath_trade/memory/consolidate.py`），没有补跑循环——
      如果某一天产生的合格 turn 持续超过这个量，没消费完的部分只会顺延到第二天，长期高频
      对话场景下积压会越滚越大，没有机制能一次性追上
- [ ] `run_daily` 的"无事可沉淀"短路分支只检查 `events` 和过滤后的 `turn_lines` 是否都为
      空；如果这一批抓到的 turn 全部是被过滤掉的系统/工具消息（`eligible` 为空但 `turns`
      非空），短路会在推进 turn marker 之前就直接返回"nothing to consolidate"，导致这批
      已经读过的 ineligible turn 下次仍会被重新抓取——无害但白做一遍，且与 `_turn_lines`
      自己文档字符串里"只有工具/系统消息的一批也必须清空队列"的承诺不一致
- [ ] ntfy.sh 上的公开主题目前仅靠主题名本身的不可猜测性保护（设置页提示与
      `.env.example` 里已提醒使用长且随机的主题名），通知正文又带 ticker、买卖方向、拒单
      详情——值得后续给 ntfy 通道加认证头（access token / Bearer）支持，而不是只依赖主题名
      保密

## Phase 6 遗留
- [x] `allpath_trade/llm/` 下两个客户端（`openai_compat.py` 的 `OpenAICompatClient`、
      `anthropic_client.py` 的 `AnthropicClient`）没有给底层 SDK 客户端传显式的请求超时——
      **ops-hardening round 已落地**：新增 `Settings.llm_timeout_seconds`（默认 180 秒，
      `.env` only，同 `REFLECTION_MAX_ITERS` 的策略），两个客户端构造函数都接收 `timeout=`
      参数并透传给底层 SDK 构造（`anthropic.Anthropic(..., timeout=...)` /
      `OpenAI(..., timeout=...)`），`llm/factory.py` 的 `build_llm` 对三档模型统一传入。
      SDK 超时异常（`anthropic.APITimeoutError` / `openai.APITimeoutError`）本身就是
      `Exception` 子类，`complete()` 已有的 `except Exception` 会照常把它包成 `LLMError`，
      `agent/loop.py` 的 `except LLMError` 继续按原有 `(llm error: ...)` 路径处理，
      未额外改动。broker 超时（本文件下面另一条）与 yfinance 超时仍待后续处理，是同一类
      问题的另外两处实例。
- [ ] `propose_strategy_revision`（`agent/reflection_tools.py`）修复一个已损坏（无法解析）
      的策略文件时，因为当前文件解析失败而拿不到 `current_doc.version` 做比较，退化为只要求
      `doc.version` 为正整数——如果该策略在 `strategy_versions` 表里已有更高版本号的历史
      快照，这次 v1 修复提案被接受后会排到 `StrategyStore.versions()`（`ORDER BY version
      DESC`）历史列表的最下面，审计顺序与实际时间顺序不符。修复思路：这种"当前文件不可解析"
      的分支应改为比较 `max(version)`（从 `strategy_versions` 表查，而不是从当前文件解析），
      而不是只检查 `> 0`
- [x] `allpath-trade run`（`cli.py` 的无 web 界面守护进程）的 daily job 相比 `serve` 的
      `build_jobs` 少两样：没有每日摘要邮件，且 consolidation 分支没有 `daily_consolidation`
      开关判断——**ops-hardening round 已落地**：把两条路径共用的每日序列（digest ->
      reflection -> consolidation，逐步独立 try/except）抽成 `scheduler.run_daily_jobs
      (components)` 一个函数，`build_jobs` 和 `cli.py` 的 `run` 分支现在都调用它，不会再
      各自维护一份、逐渐漂移。`cli.py` 不再有单独的 `daily()` 闭包。
- [ ] `reports.tokens_used` 列（`store/db.py`）目前恒为 0：`LLMClient`（`llm/base.py`）的
      接口不暴露任何 token 用量统计，`Reflector._run`（`reflect.py`）如实记 0 而不是伪造数字。
      等某个客户端接入用量返回后再把这列接上真实值
- [ ] 一次失败的 reflection（LLM 报错、corrective turn 后仍解析不出 REPORT/SUMMARY）会以
      `status="failed"` 写入 `reports` 表那一天的行，但 `Reflector.run_daily`
      （`reflect.py`）的幂等检查只看 `reports.exists(et_date)`，不区分成功/失败——同一个 ET
      日期不会重试，当天再触发只会返回 "already ran"。这是有意为之（spec §⑥ 只要求失败可见，
      不要求同日自愈），记录在此供后续讨论是否要加同日重试
- [ ] （F2 修复的遗留权衡）审批一条改写了触发规则（同一 rule id）的 reflection revision
      后，`apply_revision_factory` 仍然逐字写入 YAML、从不碰 `rule_states`——已触发
      （TRIGGERED）的规则在 approve 之后依然是 TRIGGERED，条件不会再被评估。本轮已经在
      web 审批流程（`web/routes/reviews.py` 的 strategy_revision 分支）和 CLI
      （`cli.py` 的 `cmd_reviews`）里加了提示：`StrategyStore.rearm_warning` 会在
      flash 通知里点名"某规则仍处于 triggered/disabled 状态，需要去策略页手动 re-arm"。
      这是刻意选择的设计——绝不自动 re-arm：如果自动重新武装，可能对一个已经卖出的仓位
      重新触发止损单。之所以只记提示而不是自动化，是因为"该规则是否还需要盯"这件事只有
      用户自己知道；记录在此供后续讨论是否要在 revision 卡片上加一个"顺便 re-arm"的勾选项
- [ ] （F4 修复的遗留缺口）`Reflector._positions_with_change`（`reflect.py`）已经加了
      `QUOTES_BUDGET_SECONDS`（10 秒）的整体截止时间，超时的持仓直接渲染 `n/a`，不再让一次
      挂起的行情调用拖住整条每日链路——但这只是在调用方加了一层"放弃等待"的保护，
      `data/yf.py` 底层的 `yfinance` 调用（`get_quote`/`get_bars`，`Ticker(...).history(...)`
      等）本身仍然没有传任何 `timeout=` 参数，那次已经发起的请求依然会在后台无限挂着（同一类
      问题见上面第 60 行 broker 超时、第 64 行 LLM 客户端超时那两条——`QUOTES_BUDGET_SECONDS`
      只是把这类问题在 reflection 这个调用点"止血"，没有像那两条一样从源头根治）
- [ ] `DAILY_REFLECTION`（`config.py` 的 `daily_reflection` 字段）目前只能通过 `.env`
      配置，Settings 页面（`web/templates/settings.html`）只给 `daily_consolidation`
      和 `consolidate_after_chat` 做了勾选框，没有对应的 reflection 开关——想要临时关掉
      每日 reflection 得改 `.env` 再重启进程，而不能像另外两个每日任务一样在网页上直接切换
- [x] 每日摘要邮件（`_send_daily_digest`，`scheduler.py`）和每日 reflection 共用
      `build_jobs`/`run_daemon` 里同一个 `_maybe_run_daily` 一天一次的门控，但这个门控只是
      `state = {"last_daily": ...}` 的进程内变量，不落盘——同一个 ET 日期内如果进程重启
      会对当天再发一封重复摘要邮件——**ops-hardening round 已落地**：`_send_daily_digest`
      现在自己在 `app_state`（key `digest_last_date`）上做 ET 日期级别的幂等检查，与
      reflection 的 `reports.exists(et_date)`、consolidation 自己的 turn marker 是同一类
      持久化 seam，不改动 `_maybe_run_daily` 本身。

## Fill-honesty round 遗留（M6）
- [ ] 每日 reflection 简报和（若后续加上）digest 目前都按**提交日**给交易分桶，不是**成交
      日**——`reflect.py` 的 `Reflector._trades_today` 用 `ts_to_et_date(r["ts"])` 过滤
      "今天"的交易，而 `r["ts"]` 是 `TradeJournal.record` 写入时的下单时间戳，不是
      `filled_at`；`scheduler.py` 里 `TradeJournal.trades_today()`（供风控日交易上限计数）
      同样按 `ts`（提交时间）分桶，是同一类问题的另一处实例。DAY 单在收盘后提交、下一个开盘日
      才成交是本轮修复的动机场景（见 `agent/context.py` 的 MARKET_MECHANICS_NOTE）——按这个
      口径，一笔周五收盘后提交、周一开盘才成交的订单，会被算进"周五"的简报，而它真正执行、
      产生持仓变化的那个交易日（周一）的简报里却完全不出现。修复思路：`_trades_today` 应改为
      优先按 `filled_at`（若已回填）分桶，仅当 `filled_at` 缺失时退回 `ts`；`trades_today()`
      是否要同步改口径需要单独判断，因为它喂给的是风控当日下单笔数上限，按"下单日"计数可能才是
      本来就想要的语义，不能直接照搬简报那边的修法

## Telegram 频道（Task 3）遗留

- [ ] `/start <web_token>` 配对目前直接复用 Settings 页面上的长期 WEB_TOKEN 本身作为配对口令——
      能用就必须泄露给 Telegram 服务器（token 会经由 `/start` 消息文本进出 Telegram 的
      服务端）、且这个口令本身还兼职网页会话认证，混用两种用途。已经加了两层缓解（配对成功后
      best-effort 删除含 token 的 `/start` 消息；失败配对与陌生人共用同一条 stderr 计数行，
      不单独回复攻击者），但更干净的设计是引入独立的**一次性配对码**：网页 Settings 页面
      生成一个短时效（如 10 分钟）、一次性、与 WEB_TOKEN 完全解耦的随机码，`/start <配对码>`
      验证通过后立即失效，不再复用同一个长期口令做两件事。这个改动涉及新增一张状态表
      （配对码 + 过期时间 + 是否已用）和 Settings 页面的一处 UI，本轮不做，留待后续 Phase
      纳入 plan 时再落地。

## Telegram 频道（Task 5）遗留

- [ ] **仅 `serve` 可用**：Telegram poller 只在 `allpath-trade serve`（web 界面）
      的 lifespan 里起线程；无界面的 `allpath-trade run` 守护进程没有 Telegram
      频道——同上面 Task 3 遗留段落里记录的同一个限制，这里再记一次是因为它是
      Task 5（serve 接线）自己交付清单里明确列出的已知限制，不是新发现。
- [ ] **镜像推送失败不补发**：网页轮完成后镜像到 Telegram 失败（网络问题、bot
      被拉黑、chat 已不存在等），`_send_mirror_text`（`web/app.py`）只记一行
      scrubbed stderr，不重试、不补发——完整记录以网页端 `ConversationStore`
      为准，Telegram 端只是"尽力而为"的第二份拷贝，不是权威记录。**whole-branch
      review round（Finding 3）已在此基础上补了一层相关但不同的行为**：
      `_MirrorQueue`（`web/app.py`，替代原先从不关闭的模块级 `ThreadPoolExecutor`
      单例）现在是有界队列（上限 50 条），持续积压时新消息会挤掉队首最旧的一条
      而不是无限增长——同一条"尽力而为、不是权威记录"的哲学延伸到了积压场景，
      不是新引入的取舍。`_stop_telegram` 现在也会真正关闭这个队列
      （`shutdown(wait=False, cancel_futures=True)`）并清空 mirror 钩子，
      不会再让一个挂死的 Telegram 端点拖慢 `serve` 的干净关闭。
- [ ] **至多一次（at-most-once）消息语义**：`TelegramPoller.poll_once`
      （`telegram.py`）收到更新后立即推进并落库 offset，早于该消息实际处理
      完成——中途崩溃宁可丢这条用户消息（用户重发即可，损失可见），也不重启后
      重放（重放会导致一次不可见但有害的重复下单提案）。此取舍是设计里写死的，
      不做配置项；这里记录是为了让"消息可能丢一条"这件事在文档里对用户可见，
      不只是代码注释里的隐性约定。
- [x] **重置 web token 立即生效于配对**：whole-branch review round（Finding 2，
      安全相关）修复前，这条曾经记录反了——`TelegramPoller` 在构造时把
      `web_token`/`app_state` 各拷贝一份快照，`/settings/reset-token` 之后
      快照永远不会更新（除非重启进程），实际效果是**旧 token 泄漏后依然能在
      Telegram 里配对**（"已撤销"形同虚设），而重置后的新 token 反而在配对时
      被当成陌生人消息静默拒绝。现在 `TelegramPoller` 改为持有 `ComponentHolder`，
      每次比较都通过 `holder.get().settings.web_token` 实时读取（`app_state`
      同理，见 `telegram.py` 的 `TelegramPoller` 类文档字符串）——重置生效后，
      旧 token 立即被拒绝，新 token 立即可用，都不需要重启进程。已有的配对
      状态（chat id + user id）仍然与两个 token 无关，不会因为改 token 而失效，
      这一点不变。
- [ ] **一次性配对码**：见上面 Task 3 遗留段落——`/start` 配对口令目前直接
      复用长期 WEB_TOKEN，已有两层缓解（配对成功后 best-effort 删除含 token
      的消息、失败配对静默丢弃不回应攻击者），更干净的独立一次性配对码设计
      本轮仍未落地，留在同一条遗留里跟踪，不重复开新条目。

## shadow-dual-active（Task 7）已知限制

双活账户体系落地后仍然存在的、有意选择不做或暂不做的限制，随实现一并记录，
而不是散落在各任务的 commit message 里。

- [ ] **股息/拆股/税批次不自动处理**：shadow 账本（`broker/shadow.py`）只认
      买卖两种成交，没有股息入账、拆股调整持仓与均价、或税务批次（lot）追踪的
      概念——真实券商发生这些事件后，用户需要自己算出调整后的 qty/avg_cost，
      通过 Chat 或 Settings → Brokerage → Shadow 的 `set_position` 提案手动
      改一次账本。spec §⑧ 就是这样定的："股息/拆股/税批次:手动修正,记为限制"，
      本轮 Task 7 只是把它从设计文档搬进这份面向用户的已知限制清单——不是新
      发现的缺口。期权/加密同理，完全不支持。
- [ ] **没有真实券商 API**：shadow 账本从不连接、也从不需要连接你真实的
      brokerage——它是本地记账,不是第三方集成。这意味着账本不会自动同步你
      真实账户的持仓变化;每一次买卖都要你自己去真实账户执行,再回来告诉
      agent(或用 CSV 重新导入全量持仓)。这是刻意的产品选择(spec 的"影子
      账本,不接真实凭证"不变量),不是尚待补的集成——记在这里是为了让用户在
      读文档时就知道,而不是用了才发现。
- [ ] **反思(reflection)夜间 LLM 成本**：`_run_account_daily`
      (`scheduler.py`)按账户门控——只有当天该账户存在至少一个 active
      策略,reflection 才会跑一次 LLM 调用(§③的成本闸门,空 shadow 账本不会
      产生任何反思花费)。但一旦 Paper 和 Shadow 两边都挂了 active 策略,
      两边各自跑一次独立的 reflection 会话,夜间 Opus/LLM 总花费大约是只跑
      单账户时的**两倍**——Settings → Usage 面板上能看到真实数字,这里只是
      提前把这个数量级写清楚,免得用户第一次看账单才意外发现。
- [ ] **CLI `--account` 在需要完整 bundle 的命令上仍然要求 Alpaca 凭证**：
      `cli.py` 的 `needs_broker` 判断里,`status`/`check`/`run`/`chat`/
      `serve` 这几个命令只要被调用就会走 `build_components`,而它一次性
      构建**两个账户**的 bundle(Task 4 的双活设计——一个进程,两条流水线
      始终都在),所以哪怕这次 `--account shadow` 只想看 shadow 一侧,
      Paper 侧 AlpacaBroker 的构造依然会失败、要求 `.env` 里有真实(哪怕是
      沙盒)Alpaca key——`--account shadow status` 在没配 Alpaca 凭证的
      全新安装上不会工作。真正不需要 Alpaca 凭证的是压根不经过
      `build_components` 的只读命令:`strategies`、`rearm`、纯粹的
      `reviews list`/`reject`。修复思路是让 `build_components` 支持"只建
      单个账户的 bundle",但那会改变它当前"一次调用两个账户永远同时存在"的
      不变量,本轮不做,留给后续如果这个限制真的造成困扰时再评估。
- [ ] **CSV 导入有硬上限**：Settings → Brokerage → Shadow 的持仓 CSV 导入
      (`agent/shadow_tools.py`)限制文件不超过 1,000,000 字节、不超过 2,000
      行(`_MAX_CSV_BYTES`/`_MAX_CSV_ROWS`)——超限直接拒绝、不做分批导入。
      对绝大多数个人投资组合(几十到几百个持仓)完全够用;真遇到需要导入
      上千行的场景(比如某种批量测试数据),现在的唯一办法是拆成多次
      导入(每次都是独立的批准提案)。

## chat-strategy-proposals review round 遗留

- [ ] **`pending_reviews.conversation_id` 是只写字段**：`add_strategy_revision`
      （`store/reviews.py`）把发起这条策略草稿的会话 id 存进这一列，但代码库里
      没有任何地方把它读回来——不做展示（reviews/chat 页面都不渲染它），也不
      做查询关联（没有按 conversation_id 反查草稿的路径）。目前唯一的价值是
      "审计溯源"：真出问题时可以直接查 sqlite，靠这一列把一条排队中/已解决
      的策略草稿对回它出自哪次对话。如实记录这一点，而不是假装它驱动着什么
      现有行为——后续如果要让它派上用场，大概率是给 `/reviews` 或
      `/conversations` 页面加一条"这条草稿来自这次对话"的反向链接。
- [ ] **网页发起的策略草稿会触发两条内容不同、互不知晓的手机提醒**：一条聊天
      草稿从网页排队后，用户会几乎同时收到两条通知——(1) 这轮
      chat-strategy-proposals 新增的 `_notify_chat_draft_queued`
      （`agent/action_tools.py`）经 email/ntfy 发出的"review queued"格式化
      提醒（`notify.events.review_queued`，带批准链接）；(2) Task 5 既有的
      Telegram 镜像（`web/app.py` 的 `_mirror_to_telegram`）把这轮聊天回合的
      助手回复原文（"Draft queued for your approval as #N ..."）转发到
      Telegram。两条走的是完全独立的通知管线，互相不知道对方的存在，文案也不
      一致（一个是结构化的审批通知，一个是聊天回复的逐字转发）——不是 bug，
      本轮 `draft_strategy` 新接上的是 email/ntfy 那一条腿，TG 镜像那一条腿
      本来就对每个聊天回合无差别生效（见上面 Task 5 遗留段落）；两者叠在同一次
      策略草稿排队事件上才第一次让"一次事件、两条不同文案的提醒"这个组合
      出现（订单提案 `propose_order` 目前还没接 email/ntfy 那一条腿——见上面
      Phase 5 遗留段落同一发现——所以暂时只有单条 TG 镜像）。如实记录，供后续
      讨论是否要合并成一条、或至少让两条文案对齐。
