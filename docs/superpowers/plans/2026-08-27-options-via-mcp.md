# Options via Alpaca MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Single-leg options (buy call/put, close) driven by sentinel rule actions, with all option data and orders routed through Alpaca's official MCP server.

**Architecture:** A sync `OptionsBackend` facade over a persistent `uvx alpaca-mcp-server` stdio subprocess (official `mcp` SDK, dedicated event-loop thread). New option action verbs compile to `OptionIntent`s that flow through the existing gate→executor→journal→notify chain. Loader restricts option actions to auto+hard. Stocks unchanged.

**Tech Stack:** Python 3.12, pydantic v2, `mcp` SDK, pytest. Spec: `docs/superpowers/specs/2026-08-27-options-via-mcp-design.md` — read the "Probed facts" section before touching MCP code; every payload shape there was verified live.

## Global Constraints

- `OPTIONS_TRADING` defaults **False**; when off, every code path is byte-identical to today (no subprocess spawned, sentinel/executor behave exactly as before).
- Option actions are legal ONLY in `authorization: auto` strategies on `type: hard` rules — enforced at strategy parse time with a clear error.
- Single-leg long only: `position_intent` is `buy_to_open` or `sell_to_close`; never sell-to-open, never multi-leg.
- Closes (sell_to_close) are never blocked by value/weight caps — only the daily-trade cap applies.
- Options only for the paper account; shadow gets no backend.
- MCP tool results: JSON text with a `data` envelope; error results are text starting with `Error calling tool` → raise `OptionsBackendError`.
- Stock trading paths (alpaca-py) untouched.
- Run `uv run pytest -q` (2228+ passing) before every commit.

---

### Task 1: Settings, risk limits, dependency

**Files:**
- Modify: `allpath_trade/config.py` (Settings, near `experiment_auto_apply_revisions`)
- Modify: `allpath_trade/risk/gate.py` (`RiskLimits`)
- Modify: `pyproject.toml` (add `mcp` to dependencies; run `uv lock` / `uv sync`)
- Test: `tests/test_config.py`, `tests/test_risk_gate.py` (find the actual risk-gate test file with `grep -rl "RiskLimits" tests/`)

**Interfaces:**
- Produces: `Settings.options_trading: bool = False` (env `OPTIONS_TRADING`); `RiskLimits.max_options_weight: Decimal = Decimal("0.10")`.

- [ ] **Step 1: Failing tests** — Settings default False + env-true; RiskLimits default `Decimal("0.10")`. Mirror the neighboring tests added for `experiment_auto_apply_revisions`.
- [ ] **Step 2: Run tests, verify FAIL** (`uv run pytest tests/test_config.py -q -k options`)
- [ ] **Step 3: Implement** — one field each, comments in the files' established style (options gate is .env-only; weight cap is total option exposure vs equity). Add `mcp` to pyproject dependencies and run `uv sync`.
- [ ] **Step 4: Run tests, verify PASS; full suite**
- [ ] **Step 5: Commit** `feat: options settings, risk limit, mcp dependency`

---

### Task 2: OCC symbol parser + OptionIntent model

**Files:**
- Modify: `allpath_trade/broker/base.py`
- Test: `tests/test_broker_base.py`

**Interfaces:**
- Produces:

```python
class OptionIntent(BaseModel):
    underlying: str            # e.g. "META" (upper, validated non-empty)
    right: str                 # "call" | "put"
    occ_symbol: str            # e.g. "META260918C00600000"
    side: OrderSide            # BUY (open) or SELL (close)
    qty: int                   # contracts, >= 1
    est_premium: Decimal       # total dollars (ask*100*qty); 0 for closes
    reason: str
    strategy_id: str | None = None

def parse_occ_symbol(ticker: str) -> OccParts | None: ...
class OccParts(NamedTuple):
    root: str; expiry: date; right: str; strike: Decimal
```

OCC pattern: `^(?P<root>[A-Z]{1,6})(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$`; expiry = `20YY-MM-DD`; strike = int/1000; right = "call"/"put". Non-matching → None (plain stock tickers).

- [ ] **Step 1: Failing tests** — `parse_occ_symbol("META260918C00600000")` → root META, expiry date(2026,9,18), right "call", strike Decimal("600"); a put with fractional strike (`...P00123500` → 123.5); `parse_occ_symbol("META")` → None; `parse_occ_symbol("BRKB")` → None. OptionIntent validation: qty >= 1 enforced.
- [ ] **Step 2: FAIL** → **Step 3: Implement** → **Step 4: PASS + full suite** → **Step 5: Commit** `feat: OCC symbol parser and OptionIntent`

