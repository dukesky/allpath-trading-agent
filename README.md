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
[中文文档](README.zh-CN.md)

</div>

---

> **Project status:** Phases 1-3 are complete — broker connectivity, market data, risk management, and trade journaling are operational against Alpaca paper accounts; the strategy engine + sentinel loop (YAML strategies, rule evaluation, versioning, scheduled monitoring, hard-rule auto-execution) is running; and the LLM agent core (multi-provider chat client, tool-calling loop, `tradewind chat` REPL, and a ReviewAgent that researches queued soft-rule triggers, attaching its analysis for your review (and deciding execute/skip for auto-authorized strategies, still through the risk gate)) is now in place. The Web UI + memory system is next; see the [Roadmap](#roadmap). **Paper trading only by default.**

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [Architecture](#architecture)
- [Safety Model](#safety-model)
- [Getting Started](#getting-started)
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

The framework is distributed as the Python package **`tradewind`** and is designed to run entirely on your own machine: your keys, your data, your decisions.

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
│                 Web UI (chat + dashboard)            │   Phase 5
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
│  │ (Alpaca)    │  │ (yfinance) │  │ (email)      │  │
│  └─────────────┘  └────────────┘  └──────────────┘  │
│        SQLite (strategies · memory · trade journal)  │
└─────────────────────────────────────────────────────┘
```

Design documents are maintained in [`docs/superpowers/specs/`](docs/superpowers/specs/) and implementation plans in [`docs/superpowers/plans/`](docs/superpowers/plans/).

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
uv run tradewind status
uv run tradewind chat   # talk to the agent (needs LLM + Alpaca keys in .env)
```

Expected output: your paper account equity, cash, buying power, open positions, and recent trade journal entries.

## Project Structure

```
tradewind/
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
| 3 | **Agent core** — multi-provider LLM layer (Claude / OpenAI / OpenRouter), tool loop, context assembly, `tradewind chat` REPL, ReviewAgent-annotated sentinel triggers | ✅ Complete |
| 4 | **Memory system** — four layers with cross-cutting consolidation after every loop | 🔜 Next |
| 5 | **Web UI + notifications** — chat, dashboard, pending-confirmation queue, settings, email | Planned |
| 6 | **Reflection loops** — daily deep review, post-trade retrospectives | Planned |

## Development

```bash
uv run pytest                  # unit tests (network-free)
uv run pytest -m integration   # integration tests — requires Alpaca paper keys
uv run ruff check .            # lint
uv run tradewind chat          # talk to the agent (needs LLM + Alpaca keys in .env)
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
