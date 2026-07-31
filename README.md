# All Path Trading Agent

**An open-source, self-hosted, LLM-powered trading agent for mid/long-term investing — it learns your goals, co-creates strategies with you, watches the market, executes through your own brokerage account, and grows alongside you.**

[中文文档](README.zh-CN.md) · MIT Licensed · Package name: `tradewind`

> **Status:** Phase 1 (execution foundation) complete — broker connectivity, market data, risk gate, trade journal, and CLI are working against Alpaca paper accounts. The LLM agent core, strategy engine, memory system, and Web UI are on the [roadmap](#roadmap) below. **Paper trading only by default.**

## What is this?

Most LLM trading projects stop at "here's my analysis." Most algo-trading frameworks execute code but can't reason. All Path Trading Agent bridges the two, built for **mid/long-term investing** (weeks to months, not high-frequency):

- **Talks with you** to understand your risk tolerance, capital, goals, and preferences
- **Co-creates strategies** — each strategy is a human-readable document: an investment thesis (prose) plus deterministic entry/take-profit/stop-loss rules (machine-checkable)
- **Watches the market** on a schedule and reacts when your rules trigger — researching the latest news and prices itself, like a diligent analyst, before acting
- **Executes through your own brokerage account** under tiered authorization you control: notify-only → confirm-first → auto-execute within limits
- **Learns with you** — post-trade retrospectives, per-stock dossiers that deepen over time, and lessons that inform future decisions

## How it works

### Three loops

1. **Conversation loop** (whenever you want): chat with the agent to discuss stocks, create or revise strategies. Strategy changes always follow *agent drafts → you approve → takes effect*.
2. **Sentinel loop** (every 2 hours during market hours, configurable): cheap deterministic code checks prices against your strategy rules — zero LLM cost when nothing triggers. On a trigger, **hard rules** (e.g. stop-loss) execute immediately with no LLM in the path; **soft rules** (e.g. buy-the-dip) wake the agent to research current conditions first, then act per your authorization level.
3. **Reflection loop** (daily after close + after every trade): the agent re-validates each strategy's thesis against fresh information, reviews portfolio risk, sends you a report, and distills lessons into memory.

### Safety model — the LLM can never bypass your limits

```
LLM / strategy rules  →  OrderIntent  →  RiskGate (deterministic)  →  Broker (official SDK)
                                              │
                                        Trade Journal (SQLite audit trail)
```

- Every order passes a **deterministic risk gate** — order value cap, position weight cap, daily trade cap, cash reserve — enforced by plain code the LLM cannot route around. There is exactly one code path to the broker.
- **Paper-first**: live trading is off unless you explicitly enable it (`allow_live`), and stays constrained by the gate even then.
- **Hard stop-losses run without the LLM** — they cannot fail because a model hallucinated or an API was down.
- Every trade, rejection, and error is journaled locally with its full reasoning for audit and retrospectives.

### Four-layer memory

| Layer | What it holds |
|---|---|
| User profile | Risk tolerance, goals, habits (slowly evolving) |
| Strategy memory | Every strategy with full version history and revision reasons |
| Stock dossiers | Per-ticker knowledge that compounds: behavior patterns, trade history, ticker-specific lessons |
| Lessons | Distilled retrospective insights, retrieved when relevant to new decisions |

## Why self-hosted and open source?

Your money, your keys, your machine:

- **Credentials never leave your computer** — LLM and brokerage keys live in a local `.env`, gitignored, never uploaded anywhere
- **Bring your own LLM** — Claude (Anthropic), OpenAI, or OpenRouter (one key, many models; the default for testing)
- **Bring your own brokerage** — a thin, auditable adapter (a few hundred lines over the official `alpaca-py` SDK) you can read in one sitting; more brokers welcome via the `Broker` interface
- **MIT licensed** — use it, fork it, build on it. No hosted service, no data collection, no strings

## Quickstart (Phase 1)

1. Install [uv](https://docs.astral.sh/uv/), then:

   ```bash
   uv sync
   ```

2. Copy `.env.example` to `.env` and fill in your [Alpaca paper account](https://app.alpaca.markets/paper/dashboard/overview) keys (free).

3. Verify the connection:

   ```bash
   uv run tradewind status
   ```

   You should see your paper account equity, positions, and recent trade journal.

## Roadmap

- [x] **Phase 1 — Execution foundation**: broker abstraction + Alpaca (paper) adapter, market data (yfinance), deterministic risk gate, SQLite trade journal, order executor, CLI
- [ ] **Phase 2 — Strategy engine + sentinel loop**: YAML strategy documents, restricted-expression rule evaluator, versioning, scheduled monitoring, hard-rule auto-execution
- [ ] **Phase 3 — Agent core**: multi-provider LLM layer (Claude / OpenAI / OpenRouter), tool loop (quotes, web search, portfolio, order proposal), context assembly
- [ ] **Phase 4 — Memory system**: four layers with cross-cutting consolidation after every loop
- [ ] **Phase 5 — Web UI + notifications**: chat + dashboard + pending-confirmation queue + settings, email notifications
- [ ] **Phase 6 — Reflection loops**: daily deep review, post-trade retrospectives

Design docs live in [docs/superpowers/specs/](docs/superpowers/specs/) and implementation plans in [docs/superpowers/plans/](docs/superpowers/plans/).

## Development

```bash
uv run pytest                  # unit tests
uv run pytest -m integration   # needs Alpaca paper keys in env
uv run ruff check .
```

Tech: Python ≥3.11, pydantic v2, alpaca-py, yfinance, SQLite, pytest. Sync core (mid/long-term trading needs no async). Money is `Decimal`, never float.

## Contributing

Issues and pull requests are welcome — broker adapters (IBKR via `ib_async`, Tradier, Schwab), data sources (Tiingo, Finnhub), and hardening are especially good places to start. Deterministic money-path code (risk gate, executor, journal) requires exhaustive tests.

## Disclaimer

This is self-hosted software, not investment advice. You are responsible for your own trading decisions and for compliance with your broker's terms. Start with paper trading; enable live trading only when you understand and accept the risks.

## License

[MIT](LICENSE)
