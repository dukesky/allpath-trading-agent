# Phase 6 设计:每日反思循环(Reflection Loop)

日期:2026-08-10 · 状态:已与用户对齐,待实现

## 目标

每个交易日收盘后,agent 用完整的工具能力复盘当天:成交、规则触发、行情
相对每个策略假设的走位。产出三样东西:

1. **每日复盘报告** —— 存档在新的 Reports 页,ntfy 推 3-5 句摘要,邮件发全文;
2. **记忆教训** —— 复盘结论经现有 `memory_update` 受控路径写入
   lessons / 个股层,影响后续所有对话;
3. **策略修改建议** —— 当反思认定某策略假设与现实走样,生成修改草案进
   待确认队列。**绝不自动改**:批准动作永远是用户在 Pending 页点的。

用户明确要求:能力上限优先(方案二),Reports 页 + 推送是必备 feature。

## 非目标

- 盘后自动交易(反思会话没有下单工具,永远没有);
- 节假日历法(沿用现有 weekday+时段判断,已知局限);
- 周报/月报聚合(先跑通日报,聚合另立项);
- SSE 实时旁观反思过程(会话记录事后可回放,够用)。

## 方案取舍(记录)

- **选定:方案二 + 缰绳** —— 复用聊天 AgentSession 机器跑脚本化会话。
  上限最高:agent 自己决定查什么、可以追查疑点,不受预打包材料限制。
  先例:哨兵 ReviewAgent 就是"完整 agent 机器 + 轮次帽"的非交互运用。
- 否决:专用单轮 ReflectionAgent(测试最好但封死追查路径,上限低);
  模板报告(出不了判断,达不到目标 1 以外的任何一层)。

### 缰绳(不降上限,只防事故)

1. **轮次/预算帽**:反思会话 ≤ 12 轮工具调用,token 预算帽;到帽强制
   进入收尾提示,要求立即产出报告。帽值进 Settings(不上设置页,.env 可调)。
2. **种子简报**:确定性代码开场备好当日骨架(成交含真实成交价、触发与
   规则状态变化、持仓当日涨跌、待确认队列状态),全部 `fence_external`
   围栏。agent 从已知事实起步,轮次花在深挖上。
3. **独立会话**:`conversations.kind = "reflection"`,每日一条,不进用户
   聊天上下文;Reports 页可回放全程,每一步查了什么可审计。

## 组件设计

### ① 运行时机

挂进现有 `_maybe_run_daily`(收盘后、每 ET 交易日一次)。顺序:
digest → **反思** → 记忆整合。反思产出的教训当晚即被整合。三个任务
互不牵连:反思抛异常只损失当天报告,digest 与整合照跑(沿用该函数
既有的逐任务隔离模式)。`serve` 与 headless `run` 两个入口都要接。

### ② ReflectionSession

- 模型:`memory_model` 档(当前 Opus 5)——用户选择每交易日统一强模型;
- 系统提示:IDENTITY 基础上加反思专段(职责、报告结构要求、"建议而非
  行动"的立场);
- 工具面:全部现有只读工具(行情、K线、持仓、账户、journal 查询、记忆
  检索、策略读取)+ `memory_update`(走注入防护)+ 新工具
  `propose_strategy_revision`。**无下单、无确认类工具。**
- 收尾:最后一轮要求输出结构化报告(见 ③ reports 表字段);解析失败
  重试一次,再失败按降级处理。

### ③ 数据地基(前置任务)

1. **成交明细入 journal**(TODO 既有条目):`filled_qty`、
   `filled_avg_price` 列;Alpaca 提交回执 + 提交后单次回查填充;拿不到
   (市价单延迟成交等)留 NULL,复盘材料如实标"submitted, fill pending"。
   迁移走 `_MIGRATIONS`(ALTER TABLE 加列,兼容旧库)。
2. **`reports` 表**:`id, date(唯一), body, summary, conversation_id,
   model, tokens_used, created_at`。summary 即推送用的短摘要,是报告
   输出的一部分,不是二次生成。body 为结构化纯文本(报告提示词要求
   分节标题+要点列表,不要求 markdown)。

### ④ 策略修改建议链路

- `pending_reviews` 加 `kind TEXT DEFAULT 'order'`(`order` |
  `strategy_revision`),迁移加列,现有行为零变化;
- `propose_strategy_revision(strategy_id, new_yaml, rationale)` 工具:
  即时校验(parse_strategy_text 全套 + 禁改 id)→ 校验不过直接拒绝该
  工具调用(agent 可修正重试,计入轮次帽)→ 通过则入队,负载含新旧
  YAML、规则级 diff、rationale;
- Pending 页分块:订单待确认(现状)+ 策略修改建议;建议卡片显示 diff
  与理由;批准 → 重新校验 → 原子写(tmp+os.replace,现有机制)→ 版本
  快照(reason 注明来自当日反思)→ 通知;拒绝 → 关闭留痕。导航角标 =
  两类 pending 之和;
- 同一策略同日多条建议:允许,后批的以文件当前内容为基重新校验,
  校验不过则批准动作报错并保持 pending(用户可拒绝)。

### ⑤ Reports 页 + 推送

- 导航新增 Reports:日期倒序列表(日期、摘要首句、建议数徽标);
- 详情页:报告以转义纯文本渲染(`white-space: pre-wrap`,零新依赖,
  无 markdown 渲染器——报告本身按分节纯文本生成)、当日建议及其状态、
  "查看推理过程"链接 → 反思会话回放(只读);
- 推送:ntfy 发 summary(短),邮件发全文;两通道沿用 MultiNotifier 与
  失败隔离,推送失败不影响报告落库。

### ⑥ 降级与安全

- LLM 失败/超时/报告解析两次失败 → 当天无报告,reports 表记失败行
  (Reports 页如实显示"failed"),次日照常;
- 建议校验不过 → 丢该建议不丢报告;
- 周末/假日不跑(现有判断);进程当天多次重启不重复跑(依 reports 表
  date 唯一键幂等,替代内存 state);
- 安全不变量:反思无下单工具;写入仅 memory_update(防护)与建议队列
  (人批);所有外部材料围栏;报告页渲染走转义。

### ⑦ 测试

- ScriptedLLM 驱动完整 agent 环(ReviewAgent 测试模式复用):正常产出
  报告、建议入队、轮次帽强制收尾、解析失败降级;
- 迁移测试:旧库加列后旧行为不变;
- 批准链路:kind 分流、diff 渲染转义、批准写盘原子性、重复批准幂等;
- 页面:英文化(no-CJK)、报告列表/详情/回放、角标合并计数;
- 幂等:同日重复调用只跑一次。

## 实现顺序建议

数据地基(③)→ 工具与队列(④ 后端)→ ReflectionSession(②)→
调度接入(①)→ Reports 页与推送(⑤)→ Pending 页建议块(④ 前端)→
文档。
