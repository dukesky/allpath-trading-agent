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
- [ ] 成交明细（filled_qty/filled_avg_price）入 journal schema —— **Phase 6 复盘前设计**

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
- [ ] Web chat 的 draft_strategy 审批卡片——目前 `order_sink` 只覆盖 propose_order，
      strategy 草稿在 web 模式下完全无法保存（只如实告知用户改用终端
      `allpath-trade chat`），需要仿照 Pending 队列给 strategy 保存也做一条
      排队 + 批准的路径，而不是直接落盘（终审 Finding 3）
- [ ] 策略 YAML 在线编辑（当前只读，修改走聊天让 agent 起草）
- [ ] SSE 实时推送工具活动（当前为回合结束后整体刷新，见 `_chat_messages.html` 里的说明）
- [ ] `serve` 的 HTTPS / 反向代理部署文档
- [ ] 独立守护进程 `allpath-trade run` 不发送每日摘要邮件——`_send_daily_digest` 只挂在 `serve` 的 `build_jobs` 里，`cli.py` 的 `run` 分支的 `daily_job` 只跑 consolidation
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
