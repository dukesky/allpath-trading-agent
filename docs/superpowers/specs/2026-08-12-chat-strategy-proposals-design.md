# 网页/Telegram 聊天里保存策略:草稿即提案

日期:2026-08-12 · 状态:spec 待用户过目(用户先实测产品几天,不急于开工)

## 要解决的问题

`draft_strategy` 工具在网页/Telegram 模式下做完全部校验后**明确放弃**
(`action_tools.py:43-57`):返回"去终端保存"。根因是终端聊天有阻塞式
`confirm()`(打印 diff 等你敲 y/n),而网页/Telegram 是请求-响应模型,
工具调用中途无法停下来等人,所以网页模式注入 `confirm=lambda _: False`。

后果:手机(Telegram)上根本没有终端;agent 在对话里生成的 YAML 只是文字,
不落地就丢;反思提的策略建议能一键批,你自己让 agent 改的却不能——
两条改策略的路径体验不对称。这是最后一个把用户逼回终端的场景。

## 思路:策略草稿 = 一种待确认提案

Phase 6 已建好通用链路:提案进 `pending_reviews`(kind=`strategy_revision`,
带 old/new YAML + 理由)→ Pending 页左右 diff 卡片(+ 通知里的一键审批
链接)→ 用户批准 → 字节级基线校验 → 原子写 + 版本快照。**链路不关心提案
人是谁。**

做法:网页/Telegram 模式下 `draft_strategy` 把草稿**投进这条队列**;
终端模式保持阻塞确认不变。**安全模型不变**:agent 依旧没有直接写策略
文件的能力,只是多了一个提案来源;审批动作永远是人在有登录保护的页面
(或持一次性令牌的确认页)点的。

## 非目标

- 不改终端 `allpath-trade chat` 的保存体验(阻塞 confirm 更顺手);
- 不做"批准后自动激活":新建的策略默认 `status: draft`(不监控),用户
  在策略页点 Activate——draft-not-monitored 徽标已经会提醒;
- 不做多草稿并存/分支;
- 不改 Pending 页信息架构(复用现有卡片,只加提案人标识 + 新建标识)。

## 关键设计决定

### ① 两级守卫,同一条链路

反思提案有**故意的冻结**:不能改 `authorization`/`status`(反思是自动
跑的,绝不能自己把策略切成 `auto` 或停用)。**用户在聊天里发起的改动
不受这个冻结**——"把 TSM 改成 active"、"新建一个 NVDA 策略"都是用户
本人的意图。因此:

- `pending_reviews` 已有 `source` 列(`sentinel`/`reflection`/…);聊天
  提案 `source="chat"`,附 `conversation_id`(已有列)——**提案人 = 是否
  冻结的判据**;
- 应用器(applier)按 `source` 分流:`reflection` 走现有全部守卫;`chat`
  跳过 authorization/status 冻结,**但保留**:id 不可变、strategies 目录
  内、基线字节比对(文件在提案后没被改过)、版本单调、YAML 全套校验;
- 切到 `authorization: auto` 的聊天提案:允许入队,但审批卡片和确认页
  **醒目告警**("批准后 hard 规则可自动下单"),与策略页 Activate 的
  二次确认同一措辞级别。审批仍是人点的——这就是安全边界。

### ② 新建策略

- 目前链路只处理"修改已有"。新建时基线为空:`old_yaml=""`,应用时的
  基线校验变为"文件**不存在**"(存在 = 有人在提案后建了同名文件 →
  失败留 pending,与修改的字节比对同一语义);
- 卡片/确认页显示 "New strategy" 徽标,diff 左栏空、右栏全绿;
- 新建的 `status` 由用户/agent 在 YAML 里写;缺省 draft。卡片上若为
  draft 提示"批准后需在策略页 Activate 才会被监控"。

### ③ 同一策略的重复提案

聊天里改两轮很常见("再把止损调紧点")。规则:同一 `strategy_id` 已有
**pending 的 chat 提案** → 新提案**替换**它(旧行 status=`superseded`,
留痕,resolution_note 指向新 id);反思提案不受聊天提案影响、也不替换
聊天提案(不同提案人各自独立,都留给用户裁决)。工具返回明确告诉 agent
"替换了 #N"。

### ④ 工具与即时反馈

- `draft_strategy` 在 web/Telegram 模式(现有 `order_sink is not None`
  的判据,或显式 `mode` 参数——实现时二选一,一行理由)入队后返回:
  "Draft queued for your approval as #N — open Pending (or tap the link
  in the notification). It will not take effect until you approve it."
  agent 据此告诉用户;
- 入队即发通知(现有 `review_queued` 事件族,带 Phase 6 的一次性审批
  链接;文案区分 order / revision / new-strategy);
- Telegram 模式下,agent 的回复自然经镜像回到 Telegram——不需要额外
  Telegram 特有逻辑。

### ⑤ 版本号

聊天提案沿用 `draft_strategy` 现有的"版本 ≤ 当前 → 自动 +1"惯例(用户
不该关心版本号),而反思提案要求严格递增(自动 agent 必须显式声明)——
两条路径各自保持既有行为,只在入队前把最终版本写进 new_yaml。

## 安全不变量(不变)

- agent 无直接写策略文件能力;唯一写路径是人工批准后的应用器;
- 审批门:字节级基线(或"不存在")、版本单调、id/目录守卫、YAML 全套
  校验;`auto` 切换必须经过醒目告警的人工点击;
- Telegram 路径 = 网页路径,不新增能力;
- 反思提案守卫**分毫不减**——本 spec 只为 `source="chat"` 放开冻结。

## 降级

- 入队失败(DB 错)→ 工具返回错误字符串,agent 告知用户,不丢 YAML
  (仍在对话里);
- 通知失败不影响入队;
- 应用失败(基线变了)→ 留 pending + 现有提示,用户重新让 agent 起草。

## 测试要点

- 工具:web 模式入队(不写文件)、终端模式行为不变、新建 vs 修改的
  基线记录、替换 pending 同 id、返回文案;
- 应用器:chat 提案可改 authorization/status,reflection 提案仍被拒;
  新建的"文件不存在"基线校验;版本处理两路径各自正确;
- UI:卡片 New/来源徽标、auto 告警文案、确认页同样;English-only;
- 通知:三种文案、链接存在(base_url 配置时);
- 端到端:网页聊天起草 → 队列 → 批准 → 文件落地 + 版本快照 + 策略页
  可见;Telegram 起草同样(通过共享 ChatService 自然覆盖)。