---

### Task 3: Option action grammar + loader enforcement

**Files:**
- Modify: `allpath_trade/strategy/actions.py`, `allpath_trade/strategy/loader.py:80-90` (the existing `parse_action(rule.action)` validation loop)
- Test: `tests/test_actions.py` (grammar), `tests/test_strategy_loader_atomic_write.py` or wherever `parse_strategy_text` validation is tested (`grep -rln "parse_strategy_text" tests/`)

**Interfaces:**
- Produces: `ActionKind.BUY_CALL/BUY_PUT/CLOSE_OPTIONS`; `ActionSpec` gains `min_dte: int | None = None`, `otm_pct: Decimal | None = None` (fraction, e.g. `Decimal("0.03")`). Grammar (case-insensitive):
  - `buy_call $1500`, `buy_call $1500 dte>=10 otm=3%` (params optional, either order not required — fixed order `dte` then `otm` is fine and documented)
  - `buy_put ...` identical
  - `close_options`
  Omitted params stay None; DEFAULTS (dte 7, otm 2%) are applied at the sentinel call site, not in parsing.
- Loader: option action + (`authorization != auto` or `rule.type != hard`) → `StrategyValidationError` message: `option actions require authorization: auto and rule type: hard (v1 limitation)`.
- Helper: `is_option_action(spec) -> bool`.

Patterns to add to `_PATTERNS`:

```python
(re.compile(r"^buy_(?P<right>call|put)\s+\$(?P<num>[\d,.]+)"
            r"(?:\s+dte>=(?P<dte>\d+))?(?:\s+otm=(?P<otm>[\d.]+)%)?$",
            re.IGNORECASE), ActionKind.BUY_CALL),  # right group decides CALL/PUT
(re.compile(r"^close_options$", re.IGNORECASE), ActionKind.CLOSE_OPTIONS),
```

