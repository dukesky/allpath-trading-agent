# Options Trading via Alpaca MCP Server — Design

**Date:** 2026-08-27
**Status:** Approved (compressed design session; user approved scope verbatim)
**Deadline:** same-day — the Alpaca AI Trading Agents Hackathon (lablab.ai) starts 2026-08-28 and requires (a) Alpaca's MCP server or CLI and (b) options in every strategy.

## Goal

Minimal, stable single-leg options capability: the agent's strategies can
buy calls/puts and close option positions autonomously, with all option
market data and order routing going through Alpaca's official MCP server
(`uvx alpaca-mcp-server`). Stock trading keeps the existing alpaca-py path
untouched.

## Scope decisions (user-approved)

- Single-leg long options only: buy-to-open call/put, sell-to-close.
  No short selling of options, no multi-leg spreads.
- Hedging = buying puts on the opposite sector leg (fits single-leg scope).
- Option proposals reuse the existing intent → risk gate → executor →
  journal → notification chain.
- Options autonomy comes from SENTINEL RULE ACTIONS, not from agent order
  proposals (order proposals require human approval and would stall the
  zero-intervention run). Strategy YAML validation enforces: option actions
  are only legal in strategies with `authorization: auto` and on rules with
  `type: hard`. This is the documented v1 limitation.
- Paper account only; shadow account gets no options.

## Probed facts (verified live 2026-08-27 against the official server)

- Launch: `uvx alpaca-mcp-server`, stdio transport; env `ALPACA_API_KEY`,
  `ALPACA_SECRET_KEY`, `ALPACA_PAPER_TRADE=true`. 72 tools.
- Tool results are STRICT JSON text: `{"_alpaca_mcp_security": {...},
  "data": {...}}` — parse `data`. Tool-level errors come back as plain text
  starting with `Error calling tool`.
- `get_option_contracts` filters: `underlying_symbols` (string),
  `expiration_date_gte`/`_lte` (YYYY-MM-DD), `type` (call/put), strike
  bounds; contract objects carry `symbol` (OCC), `expiration_date`,
  `strike_price`, `close_price`, `tradable`.
- `get_option_latest_quote`: param `symbols` (comma-separated string);
  gives bid/ask.
- `place_option_order` single-leg: `symbol` (OCC), `side` ("buy"/"sell"),
  `qty` (STRING number of contracts), `type` default "market",
  `time_in_force` "day" only, `position_intent`
  ("buy_to_open"/"sell_to_close" for our two uses).

## Components

### 1. Settings / limits

- `Settings.options_trading: bool = False` (env `OPTIONS_TRADING`,
  .env-only). Off = system byte-identical to today.
- `RiskLimits.max_options_weight: Decimal = Decimal("0.10")` — total
  option-position market value (absolute) plus the new premium may not
  exceed this fraction of equity.
- New dependency: `mcp` (official Python SDK) in pyproject.

### 2. `allpath_trade/broker/options_mcp.py`

`OptionPick` model: `occ_symbol, expiry (date), strike (Decimal),
ask (Decimal, per-share), qty (int), est_premium (Decimal, total $ =
ask*100*qty)`.

`OptionsBackend` protocol (sync):
- `pick_contract(underlying, right, min_dte, otm_pct, budget, spot) ->
  OptionPick | None` — query contracts with `expiration_date_gte =
  today+min_dte` and `type=right`, choose the NEAREST expiry on/after the
  bound, then the strike closest to `spot*(1+otm_pct)` for calls /
  `spot*(1-otm_pct)` for puts among tradable contracts; fetch latest quote;
  `qty = floor(budget / (ask*100))`; return None (not an error) when qty
  would be 0 or no contract/quote exists.
- `place_option_order(occ_symbol, side, qty, position_intent) -> dict`
  (raw `data` payload: id, status, filled fields when present).
- `stop()` — terminate the subprocess.

`McpOptionsBackend` implements it: lazily spawns `uvx alpaca-mcp-server`
(stdio, keys from Settings, `ALPACA_PAPER_TRADE=true`) with the `mcp` SDK;
the async session lives on a dedicated daemon thread's event loop; the sync
facade serializes calls with a lock and a 30s per-call timeout; on any
transport failure it tears down and respawns once before surfacing the
error. A result text starting with `Error calling tool` raises
`OptionsBackendError`. Tests use a `FakeOptionsBackend`; one
integration-marked test (deselected by default, like the existing two)
exercises the real server read-only.

### 3. Action grammar (`strategy/actions.py`, `strategy/loader.py`)

New kinds and syntax (case-insensitive, same `_PATTERNS` table):

- `buy_call $1500` / `buy_call $1500 dte>=10 otm=3%`
- `buy_put $1500` / same optional params
- `close_options`

