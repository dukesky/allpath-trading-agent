# Launch kit — AllPath Trading Agent

Drafts for the first public push. Read every piece before posting; each is written for its venue's norms and expects you to adjust in your own voice. Rule for all of them: **never claim or imply returns.** The story is the approval gate, the memory, and the reflection loop — not alpha.

Links to use everywhere:
- Repo: https://github.com/dukesky/allpath-trading-agent
- Site: https://trading.all-path.com

---

## 0. GitHub repo setup (do this before anything else)

**Social preview**: repo → Settings → General → Social preview → upload `docs/images/social-preview.png` (1280×640, already generated).

**About box** (repo → ⚙ next to About):
- Description: `Self-hosted LLM trading agent that proposes — you approve. YAML strategies, hourly sentinel, chat with memory (web + Telegram), nightly reflection. Paper trading by default.`
- Website: `https://trading.all-path.com`
- Topics (pick ≤ 20, these are the ones people actually search):
  `trading-agent` `llm-agent` `ai-agent` `algorithmic-trading` `paper-trading` `alpaca` `alpaca-trading-api` `self-hosted` `human-in-the-loop` `openrouter` `anthropic` `openai` `python` `fastapi` `sqlite` `telegram-bot` `agent-memory` `portfolio-management` `quant` `trading-bot`

**Repo hygiene**:
- Enable Discussions (Settings → Features) — questions land there instead of Issues.
- Add 3–5 `good first issue` items (ideas: holiday calendar for `is_market_hours`; a second broker adapter behind the `Broker` ABC; weekly report aggregation; a `docker-compose.yml`; a one-shot Telegram pairing code). Small, real, scoped.
- Pin the "known limitations" section in README so honesty is the first thing a skeptic sees.
- Tag a release `v0.1.0` with the CHANGELOG as notes — HN readers click Releases.

**Demo video** (60–90 s, one take, phone or screen): Telegram → "tighten NVDA stop to 150" → phone gets a notification → tap approve link → confirm page shows the diff → approve → Strategies page shows v4. That single clip is reused by every post below.

---

## 1. Show HN

**Title** (≤ 80 chars, no superlatives):
`Show HN: A self-hosted trading agent that can only propose – you approve every order`

**Text** (post body):

> I've been building a trading agent for my own paper account and decided to open-source it. The design constraint that shaped everything: **the agent has no tool that places an order or writes a strategy file.** It can research, remember, and propose; a human clicks Approve — in the browser, from a one-time link in a phone notification, or from Telegram.
>
> What it does:
>
> - **Strategies are YAML** — a thesis, a target weight, and rules like `price < 140 → sell all`. A sentinel evaluates active strategies every hour during market hours; hard rules can execute, soft rules queue for review with the agent's research attached.
> - **A chat agent with memory** (web + Telegram, same conversation). Four curated memory layers — profile, strategies, per-stock notes, lessons — consolidated nightly by a separate model tier, with an injection guard on anything external before it can be written.
> - **A nightly reflection loop** — after close, a bounded agent session (12 tool calls) re-reads every strategy against the day's fills and prices, writes lessons to memory, and proposes strategy revisions as side-by-side diffs. Those wait for approval too.
> - **Guards in code, not prompts** — position caps and daily limits are deterministic Python; approving a strategy revision does a byte-exact check that the file hasn't moved since the proposal; a reflection proposal can't flip a strategy to auto-execute or take it out of monitoring; a chat proposal that would do either gets a loud warning and a confirm dialog.
>
> Stack: Python, SQLite, FastAPI + htmx (no JS framework), Alpaca paper by default, any model via OpenRouter / OpenAI / Anthropic. Runs on your machine; nothing leaves it except the API calls you configured. ~1,300 tests.
>
> Things I learned building it that surprised me: the sentinel silently ignored every strategy I'd written for a week because they were all `status: draft` and nothing on the dashboard said so (fixed with loud "not monitored" badges); a stale strategy proposal could revert an already-approved stop-loss tightening until the applier compared against the recorded base byte-for-byte; and a Sunday-evening market order that Alpaca queued to Monday's open exposed that my journal only ever recorded *submission* time — the agent then confidently mislabeled it as the fill time.
>
> Honest limitations: no holiday calendar yet; one broker (Alpaca); paper trading is the default and I run it that way; it's alpha and I use it daily but it's not investment advice or a return-generating machine. What I'd love feedback on: the memory/reflection design, and whether the approval-gate model feels right to people who've built agents that touch money.
>
> Site with screenshots and a 5-minute quick start: https://trading.all-path.com
> Repo: https://github.com/dukesky/allpath-trading-agent

