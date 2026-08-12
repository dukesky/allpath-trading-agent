# Telegram 聊天通道设计

日期:2026-08-12 · 状态:待用户过目

## 目标

在 Telegram 里和 agent 双向聊天,与网页端共享同一条对话:同一个
ChatService、同一条 conversation 记录、同一套记忆与工具。网页端的往来
**全量镜像**到 Telegram(用户决定),让 Telegram 成为随身的完整聊天记录。

## 非目标(用户决定:只做双向聊天)

- Telegram 内审批/拒绝订单(审批走现有 Pending 页与一键审批链接);
- 哨兵通知/反思摘要进 Telegram(继续走 ntfy/邮件);
- headless `run` 进程带 poller(v1 只挂在 `serve` 上,记录为已知限制);
- webhook 模式(长轮询足够,且不要求公网暴露)。

## 架构

```
Telegram 云 <--长轮询 getUpdates/sendMessage(stdlib urllib,零新依赖)--> TelegramPoller 线程
                                                                            |
                                                    ChatService(与网页端同一实例、同一把轮锁)
                                                                            |
                                                        ConversationStore(kind='chat',同一条对话)
网页 /chat/send 完成一轮后 --> 镜像推送(fire-and-forget)--> sendMessage
```

## 组件设计

### ① 配置与配对

- `telegram_bot_token: str = ""` 进 Settings(SECRET_FIELDS,设置页
  write-only 字段,BotFather 生成);空 = 整个功能关闭,poller 不启动。
- 绑定的 chat id 存 `app_state`(key `telegram_chat_id`)——运行时状态,
  不是配置。**配对流程**:用户对 bot 发 `/start <web_token>`;poller 校验
  token(constant-time)→ 存 chat id → 回 "Paired. This chat now talks to
  your AllPath agent.";token 不对 → 不回复(不向陌生人确认 bot 活着)。
  重新配对 = 再发一次,覆盖旧 chat id(单用户)。
- 非配对 chat 的任何消息:静默忽略(记一行 stderr 计数,不回复)。
- 设置页显示配对状态(chat id 打码)+ 配对方法提示 + Unpair 按钮
  (清 app_state key,POST,不碰交易语义)。

### ② TelegramPoller(新 `allpath_trade/telegram.py`)

- `serve` 启动时若 token 非空则起一条 daemon 线程;随 app lifespan 干净
  退出(threading.Event 停止信号,长轮询超时后自查)。
- `getUpdates` 长轮询 timeout=50(socket 超时 55);offset 持久化到
  app_state(key `telegram_update_offset`),重启不重放旧消息。
- 错误退避:5s 起,双倍,封顶 60s;恢复后归零。poller 任何异常都不影响
  serve 主体(线程顶层 try/except + 循环继续)。
- 收到配对 chat 的文本 → `chat_service` 同步跑一轮(与网页端同一把锁,
  全局同时只有一轮)→ 回复 sendMessage。处理单线程串行:一轮跑完才拉
  下一批。**offset 在收到消息时立即推进并落库(至多一次语义)**——轮
  中途崩溃宁可丢这条消息(用户重发即可、损失可见),也不能重启后重放
  导致重复的下单提案(不可见且有害)。此取舍写死,不做配置。
- 发送中提示:收到消息先回 sendChatAction typing(轻量,失败无视)。

### ③ 消息格式与切分

- agent 回复经 `to_telegram_html()`(`web/markdown.py` 内新函数,复用
  转义优先纪律):**粗体/行内代码/代码块 → Telegram HTML 的 b/code/pre;
  标题行 → 粗体行;表格/列表 → 等宽 pre 保持对齐;其余全部转义为文本**。
  sendMessage parse_mode=HTML;发送失败(实体解析错误等)自动降级为
  纯文本重发一次。
- 超 4096 字符按段落边界切分多条。

### ④ 全量镜像(网页 → Telegram)

- 网页端每轮完成后(含确认提示、批准/拒绝的 system_note 回执),将
  `You (web): <用户消息>` 与 agent 回复推送到配对 chat。
- fire-and-forget:独立线程/池提交,失败只记一行 stderr,**绝不影响网页
  轮本身**;Telegram 来源的轮不回推(无回声循环——按消息来源标记)。
- 实现挂点:chat_service 完成轮的统一出口(网页路由与 poller 共用),
  按 source 参数分流镜像方向。

### ⑤ 安全不变量

- 单 chat 绑定;绑定需知道 web_token;未配对消息静默丢弃;
- bot token 走 SECRET_FIELDS 全套(write-only、掩码、永不回显/日志);
- Telegram 路径拿到的工具面 = 网页聊天完全一致(下单提案仍进待确认
  队列,人批不变);无新增写路径;
- 传给 Telegram 的内容只经我们自己的转义转换;收进来的文本按用户输入
  处理(与网页聊天输入同级,不围栏——这是配对用户本人)。

### ⑥ 降级

- token 未配置:poller 不启动,零开销;
- Telegram API 不可达:退避重试,网页聊天不受影响;镜像推送失败的消息不补发(记录为已知限制——完整记录以网页端为准);
- 配对丢失(app_state 清了):回到未配对状态,消息静默忽略,设置页
  显示未配对。

### ⑦ 测试

- 假传输(monkeypatch urlopen):配对流程(对/错 token、陌生 chat 静默、
  重配对覆盖);offset 持久化与重启不重放;退避序列;
- ChatService 集成:Telegram 轮与网页轮共锁、同一 conversation、镜像
  方向正确、无回声循环;
- `to_telegram_html`:敌意载荷全转义、4096 按段落边界切分、HTML 失败
  降级纯文本;
- 设置页:secret 字段掩码、配对状态、Unpair;English-only(bot 系统
  消息英文,agent 回复语言跟随用户)。

## 实现顺序

配置+配对存储(①)→ poller 与传输(②)→ 格式转换(③)→
ChatService 挂点与镜像(④)→ 设置页(①UI)→ 文档。