Defaults when omitted: `dte>=7`, `otm=2%`. `ActionSpec` gains
`min_dte: int | None`, `otm_pct: Decimal | None` (percent stored as
fraction). Loader validation (`parse_strategy_text` path, where rule
actions are already parsed): an option action in a strategy whose
`authorization != auto`, or on a rule whose `type != hard`, is a
`StrategyValidationError` naming the v1 limitation.

### 4. Intent + risk gate

`OptionIntent` (broker/base.py): `underlying, right ("call"/"put"),
occ_symbol, side (buy/sell), qty (int), est_premium (Decimal, total $;
0 for closes), reason, strategy_id`.

`RiskGate.check_option(intent, *, account, positions, trades_today,
is_paper) -> RiskDecision`:
- live trading disabled check (same as stocks),
- BUY only: `est_premium <= max_order_value`,
- BUY only: options exposure — sum of `abs(market_value)` of positions
  whose ticker parses as an OCC option symbol, plus `est_premium`, must be
  `<= max_options_weight * equity`,
- shared `max_daily_trades` check,
- SELL (close): always allowed apart from the daily-trade cap — closing
  risk-reducing positions must not be blocked by value caps.

OCC detection helper (shared, e.g. in broker/base.py):
`parse_occ_symbol(ticker) -> (root, expiry_date, right, strike) | None`
(pattern: root A-Z up to 6 + YYMMDD + C/P + 8-digit strike*1000).

### 5. Executor (`execution.py`)

`Executor.__init__` gains `options_backend: OptionsBackend | None = None`.
New `execute_option(intent: OptionIntent) -> ExecutionResult`:
- fetch account/positions/trades_today (same failure handling as
  `execute`),
- `gate.check_option(...)`; rejection → journal + result exactly like
  stocks (journal rows use a synthetic `OrderIntent(ticker=occ_symbol,
  side=..., qty=contracts, notional=est_premium or None, reason,
  strategy_id)` so the journal/digest/reflection pipelines see option
  trades with zero schema change),
- approved → `options_backend.place_option_order(...)`
  (`position_intent="buy_to_open"` for buys, `"sell_to_close"` for
  closes); build an `Order` from the returned payload; journal it.
- `options_backend is None` → `ExecutionError("options trading disabled")`.

### 6. Sentinel (`sentinel.py`)

- `__init__` gains `options_backend: OptionsBackend | None = None`.
- `_dispatch`: option ActionKinds branch to `_dispatch_option` instead of
  `to_order_intent`:
  - BUY_CALL/BUY_PUT: `pick_contract(...)` with the rule's params, spot =
    trigger price, budget = action amount → None → "skipped, no affordable
    contract" outcome + notify; else `OptionIntent` →
    `executor.execute_option` (loader guarantees auto+hard) → same
    executed/rejected/error outcomes and order-receipt notifications as
    stocks (subject shows the OCC symbol).
  - CLOSE_OPTIONS: every position whose OCC root == strategy underlying →
    one sell-to-close `execute_option` each; no positions → skipped.
- Expiry sweep in `run_once` (after the breaker block, before strategy
  loop; only when `options_backend` is set): any position whose OCC expiry
  is ≤ 1 calendar day away is closed sell-to-close through
  `execute_option` with reason "expiry safety sweep (DTE<=1)", one
  observation + receipt notification each. Errors isolate per position
  into `report.errors`.

### 7. Wiring (`app.py`) + agent guidance

- `_build_account_components`: for the paper account only, when
  `settings.options_trading` and Alpaca keys are set, build one
  `McpOptionsBackend`; pass to that account's `Executor` and `Sentinel`.
  `close_components`/shutdown path calls `backend.stop()` (follow how the
  app currently tears down; if there is no such hook, register `atexit`).
- Agent guidance: wherever the system prompt / draft_strategy /
  propose_strategy_revision describe the strategy YAML and rule actions
  (locate via context.py + tool descriptions), document the three option
  actions, their params and defaults, the auto+hard-only rule, and the
  discipline line: option budget per position ≤ ~2% of equity and every
  option strategy must include an exit rule (`close_options` on profit
  target and stop). Only shown when options are enabled? No — text is
  static; it says "if options trading is enabled".

## Out of scope

- Multi-leg orders, selling to open, exercise decisions
  (`exercise_options_position` never called; DTE sweep prevents expiry
  holding), Greeks-based selection, options on shadow, pending-review
  queue support for option intents, options in the web equity/positions UI
  beyond what falls out of existing broker views.

## Testing

Unit: grammar (all three verbs, params, defaults, rejects), loader
auth/type enforcement, OCC parser, `check_option` (each limit), picker math
on fake chain JSON shaped like the probed payloads (envelope + fields),
executor option paths (approve/reject/disabled/backend error), sentinel
dispatch + close + sweep with `FakeOptionsBackend`, wiring on/off.
Integration (deselected by default): real `uvx alpaca-mcp-server` spawn,
list tools, one chain fetch. Full suite green.
