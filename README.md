<div align="center">

# All Path Trading Agent

**A self-hosted, LLM-powered trading agent framework for mid/long-term investing**

*It learns your goals, co-creates strategies with you, monitors the market, executes through your own brokerage account — and grows alongside you.*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://github.com/astral-sh/ruff)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#roadmap)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#contributing)

[Getting Started](#getting-started) ·
[Architecture](#architecture) ·
[Safety Model](#safety-model) ·
[Roadmap](#roadmap) ·
[Contributing](#contributing) ·
[中文文档](README.zh-CN.md) ·
[**Website →**](https://trading.all-path.com)

</div>

---

<p align="center">
  <img src="docs/images/dashboard-demo.png" alt="AllPath Trading Agent dashboard — equity curve, positions, strategy cards (demo data)" width="900">
</p>

<p align="center"><b>A trading agent that proposes. You approve.</b><br>
Write strategies in plain YAML, let a sentinel watch them every hour, chat with an agent that remembers how you think (web or Telegram) — and keep a human approval gate in front of every single order. Paper trading by default; the agent has no tool that places an order or writes a strategy file.</p>

<p align="center">
  <code>rule fires → agent researches → proposal queued → <b>you approve</b> → risk gate → paper order</code>
</p>

<p align="center"><sub>Screenshots use fictional demo data — not a real account.</sub></p>

---

> **Project status:** Phases 1-6 are complete — broker connectivity, market data, risk management, and trade journaling are operational against Alpaca paper accounts; the strategy engine + sentinel loop (YAML strategies, rule evaluation, versioning, scheduled monitoring, hard-rule auto-execution) is running; the LLM agent core (multi-provider chat client, tool-calling loop, `allpath-trade chat` REPL, and a ReviewAgent that researches queued soft-rule triggers) is in place; the memory system (four curated markdown layers + consolidation + session search) enables the agent to learn and recall durable patterns across sessions; and the web interface (`allpath-trade serve`, token-gated, LAN-reachable) puts the dashboard, chat, and confirmation queue on your phone. Phase 5.5 rounded out that web interface — a visible sentinel heartbeat, push notifications via ntfy alongside email, per-strategy notification control, and daily memory consolidation now reads the day's web chat, not just terminal sessions. Phase 6 closed the third loop: an after-close **reflection** pass that re-checks each strategy against the day's trades and prices, writes durable lessons to memory, and proposes strategy revisions for your approval — see [Reflection](#reflection). The agent is now also reachable from Telegram — same conversation and memory as the web chat, mirrored both ways — see [Telegram Chat](#telegram-chat); and drafting or revising a strategy from either chat surface now queues a Pending approval instead of requiring a terminal. Phase 7 added a second, always-on account — **Shadow**, a local ledger that mirrors your real brokerage and records rather than routes orders — running in parallel with Paper; see [Two Accounts](#two-accounts). **Paper trading (and shadow recording — never a real order) only by default.**

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [Architecture](#architecture)
- [How It Works](#how-it-works)
- [Reflection](#reflection)
- [Safety Model](#safety-model)
- [Getting Started](#getting-started)
- [Web Interface](#web-interface)
- [Telegram Chat](#telegram-chat)
- [Two Accounts](#two-accounts)
- [Project Structure](#project-structure)
- [Roadmap](#roadmap)
- [Development](#development)
- [Contributing](#contributing)
- [Security](#security)
- [Disclaimer](#disclaimer)
- [License](#license)

## Overview

Most LLM trading projects stop at *"here's my analysis."* Most algorithmic trading frameworks execute code but cannot reason. **All Path Trading Agent** bridges the two for **mid/long-term investing** (holding periods of weeks to months, not high-frequency trading):

| Capability | Description |
|---|---|
| **Conversational onboarding** | The agent interviews you to understand risk tolerance, capital, goals, and preferences |
| **Strategy co-creation** | Each strategy is a human-readable document: an investment thesis (prose) plus deterministic entry / take-profit / stop-loss rules (machine-checkable) |
| **Autonomous monitoring** | Scheduled market checks; on triggers the agent researches current news and prices itself before acting |
| **Tiered execution** | Trades through *your own* brokerage account, at the authorization level you choose: notify-only → confirm-first → auto-execute within limits |
| **Continuous learning** | Post-trade retrospectives, per-stock dossiers that compound over time, and distilled lessons that inform future decisions |

The framework is distributed as the Python package **`allpath-trade`** and is designed to run entirely on your own machine: your keys, your data, your decisions.

## Key Features

- **Three operating loops**
  1. **Conversation loop** — on demand: discuss stocks, create or revise strategies. Changes always follow *agent drafts → you approve → takes effect*.
  2. **Sentinel loop** — hourly during market hours (configurable interval): deterministic code evaluates prices against strategy rules at zero LLM cost. On a trigger, *hard rules* (e.g. stop-loss) execute immediately with no LLM in the path; *soft rules* (e.g. buy-the-dip) wake the agent to research conditions first.
  3. **Reflection loop** — daily after close and after every trade: the agent re-validates each strategy's thesis against fresh information, reviews portfolio risk, and reports to you.

- **Four-layer memory**

  | Layer | Contents |
  |---|---|
  | User profile | Risk tolerance, goals, decision habits (slowly evolving) |
  | Strategy memory | Full version history of every strategy, with revision rationale |
  | Stock dossiers | Per-ticker knowledge that compounds: behavior patterns, trade history, ticker-specific lessons |
  | Lessons | Distilled retrospective insights, retrieved by relevance for new decisions |

- **Bring your own LLM** — Anthropic Claude, OpenAI, or OpenRouter (one key, many models); strong models for strategy work, cheaper models for routine checks, and no LLM at all for rule evaluation.

- **Bring your own brokerage** — a thin, auditable adapter layer (a few hundred lines over the official `alpaca-py` SDK) that can be reviewed in one sitting. Additional brokers integrate through the `Broker` interface.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                 Web UI (chat + dashboard)            │   ✅ Phase 5
└──────────────────────────┬──────────────────────────┘
                           │ HTTP / WebSocket
┌──────────────────────────▼──────────────────────────┐
│                  FastAPI application                 │
│  ┌────────────┐  ┌──────────────┐  ┌─────────────┐  │
│  │ Agent core │  │ Strategy     │  │ Scheduler   │  │   Phases 2–4
│  │ (LLM, tools│  │ engine (rule │  │ (sentinel / │  │
│  │  memory)   │  │  evaluator)  │  │  reflection)│  │
│  └──────┬─────┘  └──────┬───────┘  └──────┬──────┘  │
│  ┌──────▼───────────────▼────────────────▼───────┐  │
│  │   Risk gate — deterministic, cannot be bypassed│  │   ✅ Phase 1
│  └──────┬─────────────────────────────────────────┘ │
│  ┌──────▼──────┐  ┌────────────┐  ┌──────────────┐  │
│  │ Broker layer│  │ Data layer │  │ Notifications│  │   ✅ Phase 1
│  │ (Alpaca)    │  │ (yfinance) │  │ (email, ntfy)│  │
│  └─────────────┘  └────────────┘  └──────────────┘  │
│        SQLite (strategies · memory · trade journal)  │
└─────────────────────────────────────────────────────┘
```

Design documents are maintained in [`docs/superpowers/specs/`](docs/superpowers/specs/) and implementation plans in [`docs/superpowers/plans/`](docs/superpowers/plans/).

## How It Works

**Conversation loop** — you talk, the agent researches with live tools, and anything that touches money or your strategy files stops for an explicit confirmation. On exit it distills what it learned into curated memory, which seeds the next session (dashed line).

![Conversation loop](docs/images/conversation-loop.svg)

**Sentinel loop** — runs on its own while you're away. Deterministic rule checks cost nothing; hard rules (stop-losses) execute without any LLM in the path, soft rules wake the agent to research before queuing for your approval. Every outcome is journaled, and after the close it's distilled into the same memory that makes the next review sharper.

![Sentinel loop](docs/images/sentinel-loop.svg)

## Reflection

Once a trading day closes, a scheduled job runs a bounded agent session
(up to `REFLECTION_MAX_ITERS` tool calls, default 12) that reviews the day
against every active strategy's stated thesis and rules. It runs after the
daily email digest and before nightly memory consolidation, so any
conclusions it reaches are available to that same night's consolidation
pass. Toggle it with `DAILY_REFLECTION` in `.env` (on by default).

What it produces, every day it runs:

- **A written report** — day summary, per-strategy check, lessons, and any
  proposals — plus a short plain-language summary sized for a phone push
  notification.
- **Durable memory updates**, when the agent reaches a real conclusion (a
  confirmed pattern, a broken assumption) — the same curated memory the
  conversation and sentinel loops read from.
- **Strategy revision proposals**, when a strategy's real-world behavior
  measurably diverges from what it assumes — a full diff against the
  current file plus the agent's rationale.

Where to see it:

| Surface | What's there |
|---|---|
| **Reports page** (web UI) | One row per day, a summary teaser, and a detail view with the full report and a replay of the session's tool calls |
| **Push / email** | ntfy gets the short summary as a phone banner; email gets the full report body |
| **Pending page** | Any proposed strategy revision waits here, diffed against the current file, until you approve or reject it |

Reflection **never applies anything on its own.** It has no order tool and
no way to write a strategy file directly — every conclusion it acts on goes
through `memory_update` (curated memory, not the money path) or
`propose_strategy_revision` (queued on Pending, gated by the same
byte-exact staleness check as every other approval). If the file changes
underneath a pending proposal before you approve it, approval is refused
and the diff is regenerated for review rather than silently applied against
a moving target.

## Safety Model

The framework is built on one invariant: **the LLM can never bypass your limits.**

```
LLM / strategy rules  →  OrderIntent  →  RiskGate (deterministic)  →  Broker (official SDK)
                                              │
                                        Trade journal (SQLite audit trail)
```

| Guarantee | Mechanism |
|---|---|
| Single path to the broker | `Executor.execute()` is the only code path that can submit an order; the LLM can only produce an `OrderIntent` |
| Deterministic pre-trade checks | Order value cap, position weight cap, daily trade cap, and cash reserve are enforced by plain code — no model in the loop |
| Paper-first | Live trading is disabled unless explicitly enabled (`allow_live`), and remains constrained by the risk gate |
| Fail-safe stop-losses | Hard rules execute without the LLM — they cannot fail due to model or API outages |
| Full auditability | Every trade, rejection, and error is journaled locally with its complete reasoning |
| Local credentials | LLM and brokerage keys live in a local `.env` (gitignored); nothing is transmitted anywhere else |

## Getting Started

### Prerequisites

- Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/)
- A free [Alpaca paper trading account](https://app.alpaca.markets/paper/dashboard/overview)
- SQLite built with FTS5 (standard in official Python ≥ 3.11 builds) — needed for memory search

### Installation

```bash
git clone https://github.com/dukesky/allpath-trading-agent.git
cd allpath-trading-agent
uv sync
```

### Configuration

```bash
cp .env.example .env
# Edit .env and set ALPACA_API_KEY / ALPACA_SECRET_KEY
```

All credentials stay in this local file. `ALPACA_PAPER=true` is the default; live trading additionally requires enabling `allow_live` in the risk limits.

### Verify

```bash
uv run allpath-trade status
uv run allpath-trade chat   # talk to the agent (needs LLM + Alpaca keys in .env)
```

Expected output: your paper account equity, cash, buying power, open positions, and recent trade journal entries.

### Run the web interface

```bash
uv run allpath-trade serve
```

Open `http://localhost:8791`. The access token is printed on startup and
stored as `WEB_TOKEN` in `.env`. To reach it from your phone on the same
network, bind to all interfaces:

```bash
uv run allpath-trade serve --host 0.0.0.0
```

The sentinel runs inside the same process, so this one command covers
monitoring, consolidation, and the interface.

## Web Interface

`allpath-trade serve` runs the FastAPI app and the sentinel scheduler in one
process (default port 8791). The token is generated once on first run and
stored in `.env`; later starts reuse it instead of reprinting it, and the
Settings page can reset it if it leaks. Sign in with the token at the
printed URL — the session cookie is `HttpOnly` and `SameSite=Strict`. There
is no built-in HTTPS, so only bind `--host 0.0.0.0` on a network you trust.

| Page | Purpose |
|---|---|
| Dashboard | Account equity, positions, active strategies (compact cards), recent trades, and a sentinel heartbeat so you can see at a glance that scheduled monitoring is actually running |
| Chat | The same agent as `allpath-trade chat`, with inline approval cards for orders it proposes; your message appears instantly with a "thinking" indicator while the agent works |
| Pending | The confirmation queue — approve or reject agent-proposed orders and strategy revisions, each with a risk pre-check (orders) or a byte-exact staleness check (revisions) |
| Reports | One row per day the reflection job ran, with a summary teaser; the detail view shows the full report and proposed revisions raised that day, and a transcript replay shows every tool call the session made |
| Strategies | Strategy documents, lifecycle badges, and version history, read-only on this page itself, but ask the agent in Chat to draft or revise one — the proposal lands on Pending for your approval |
| Memory | The four memory layers, tabbed, plus their change history — read-only |
| Settings | LLM/broker keys (write-only, never redisplayed); model dropdowns fed by a cached OpenRouter catalog; email and ntfy push notification settings with a save-and-test button that reports each channel's outcome; sentinel interval and consolidation toggles |

Orders the agent proposes in Chat never reach the broker directly — they
land in the Pending queue exactly like a sentinel soft-rule trigger, and
only your approval sends them on. Switching to live trading is not
reachable from the web interface; that still requires editing `.env`
directly (see [Safety Model](#safety-model)).

Strategy changes work the same way: ask the agent to draft a brand-new
strategy or revise an existing one, from the web Chat page or Telegram,
and it lands on Pending as a side-by-side diff against the current file
instead of being saved directly. Approving it is the only thing that
writes the YAML. A new strategy starts as a `draft` — you activate it from
its Strategies page when you're ready for the sentinel to evaluate it. If
a proposal would flip a strategy's authorization to `auto` (hard rules
place orders without a further approval) or its status from `active` to
`draft`, the card carries a warning so you don't approve that change by
accident.

Daily memory consolidation reads the day's conversation turns from every
web chat session, not just terminal ones, so lessons and preferences you
share in the browser make it into curated memory the same as a terminal
session would.

## Telegram Chat

Talk to the same agent from Telegram — one conversation, one memory, shared
with the web Chat page. Create a bot with [@BotFather](https://t.me/BotFather)
(`/newbot`), paste the token it gives you into Settings → Telegram, and pair
your account by sending `/start <your web token>` in a private chat with
your new bot (the same token you sign in to the web dashboard with). Delete
that `/start` message afterward — it contains your web token, and while the
bot tries to delete it for you automatically, it may lack the permission to.

Once paired, everything is mirrored both ways: a message you send in the
web Chat page also appears in Telegram (prefixed `You (web): ...`, followed
by the agent's reply), and a message you send in Telegram gets the agent's
reply back in-channel and shows up in the web Chat page too — same
conversation, same tool access, same order-approval discipline (a proposed
order still lands in the Pending queue either way; Telegram cannot approve
or reject anything itself). Approval/reject receipts mirror to Telegram as
well, so it doubles as a pocket record of what actually happened.

Only one Telegram chat can be paired at a time (re-pairing with `/start`
overwrites it); a message from any other chat is silently ignored. The
poller only runs under `allpath-trade serve` — the headless `run` daemon
has no Telegram channel, a known limitation — and a settings-page bot
token change takes effect after the next restart, not immediately (a
reset of your web token, the one you sign in and pair with, applies
immediately instead — the old value stops pairing and the new one starts
working right away).

Web chat and Telegram share the same conversation lock: a long-running
Telegram turn (a slow LLM call, several tool round-trips) holds that lock
for its whole duration, so an Approve/Reject click on the web dashboard
during that window will simply wait its turn rather than fail — expect a
brief stall, not an error.

## Two Accounts

`allpath-trade` runs **two practice grounds at once, always active** — not a
toggle you flip between, but two parallel pipelines (own sentinel, queue,
memory, strategies, reflection, equity curve) sharing one process:

| Account | What it is |
|---|---|
| **Paper** | An Alpaca paper-trading sandbox, starting from zero. Orders route through Alpaca's own simulated execution — exactly how this app has always worked. |
| **Shadow** | A local ledger that **mirrors your real brokerage account**. It holds no real-broker credentials and never places a real order — every buy/sell the agent (or a rule) decides on is *recorded* here, and the notification tells you plainly to go place it yourself, in your real account, if you agree with it. |

The point isn't just "paper trading is safer" — it's **comparison**. Feed
both accounts the same stock, the same news, the same week, and watch where
your paper-account instincts (free to fail, nothing real at stake) diverge
from the account that actually shadows what you hold. Every page, every
notification, and the Telegram bot make it unambiguous which account you're
looking at, so that comparison never turns into confusion.

**Switching accounts** — a chip in the top nav (`PAPER` in blue, `SHADOW` in
amber) picks which account every page shows — dashboard, chat, pending
queue, reports, memory, strategies. The choice is remembered in a cookie
(default: paper, until you first switch); a small dot on the switcher for
the account you're *not* looking at means it has something waiting for you.

**Getting your real positions into Shadow** — tell the agent in Chat ("I
hold 10 AAPL at $180 average cost") or go to Settings → Brokerage → Shadow
and upload a CSV (`ticker,qty,avg_cost` per line, capped at 2,000 rows).
Either way it's a normal approval-queue proposal — nothing lands in the
ledger until you approve it — and a "Reset ledger" button is there if you
want to start clean.

**Notifications** — every subject line is prefixed `[Paper]` or `[Shadow]`,
so a rule trigger, an order receipt, or the daily digest can never be
mistaken for the account you actually hold. A shadow order that gets
recorded says so plainly:

> *A buy order for TSLA was recorded in your shadow ledger — place this
> order in your brokerage now: BUY 4.5 TSLA @ ~$332.01.*

and a shadow item still waiting for your approval adds:

> *If approved, you'll place it yourself — approving here only records it
> in your shadow ledger.*

**Telegram** — send `/account` to a paired bot to switch which account that
chat talks to (inline Paper/Shadow buttons); every message, receipt, and
approval prompt carries the same `[Paper]`/`[Shadow]` prefix as the web UI.

**Cost** — reflection (the after-close deep-review pass) runs *per
account*, gated on that account having at least one active strategy, so a
fresh, empty Shadow ledger costs nothing extra. Once both accounts have
active strategies, expect roughly **2× the nightly LLM spend** of running
Paper alone — the Usage tab in Settings shows the real number, not an
estimate you have to trust.

## Project Structure

```
allpath_trade/          # import package (PyPI/CLI name: allpath-trade)
├── broker/       # Broker abstraction + Alpaca adapter
├── data/         # Market data sources (yfinance)
├── risk/         # Deterministic risk gate
├── store/        # SQLite persistence + trade journal
├── execution.py  # Order executor — the single trading entry point
├── config.py     # Settings + runtime-writable .env store
└── cli.py        # Command-line interface
```

## Roadmap

| Phase | Scope | Status |
|:---:|---|:---:|
| 1 | **Execution foundation** — broker abstraction, Alpaca (paper) adapter, market data, risk gate, trade journal, executor, CLI | ✅ Complete |
| 2 | **Strategy engine + sentinel loop** — YAML strategy documents, restricted-expression rule evaluator, versioning, scheduled monitoring, hard-rule auto-execution | ✅ Complete |
| 3 | **Agent core** — multi-provider LLM layer (Claude / OpenAI / OpenRouter), tool loop, context assembly, `allpath-trade chat` REPL, ReviewAgent-annotated sentinel triggers | ✅ Complete |
| 4 | **Memory system** — four layers with cross-cutting consolidation after every loop | ✅ Complete |
| 5 | **Web UI + notifications** — `allpath-trade serve`, token auth, chat, dashboard, pending-confirmation queue, settings, email | ✅ Complete |
| 5.5 | **UI polish + notification completion** — sticky nav, compact strategy cards, chat instant feedback, strategy lifecycle badges, tabbed memory page, model dropdowns from a cached catalog, save-and-test notifications, sentinel heartbeat, ntfy push, daily consolidation reads web chat | ✅ Complete |
| 6 | **Reflection loop** — after-close daily deep review, memory updates, strategy revision proposals, Reports page | ✅ Complete |
| 6.5 | **Telegram chat channel** — two-way chat with the same agent from Telegram, paired to one private chat, full web→Telegram mirroring, no new write capability | ✅ Complete |
| 6.6 | **Chat strategy proposals** — `draft_strategy` in web/Telegram chat queues a proposal on Pending instead of requiring a terminal, reusing the reflection revision pipeline (proposer chip, new-strategy and auto/status-regression warnings, superseding an earlier chat draft) | ✅ Complete |
| 7 | **Shadow dual-active accounts** — a second, always-on account that mirrors your real brokerage (local ledger, no real-broker credentials, orders recorded not routed) running in parallel with Paper; account switcher, per-account chat/memory/strategies/reflection, row-bound Telegram/web approvals, CSV import, `[Paper]`/`[Shadow]`-prefixed notifications | ✅ Complete |

## Development

```bash
uv run pytest                  # unit tests (network-free)
uv run pytest -m integration   # integration tests — requires Alpaca paper keys
uv run ruff check .            # lint
uv run allpath-trade chat          # talk to the agent (needs LLM + Alpaca keys in .env)
uv run allpath-trade memory show   # view agent memory files
```

**Engineering conventions**

- Python ≥ 3.11; synchronous core (mid/long-term trading requires no async)
- Monetary values are `Decimal`, never `float`
- Money-path modules (risk gate, executor, journal) require exhaustive unit tests
- Unit tests never touch the network; broker/data clients are injectable

## Contributing

Contributions are welcome. High-impact areas:

- **Broker adapters** — Interactive Brokers (via `ib_async`), Tradier, Charles Schwab
- **Data sources** — Tiingo (EOD), Finnhub (news / sentiment)
- **Hardening** — edge cases, error handling, and test coverage on the money path

Please open an issue to discuss substantial changes before submitting a pull request. All money-path code is expected to ship with exhaustive tests.

## Security

- Never commit `.env` or any credentials; the repository's `.gitignore` excludes them by default.
- The adapter layer delegates all authentication and transport to official broker SDKs.
- To report a vulnerability, please open a GitHub issue with minimal details and a contact method, and we will follow up privately.

## Disclaimer

This is self-hosted software provided under the MIT license, **not investment advice**. Trading involves substantial risk of loss. You are solely responsible for your trading decisions, your credentials, and compliance with your broker's terms of service. Start with paper trading; enable live trading only after you fully understand and accept the risks.

## License

This project is licensed under the [MIT License](LICENSE).