**Timing**: Tue–Thu, 8–10am US Eastern. Stay in the thread for the first 3 hours; answer every substantive comment; concede real critiques plainly.

**Comments to pre-write answers for**: "why not just use a broker's conditional orders?" (memory + reflection + natural-language strategy authoring, and the sentinel evaluates portfolio-level conditions like weight vs target); "isn't an LLM near money reckless?" (that's exactly why it can't touch money — the whole design is the answer); "why SQLite/htmx?" (single-user self-hosted; boring is a feature); "live trading?" (deliberate `.env` change, not a checkbox; I don't recommend it yet).

---

## 2. Reddit — three venue-specific versions

Space them out (2–4 days apart). Read each subreddit's rules the day you post; r/algotrading in particular removes anything that smells like promotion — lead with the engineering, link at the end.

### r/algotrading
**Title**: `Open-sourced my human-in-the-loop trading agent: LLM proposes, I approve, deterministic risk gate executes (Alpaca paper)`

> Sharing the architecture more than the product, since this sub cares about the plumbing.
>
> The core rule: the LLM never has an order-placing tool. Flow is `YAML rule fires → agent researches (chart, memory, thesis) → proposal queued with a diff → human approves (web/phone link/Telegram) → deterministic risk gate (position caps, daily limits) → broker`. Hard rules can auto-execute; soft rules always queue.
>
> Two things that might interest people here:
> 1. **Nightly reflection** — a bounded agent session re-reads each strategy against the day's *actual fills* (I record `filled_at`/`filled_avg_price` and re-poll pending orders each sentinel tick, so a Sunday market order that fills Monday 9:34 shows up honestly), and proposes rule changes as diffs. Approval does a byte-exact base check so a stale proposal can't undo a newer approved change.
> 2. **The sentinel evaluates portfolio conditions**, not just price: `price < 170 and position_weight < target_weight` — which is what stopped a momentum-add rule from firing forever once I was at target.
>
> Python/SQLite/FastAPI, ~1300 tests, MIT. Paper by default. Not claiming returns — this is infrastructure for a discretionary process, not a strategy.
> Repo + screenshots: https://github.com/dukesky/allpath-trading-agent

### r/LocalLLaMA
**Title**: `Self-hosted trading agent with a memory system + nightly reflection loop — BYO model (OpenRouter/OpenAI/Anthropic), never places orders itself`

> Built for my own paper account, open-sourcing it because the agent-architecture parts turned out more interesting than the trading parts.
>
> - **Three model tiers** you set independently: chat, hourly review (cheap/fast), and memory consolidation (strongest). All via OpenRouter, or OpenAI/Anthropic direct. Timeouts are enforced at the client so a hung call can't wedge the nightly chain.
> - **Memory** is four markdown layers (profile / strategies / per-stock / lessons) that the agent writes through a guarded tool (injection scan on external text) and a nightly consolidator distills. Web and terminal and Telegram chats all feed the same store.
> - **Reflection** is a real agent session — full read toolset, 12-call cap, corrective turn capped at 1 — that outputs a structured report + proposes strategy revisions. Its transcript is replayable in the UI so you can audit what it looked at.
> - **Safety by construction**: no order tool, no direct file-write tool; every write goes through a human approval queue; a reflection proposal is frozen from changing authorization/status. All prompts and tool results are fenced.
>
> Everything runs locally: SQLite + FastAPI + htmx, no JS framework, no cloud. Would love feedback specifically on the memory consolidation and reflection design from people who've built long-running agents.
> https://github.com/dukesky/allpath-trading-agent

### r/selfhosted
**Title**: `AllPath Trading Agent — self-hosted, human-in-the-loop LLM trading agent (paper by default, Alpaca, Telegram)`

> Single Python process, SQLite, one `.env`. `uv sync && uv run allpath-trade serve` and you have a token-gated web UI on your LAN; pair a Telegram bot with `/start <token>` and it's on your phone. Notifications via ntfy (self-hostable) or email; approve links are one-time tokens.
>
> It watches YAML strategies hourly, lets you chat with an agent that remembers your goals, and writes a nightly reflection report — but it **cannot place an order or change a strategy without you clicking Approve.** Paper trading by default; nothing leaves your box but the broker/LLM calls you configured.
>
> No Docker image yet (good first issue if anyone wants it). MIT.
> https://github.com/dukesky/allpath-trading-agent · https://trading.all-path.com

---

## 3. X / Twitter thread (also fine for LinkedIn as one post)

1/ I open-sourced the trading agent I've been running on my paper account. One design rule shaped all of it: **the agent can only propose. I approve every order.** 🧵 [demo video]

2/ Strategies are plain YAML — a thesis, a target weight, rules like `price < 140 → sell all`. A sentinel checks them every hour. Hard rules can execute; soft rules queue for review with the agent's research attached.

3/ You talk to it in the browser or in Telegram — same agent, same memory. It keeps profile / strategy / per-stock / lessons layers and consolidates them nightly, so it stops being generic after a week. [chat screenshot]

4/ After the close it runs a bounded reflection session: re-reads every strategy against the day's real fills, writes lessons to memory, and proposes rule changes as side-by-side diffs. Those wait for approval too. [pending diff screenshot]

5/ Guards are code, not prompts: no order tool exists; strategy files only change via approved diffs with a byte-exact base check; a reflection proposal can't flip a strategy to auto. Risk caps are deterministic Python.

6/ Stack: Python · SQLite · FastAPI + htmx · Alpaca paper · any model via @OpenRouterAI / @OpenAI / @AnthropicAI. Runs on your machine. ~1,300 tests. MIT.

7/ Not investment advice, not a return machine — it's infrastructure for a discretionary process with an agent that remembers. Site + 5-min quick start: trading.all-path.com — repo: github.com/dukesky/allpath-trading-agent

(Tag @AlpacaHQ on tweet 6 — they retweet ecosystem projects. Also email their developer relations with the repo link and ask to be listed as a community project.)

---

## 4. Chinese long-form outline (掘金 / 知乎 / V2EX / 少数派)

**标题候选**:
- 《我做了一个永远不能自己下单的交易 agent:记忆、反思和审批门是怎么设计的》
- 《给 LLM 交易 agent 装一道人工闸门:一次开源项目的工程复盘》

**结构**(3000–4500 字,配 4–5 张截图 + 1 张流水线图):

1. **为什么反着做**:满地都是"AI 帮你赚钱"的项目,我要的是"AI 帮我想清楚、盯着盘、提建议——但每一分钱经我的手"。一句话立场 + 六格流水线图(只有"你批准"亮着)。
2. **策略即 YAML**:thesis / target_weight / rules;`price < 170 and position_weight < target_weight` 这种组合条件为什么重要(讲那个"仓位到位后加仓规则永远不该再触发"的真实案例)。哨兵每小时巡检、hard/soft 规则的区别。
3. **有记忆的 agent**:四层 markdown 记忆、注入防护、夜间整合;网页/Telegram/终端同一条对话。三档模型各司其职。
4. **每日反思循环**:为什么选"完整 agent 会话 + 缰绳"而不是单轮模板(能力上限 vs 可控);12 轮帽、种子简报、可回放的推理过程;报告推手机、建议进队列。
5. **审批门的工程细节**(本文最硬的部分):无下单工具、无写文件工具;字节级基线比对防"陈旧提案回滚已批准的止损";反思提案冻结 authorization/status;聊天提案切 auto 时的醒目告警;一次性审批链接的令牌设计(sha256 只存哈希、24h、用后即废、统一无效页防 oracle)。
6. **被评审拦下的坑**(读者最爱看的真实故事,挑 4 个):四个策略全 draft 没人监控却无提示;周日提交的市价单周一成交、journal 只记提交时间导致 agent 错标成交时间 17 小时;粗体长回复被切成上千条 Telegram 消息;聊天页面在一次改动后悄悄变成了没有告警的第三个审批入口。
7. **它不能做什么**:无节假日历、单券商、默认模拟盘、不是投资建议——写在最后不是免责声明,而是产品立场。
8. **上手**:六步 + 官网 + GitHub;欢迎 issue/PR,列几个 good first issue。

**发布顺序**:掘金/知乎先发全文 → V2EX 发精简版("分享创造"节点,600 字 + 链接) → 少数派投稿。B 站/视频号用同一支演示视频配中文解说。

---

## 5. Cadence after launch (low effort, compounding)

- Weekly: post one (fictional/redacted-numbers) reflection report excerpt — "the agent found my add rule's weight unit mismatch" — nothing else markets this product as well as its own output.
- Every issue/Discussion answered within 24h for the first month.
- Monthly release notes from CHANGELOG as a Discussion post + tweet.
- When someone forks/adapts (second broker, Docker), amplify them.
