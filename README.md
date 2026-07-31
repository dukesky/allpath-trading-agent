# All Path Trading Agent

An open-source, self-hosted, LLM-powered **mid/long-term** trading agent
framework. It discusses your goals with you, co-creates strategies with
explicit take-profit/stop-loss rules, monitors daily, executes through your
own brokerage account under tiered authorization, and learns with you over
time. Package name: `tradewind`.

> Status: Phase 1 (execution foundation). Paper trading only by default.

## Quickstart

1. Install [uv](https://docs.astral.sh/uv/), then:

   ```bash
   uv sync
   ```

2. Copy `.env.example` to `.env` and fill in your
   [Alpaca paper account](https://app.alpaca.markets/paper/dashboard/overview) keys.

3. Verify the connection:

   ```bash
   uv run tradewind status
   ```

## Safety model

- **Paper-first**: live trading is off unless you explicitly enable it.
- Every order passes a **deterministic risk gate** (order value cap, position
  weight cap, daily trade cap, cash reserve) that the LLM cannot bypass.
- Credentials stay in your local `.env`; nothing is ever uploaded.
- All trades and rejections are journaled in a local SQLite DB.

## Development

```bash
uv run pytest        # unit tests
uv run pytest -m integration   # needs Alpaca paper keys in env
uv run ruff check .
```
