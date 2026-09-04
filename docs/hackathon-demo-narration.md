# Demo Video Narration Script (≤5 min)

Read as-is or paraphrase — each segment stands alone, so record them
separately and cut them together. Stage directions in *italics*; spoken
lines in quotes. Target pace: ~145 words/minute. Total spoken: ~650 words.

---

## 1 · Hook — 0:00–0:20

*Screen: slides page 1 (title), hold 5s, then cut to the live Dashboard.*

> "This is AllPath — a self-hosted trading agent. And this is a real week
> of autonomous trading. After one kickoff conversation, no human touched
> it: the agent picked the tickers, wrote the strategies, traded stocks
> and options, and rewrote its own rules every night. Let me show you."

## 2 · Setup — 0:20–0:50

*Screen: terminal with `uv run allpath-trade serve` typed, then the setup
wizard page (step 1, nothing filled in).*

> "Setup is one command. `uv sync`, then `allpath-trade serve` — the web
> app, the scheduler, and the agent all run in this single process, on
> your own machine. A first-run wizard collects your LLM key and your
> Alpaca paper keys. You never edit a config file. That's it — from
> clone to running agent in about two minutes."

## 3 · Dashboard — 0:50–1:35

*Screen: Dashboard. Point at equity cards, scroll to positions, pause on
the OCC option rows, then the strategy cards.*

> "This is mission control. Account equity, cash, and the live equity
> curve — this is a fresh one-hundred-thousand-dollar paper account,
> started the day the competition opened."

> "Here are the positions. Notice the stock legs and the option legs
> together — these long symbols are real option contracts: NVDA calls,
> a CRM put, all with live profit and loss."

> "Below, each strategy is a card: its authorization level, progress
> toward its target weight, and its profit and stop levels. And this
> heartbeat line at the top means the sentinel is checking every rule
> every thirty minutes — with zero LLM cost."

## 4 · Chat — 1:35–2:25

*Screen: Chat page, scrolled to the kickoff / planning exchange with the
revision table.*

> "Everything started with one conversation. I gave the agent my budget,
> my risk appetite, and three market ideas. It didn't just agree — it
> pulled live prices and news and checked each idea. It accepted my
> semiconductor-rotation thesis but redesigned it. And it rejected my
> META idea, with evidence: no catalyst this week, and pending
> litigation."

> "Then it drafted five strategies — as YAML documents anyone can read.
> A thesis in plain English, plus entry and exit rules as code. When it
> revises a plan, you get this: old condition, new condition, and the
> price levels re-checked against live quotes. It even tells you what it
> deliberately did *not* touch."

## 5 · Strategies — 2:25–3:05

*Screen: Strategies page, then the NVDA strategy detail.*

> "Here's a strategy up close. Entry rules, profit-taking, stop-loss —
> and this one: `buy_call`, fifteen hundred dollars, at least five days
> to expiry, three percent out of the money. The agent writes the
> *intent*. Deterministic code queries the option chain through Alpaca's
> official MCP server, picks the exact contract, and sizes the order.
> The LLM never touches a strike price by hand."

> "Every rule is one-shot — it fires once, and every version of this
> file is tracked with its rationale."

## 6 · Approvals — 3:05–3:45

*Screen: Pending page with the diff card; then a phone (or screenshot)
showing the Telegram approve buttons.*

> "You choose how much leash the agent gets — per strategy. This week,
> everything runs on full auto: hard rules execute straight through a
> deterministic risk gate, and I just get the receipt on my phone."

> "But in normal life you can run confirm mode: proposals queue up here,
> with the review agent's analysis attached, and I approve from the web —
> or right from Telegram, with one tap. Either way, an account-level
> circuit breaker sits underneath: fifteen percent drawdown, and all new
> risk halts automatically. Exits still run."

## 7 · Reports / self-evolution — 3:45–4:30

*Screen: Reports page, open the Aug 28 report detail; slow scroll.*

> "And here's my favorite part. Every night after close, the agent
> reflects — in writing. On night one, completely unprompted, it found
> a real design flaw in all five of its own strategies: every stop-loss
> would re-enable its own entry at a lower price, so the book would buy
> back whatever it had just stopped out of."

> "It queued five revisions to fix that, and they auto-applied through
> the guarded pipeline. And it kept going all week — twelve self-revisions
> in five nights. It saw its short-dated calls decaying faster than the
> stocks were moving, so it wrote salvage rules to cut them — those rules
> alone recovered about eleven hundred dollars of premium. By the final
> night it was critiquing its own stop placement: 'the fourth stop this
> week that harvested the intraday low.' That's an agent being honest
> about its own performance."

## 8 · Close — 4:30–5:00

*Screen: Dashboard equity once more, then slides results page, hold on
repo URL.*

> "The honest scoreboard: down four-point-nine percent on the week. The
> stock book was basically flat — the tuition was the five-day options
> overlay, and the agent diagnosed exactly that, in writing, on night
> one. Every trade, every rejection, every LLM decision, and every
> nightly report is journaled locally. The audit trail *is* the demo."

> "AllPath is open source, MIT licensed, and everything you saw is in
> the repo — including the full paper trail of this week. Thanks for
> watching."

---

## Recording checklist

- Browser full screen, light theme, bookmarks bar hidden, PAPER chip visible
- Never open the Settings page (API keys live there)
- Record segments separately; trim loading waits; 1920×1080
- Phone shot for the Telegram moment (or a clean screenshot zoom-in)
- Re-render slides + refresh screenshots the morning of recording
