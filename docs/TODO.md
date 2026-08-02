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
- [ ] SettingsStore `quote_mode="never"` 对含空格/#/= 值的破坏问题 —— **Phase 5 前必修**（Web UI 会写任意值）

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
