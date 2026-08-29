# Hackathon Submission Materials — Alpaca AI Trading Agents (Aug 28 – Sep 4, 2026)

Copy-paste source for the lablab.ai submission form, plus the demo video
script and presentation outline. Keep every claim here consistent with
README.md and the live account.

## Step 1 — Basic Information

### Submission Title (45/50 chars)

```
AllPath — Self-Evolving Options Trading Agent
```

### Short Description (254/255 chars)

```
An autonomous agent that drafted its own options strategies, trades them via Alpaca's official MCP server, reflects nightly, and revises itself — behind a deterministic risk gate and a 15% drawdown circuit breaker. Live all week, zero human intervention.
```

### Long Description (~290 words, min 100)

```
AllPath is a self-hosted framework for LLM trading agents that reason like an analyst and execute like a machine — and for this hackathon it is running a documented, fully autonomous live experiment on a fresh $100k Alpaca paper account.

After a single kickoff conversation, the agent did everything itself. It validated our market theses against live prices and news — accepting a semiconductors-vs-software rotation idea (redesigned as long NVDA + a CRM put hedge after finding the two were co-moving), and rejecting a META play with evidence (no catalyst in the window, pending litigation). It then drafted a five-strategy portfolio as human-readable YAML: a prose thesis plus deterministic entry/exit rules, every strategy carrying stock and/or option legs with paired profit and stop exits.

Options are first-class and routed through Alpaca's official MCP server: the agent writes intent parameters (budget, minimum DTE, percent OTM), and plain code queries the option chain, selects the contract deterministically, and places the order. A sentinel evaluates every rule each 30 minutes at zero LLM cost; hard rules execute with no model in the path. Every night a bounded reflection session reviews the day against each thesis, writes lessons into curated markdown memory, re-arms spent triggers, and revises its own strategies — inside guards that freeze authorization changes and refuse stale writes.

Safety is deterministic and non-bypassable: per-order and options-exposure caps, cash reserve, an account-level 15% drawdown circuit breaker that halts new risk while still allowing exits, an option expiry sweep, and market-hours discipline. Every trade, skip, LLM decision, and nightly report with its full tool-call transcript is journaled — the audit trail is the demo.

Built in Python (2,368 tests), MIT-licensed, with a web dashboard, Telegram bridge, and CLI. Your keys, your machine, your agent.
```

## Later form steps — likely fields

| Field | Value |
|---|---|
| GitHub repo | https://github.com/dukesky/allpath-trading-agent |
| Website | https://trading.all-path.com |
| Category / track | Options Alpha Agents |
| Technology tags | Alpaca Trading API, Alpaca MCP Server, Python, FastAPI, Claude / OpenAI (bring-your-own-LLM), SQLite |
| Demo video | (YouTube link — record per script below) |
| Cover image | docs/images/hackathon/dashboard.png (or a composed banner) |

## Demo video script (target ≤ 3 minutes)

1. **Hook (0:00–0:20)** — dashboard of the live account. "This is a real
   week of autonomous trading. After one kickoff conversation, no human
   touched it: the agent picked the tickers, wrote the strategies, traded
   stocks and options, and rewrote its own rules every night."
2. **Strategy as a document (0:20–0:50)** — Strategies page → NVDA detail.
   Show thesis prose + rules; point at `buy_call $1500 dte>=5 otm=3%` and
   the paired `close_options` exits. "Strategies are YAML the agent writes
   and you can read."
3. **The options path (0:50–1:30)** — architecture slide or README diagram.
   Rule fires → option chain queried via Alpaca's official MCP server →
   deterministic contract selection → risk gate → order → journal →
   push receipt. Show a filled option position on the dashboard.
4. **Self-evolution (1:30–2:10)** — Reports page: a nightly reflection with
   transcript replay; then the Pending page's revision diff card (the
   day-one fix the agent wrote for its own entry conditions). "It found the
   flaw, rewrote the rule, and re-armed it — through the same guarded
   pipeline as everything else."
5. **Safety (2:10–2:40)** — Safety Model table from the README: risk gate,
   15% drawdown breaker (demotes to confirm, exits still run), expiry
   sweep, market-hours discipline, honest-failure journaling.
6. **Close (2:40–3:00)** — equity curve + trade journal. "Open source, MIT,
   self-hosted. The audit trail is the demo." Repo URL on screen.

Recording notes: 1440×900 browser, light theme, PAPER chip visible; use the
live account, not mocks; capture with the same pages the README screenshots
use for visual consistency.

## Presentation outline (5-7 slides, trim of README)

1. Title + one-liner + live equity number of the day
2. Problem: analysis bots don't execute; exec bots don't reason or remember
3. Architecture (mermaid diagram) — three loops, one process
4. Options via Alpaca MCP: intent → deterministic contract selection → gate
5. Self-evolution: nightly reflection, memory layers, guarded self-revision
6. Safety: the non-bypassable list + the day-one honest-failure story
7. Results: P&L curve, trade count, cost (Usage panel), what we'd do next
```