(Implementation note: the `right` group means one pattern serves both kinds — restructure `parse_action`'s loop minimally: after match, if `right` group present, kind = BUY_CALL or BUY_PUT by its value. Amount validation reuses the existing positive-Decimal path; `dte`/`otm` parse to int / fraction with `otm` bounds `0 < otm <= 50%`.)

- [ ] **Step 1: Failing grammar tests** — full forms, param-less defaults (None), bad forms (`buy_call` without $, `otm=0%`, `dte>=-1` rejected), case-insensitivity, `close_options`.
- [ ] **Step 2: Failing loader tests** — strategy YAML with `buy_call $1000` on hard rule + auto → parses; same with confirm auth → `StrategyValidationError`; soft rule + auto → error; plain stock actions unaffected.
- [ ] **Step 3: FAIL → implement → PASS + full suite**
- [ ] **Step 4: Commit** `feat: option action grammar with auto+hard enforcement`

---

### Task 4: MCP options backend

**Files:**
- Create: `allpath_trade/broker/options_mcp.py`
- Test: `tests/test_options_mcp.py` (new; unit tests on parsing/selection with canned JSON + one `@pytest.mark.integration`-style deselected live test — copy the marker mechanics from the existing deselected tests, `grep -rn "deselected\|integration" tests/ pyproject.toml` to find how)

**Interfaces:**
- Produces (spec §2, signatures verbatim):

```python
class OptionsBackendError(Exception): ...
class OptionPick(BaseModel):
    occ_symbol: str; expiry: date; strike: Decimal
    ask: Decimal; qty: int; est_premium: Decimal

class OptionsBackend(Protocol):
    def pick_contract(self, underlying: str, right: str, min_dte: int,
                      otm_pct: Decimal, budget: Decimal,
                      spot: Decimal) -> OptionPick | None: ...
    def place_option_order(self, occ_symbol: str, side: str, qty: int,
                           position_intent: str) -> dict: ...
    def stop(self) -> None: ...
```

- `McpOptionsBackend(settings)`: lazy start on first call — daemon thread runs an asyncio loop; on it, `stdio_client(StdioServerParameters(command="uvx", args=["alpaca-mcp-server"], env={keys, ALPACA_PAPER_TRADE:"true"}))` + `ClientSession.initialize()`. Sync methods submit coroutines via `asyncio.run_coroutine_threadsafe(...).result(timeout=30)` under a `threading.Lock`. One respawn attempt on transport failure, then raise `OptionsBackendError`.
- `_call(tool, args) -> dict`: `session.call_tool`, take `content[0].text`; text startswith `Error calling tool` → `OptionsBackendError(text)`; else `json.loads(text)["data"]`.
- `pick_contract` algorithm (spec §2): `get_option_contracts` with `{"underlying_symbols": underlying, "type": right, "expiration_date_gte": (today+min_dte).isoformat(), "limit": 300}` → contracts list under `data["option_contracts"]`; keep `tradable` ones; nearest expiry ≥ bound; strike closest to `spot*(1±otm_pct)`; `get_option_latest_quote {"symbols": occ}` → ask from the quote payload (inspect its actual key shape in the unit fixtures; live shapes: quote dict keyed by symbol with `ap` or `ask_price` — the integration test pins it, unit fixtures follow whatever the live probe shows); `qty = int(budget // (ask*100))`; qty < 1 or ask <= 0 → None.
- Env for subprocess: copy `os.environ` + keys from settings — never log key values.

- [ ] **Step 1: Failing unit tests** — `_parse_result` envelope/error handling; `pick_contract` selection math driven by a fake `_call` returning canned contract/quote JSON in the probed shape (nearest expiry, strike rounding both directions for call/put, unaffordable → None, non-tradable filtered).
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Live integration test (deselected by default)** — spawn real server, list tools (assert `place_option_order` present), one `get_option_contracts` call for META; run it once now (`uv run pytest tests/test_options_mcp.py -q -m integration` or the repo's equivalent) and record the quote-payload key names it shows in a comment; align unit fixtures.
- [ ] **Step 4: Full suite PASS** → **Step 5: Commit** `feat: MCP options backend (chain pick + order placement)`

---

### Task 5: RiskGate.check_option + Executor.execute_option

**Files:**
- Modify: `allpath_trade/risk/gate.py`, `allpath_trade/execution.py`
- Test: the risk-gate test file, `tests/test_execution.py`

**Interfaces:**
- Consumes: `OptionIntent`, `parse_occ_symbol`, `OptionsBackend` (Task 4), `RiskLimits.max_options_weight`.
- Produces: `RiskGate.check_option(intent, *, account, positions, trades_today, is_paper) -> RiskDecision` (spec §4 rules: allow_live, BUY premium ≤ max_order_value, BUY options-exposure cap via `sum(abs(p.market_value) for p in positions if parse_occ_symbol(p.ticker))`, shared daily-trade cap; SELL only daily cap). `Executor.__init__(..., options_backend: OptionsBackend | None = None)`; `Executor.execute_option(intent: OptionIntent) -> ExecutionResult` per spec §5 — synthetic `OrderIntent(ticker=occ_symbol, side, qty=Decimal(qty), notional=None, reason, strategy_id)` for journal rows (qty is contracts; notional deliberately None so the gate's own record shows the premium via decision reasons — journal.record signature is `record(intent, decision, order)` at `store/journal.py:51`); `Order` built from the MCP payload (`id`, `status` mapped via the same `_STATUS_MAP` idea as alpaca.py — copy the minimal mapping locally, `submitted_at=now`, filled fields when present); backend None → `ExecutionError("options trading disabled")`; backend errors → journal `status_override="error"` + `ExecutionError`, mirroring the stock path's broker-error handling.

- [ ] **Step 1: Failing gate tests** — each rule fires and passes independently (premium cap, exposure cap counts existing OCC positions, closes exempt from caps, daily cap shared).
- [ ] **Step 2: Failing executor tests** — approved buy places via FakeOptionsBackend and journals; gate rejection journals without placing; disabled backend raises; backend error journals error. Mirror `tests/test_execution.py`'s existing fakes.
- [ ] **Step 3: Implement → PASS + full suite** → **Step 4: Commit** `feat: option risk checks and executor path`

---

### Task 6: Sentinel — option dispatch, close_options, expiry sweep

**Files:**
- Modify: `allpath_trade/sentinel.py`
- Test: `tests/test_sentinel.py`

**Interfaces:**
- Consumes: everything above. `Sentinel.__init__(..., options_backend: OptionsBackend | None = None)`.
- Produces (spec §6):
  - `_dispatch` routes option ActionKinds to `_dispatch_option(doc, rule_id, spec, price, positions)` BEFORE the `to_order_intent` call (which must never see option specs). Defaults applied here: `min_dte = spec.min_dte or 7`, `otm_pct = spec.otm_pct or Decimal("0.02")`.
  - BUY: `pick_contract` → None → outcome `skipped` detail "no affordable option contract" + `_notify_rule`; else `OptionIntent(side=BUY, est_premium=pick.est_premium, ...)` → `executor.execute_option` → executed/rejected/error outcomes + `_notify_order` with the OCC symbol as ticker (reuse the existing helper; it takes a ticker string).
  - CLOSE_OPTIONS: positions with `parse_occ_symbol(p.ticker).root == doc.position.ticker` → one `OptionIntent(side=SELL, qty=int(p.qty), est_premium=0)` each through `execute_option`; none → skipped outcome.
  - `options_backend is None` but an option rule fires (possible when the operator turns the flag off with strategies still active): outcome `error`, detail "options trading disabled", notify — never crash the pass.
  - Expiry sweep in `run_once` right after the breaker block: when backend present, for each position with `parse_occ_symbol(...)` and `(expiry - today).days <= 1` → sell-to-close via `execute_option` (reason "expiry safety sweep (DTE<=1)"), observation source `"sentinel"`? NO — use source `"options_sweep"` (same digest-count reasoning as `"breaker"`), receipt notification per close, per-position try/except into `report.errors`.

- [ ] **Step 1: Failing tests** — auto+hard buy_call executes via fakes (assert OptionIntent fields incl. defaults); pick=None → skipped; close_options closes only that underlying's OCC positions; disabled-backend error outcome; sweep closes DTE<=1 and leaves DTE>1; sweep absent when backend None. Mirror existing sentinel fakes; extend the fake broker's positions with OCC-symbol positions.
- [ ] **Step 2: Implement → PASS + full suite** → **Step 3: Commit** `feat: sentinel executes option actions and expiry sweep`

---

### Task 7: App wiring + shutdown + agent guidance

**Files:**
- Modify: `allpath_trade/app.py` (`_build_account_components`; find the serve shutdown path — `grep -n "atexit\|shutdown\|close" allpath_trade/app.py allpath_trade/web/server.py allpath_trade/scheduler.py` — and hook `backend.stop()`; if no clean hook exists, `atexit.register`)
- Modify: agent guidance — locate where the strategy YAML schema/actions are described to the LLM (`grep -rn "condition\|action" allpath_trade/agent/context.py | head`, plus `draft_strategy`'s description in `agent/action_tools.py:210` and `REFLECTION_INSTRUCTIONS` in `reflect.py`) and add the option grammar documentation there.
- Test: `tests/test_app.py`, plus a context/prompt test if one exists for the schema text (`grep -rln "build_system_prompt" tests/`)

**Interfaces:**
- Consumes: `McpOptionsBackend`, `Settings.options_trading`.
- Produces: paper account + `options_trading` + Alpaca keys present → one shared `McpOptionsBackend` instance passed to that account's `Executor` AND `Sentinel`; shadow always None; flag off → both None (assert byte-identical construction). Guidance text (English) documents: the three actions with params/defaults, auto+hard-only restriction, discipline (≤ ~2% equity premium per position; every option strategy must pair entries with `close_options` exit rules on both profit and stop conditions), and that it applies only when options trading is enabled.

- [ ] **Step 1: Failing wiring tests** — flag on: paper executor/sentinel share a backend, shadow gets None; flag off: all None.
- [ ] **Step 2: Implement wiring + stop() hook + guidance text.**
- [ ] **Step 3: PASS + full suite** → **Step 4: Commit** `feat: options backend wiring and agent guidance`

---

### Task 8: Docs + final full suite

**Files:**
- Modify: `CHANGELOG.md`, `docs/experiment-autonomous-run.md` (hackathon section: `OPTIONS_TRADING=true` joins the run's `.env` block; note the new-account requirement and options-level check), `docs/TODO.md` (record deferred: multi-leg, pending-queue option intents, Greeks-based selection)
- Test: none (docs)

- [ ] **Step 1: Write docs in each file's established style/language.**
- [ ] **Step 2: `uv run pytest -q` full suite green.**
- [ ] **Step 3: Commit** `docs: options-via-MCP changelog, runbook, deferred items`
