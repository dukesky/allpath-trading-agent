# Option Pending-Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Option buys and `close_options` on `authorization: confirm` strategies wait in Pending for human approval, re-priced and risk-checked at approval time.

**Architecture:** A new review kind `option_order` stores an *option instruction* (not a fixed contract) plus a trigger-time preview. The sentinel queues it instead of skipping (buys) or executing (closes); `ReviewQueue._approve_option_order` refuses while the market is closed, re-picks the contract / re-reads positions, and executes through `Executor.execute_option`. Each approval surface gains an explicit `option_order` branch.

**Tech Stack:** Python 3.11+, pydantic v2, FastAPI + Jinja2, sqlite, pytest (`uv run pytest`).

**Spec:** `docs/superpowers/specs/2026-09-14-option-pending-queue-design.md` — read it first.

## Global Constraints

- The stock `kind="order"` path must behave exactly as today (existing tests unchanged, except `tests/test_sentinel.py:935` which Task 5 updates on purpose).
- `authorization: auto` option behavior is unchanged: buys and closes execute immediately.
- The DTE≤1 expiry sweep is unchanged (automatic).
- A confirm strategy's `close_options` executes immediately when the account's drawdown breaker has tripped (`breaker.tripped_at() is not None`); otherwise it queues.
- Approving an `option_order` while the US market is closed raises `ReviewError`, and the row stays `pending`. Token surfaces (approve-by-link, Telegram buttons) must check this BEFORE consuming the token.
- `execution_result` for an `option_order` records the trigger-time `preview` and the approval-time outcome.
- No schema migration (`pending_reviews.kind` has no CHECK constraint).
- Before every commit: `uv run pytest -q` fully green (baseline 2405 passed) and `uv run ruff check <touched files>` introduces no new errors.
- User-visible copy in English. Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

### Task 1: Shared market-hours helper

**Files:**
- Create: `allpath_trade/market_hours.py`
- Modify: `allpath_trade/scheduler.py:90-96` (`is_market_hours`)
- Modify: `allpath_trade/sentinel.py` (`_us_market_open_now`, near line 77)
- Test: `tests/test_market_hours.py` (new)

**Interfaces:**
- Produces: `allpath_trade.market_hours.is_us_market_open(now: datetime | None = None) -> bool`. Callers must call it as `market_hours.is_us_market_open()` (module attribute) so tests can monkeypatch `allpath_trade.market_hours.is_us_market_open`.
- `scheduler.is_market_hours(now=None)` and `sentinel._us_market_open_now()` keep their names and signatures (existing tests monkeypatch them) and delegate.

- [ ] **Step 1: Write the failing test** — `tests/test_market_hours.py`

```python
from datetime import UTC, datetime

from allpath_trade.market_hours import is_us_market_open


def _et(y, m, d, hh, mm):
    # EDT (UTC-4) in September
    return datetime(y, m, d, hh + 4, mm, tzinfo=UTC)


def test_open_mid_session_weekday():
    assert is_us_market_open(_et(2026, 9, 14, 12, 0)) is True  # Monday


def test_boundaries_open_inclusive_close_exclusive():
    assert is_us_market_open(_et(2026, 9, 14, 9, 30)) is True
    assert is_us_market_open(_et(2026, 9, 14, 9, 29)) is False
    assert is_us_market_open(_et(2026, 9, 14, 16, 0)) is False


def test_weekend_closed():
    assert is_us_market_open(_et(2026, 9, 12, 12, 0)) is False  # Saturday


def test_naive_datetime_treated_as_utc():
    assert is_us_market_open(datetime(2026, 9, 14, 16, 0)) is True  # 12:00 ET
```

- [ ] **Step 2: Run it — expect ImportError**

Run: `uv run pytest tests/test_market_hours.py -q`

- [ ] **Step 3: Implement** — `allpath_trade/market_hours.py`

```python
from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)


def is_us_market_open(now: datetime | None = None) -> bool:
    """US regular session: Mon-Fri 09:30-16:00 America/New_York.

    No holiday calendar yet (docs/TODO.md) -- a US market holiday reads as
    open. This is the one shared implementation: `scheduler.is_market_hours`,
    `sentinel._us_market_open_now` and `ReviewQueue` (option approvals) all
    call it, so the store layer never has to import `sentinel`."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    et = now.astimezone(ET)
    return et.weekday() < 5 and OPEN <= et.time() < CLOSE
```

In `scheduler.py`, replace the body of `is_market_hours` with `return market_hours.is_us_market_open(now)` (add `from allpath_trade import market_hours`; keep the existing `OPEN`/`CLOSE`/`ET` constants — other code in the module uses them). In `sentinel.py`, replace the body of `_us_market_open_now` with `return market_hours.is_us_market_open()` (add the same import; leave the explanatory comment block above it, and remove `_MARKET_TZ`/`_MARKET_OPEN`/`_MARKET_CLOSE` only if nothing else in the file uses them — check with grep).

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_market_hours.py tests/test_sentinel.py tests/test_scheduler*.py -q`, then the full suite. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add allpath_trade/market_hours.py allpath_trade/scheduler.py allpath_trade/sentinel.py tests/test_market_hours.py
git commit -m "refactor: one shared US market-hours helper

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Option instruction and approval-result models

**Files:**
- Modify: `allpath_trade/broker/base.py` (next to `OptionIntent`)
- Modify: `allpath_trade/execution.py` (next to `ExecutionResult`)
- Test: `tests/test_broker_base.py`, `tests/test_execution.py`

**Interfaces:**
- Produces (`broker/base.py`):

```python
class OptionPositionRef(BaseModel):
    occ_symbol: str
    qty: int = Field(ge=1)


class OptionInstruction(BaseModel):
    op: Literal["buy", "close"]
    underlying: str                      # upper-cased, non-empty
    reason: str
    strategy_id: str | None = None
    # buy only (all required when op == "buy")
    right: Literal["call", "put"] | None = None
    min_dte: int | None = None
    otm_pct: Decimal | None = None
    budget: Decimal | None = None
    spot_at_trigger: Decimal | None = None
    # close only
    positions_at_trigger: list[OptionPositionRef] = []
```

- Produces (`execution.py`):

```python
class OptionApprovalResult(BaseModel):
    submitted: bool                      # at least one option order submitted
    summary: str                         # one human-readable line
    reasons: list[str] = []
    results: list[ExecutionResult] = []
```

- [ ] **Step 1: Failing tests** — append to `tests/test_broker_base.py`

```python
from decimal import Decimal

import pytest
from pydantic import ValidationError

from allpath_trade.broker.base import OptionInstruction, OptionPositionRef


def _buy(**kw):
    base = dict(op="buy", underlying="nvda", reason="r", right="call", min_dte=30,
                otm_pct=Decimal("0.05"), budget=Decimal("1000"),
                spot_at_trigger=Decimal("211"))
    base.update(kw)
    return OptionInstruction(**base)


def test_buy_instruction_round_trips_and_uppercases():
    i = _buy()
    assert i.underlying == "NVDA"
    assert OptionInstruction.model_validate_json(i.model_dump_json()) == i


def test_buy_instruction_requires_buy_fields():
    with pytest.raises(ValidationError):
        _buy(budget=None)


def test_close_instruction_needs_no_buy_fields():
    i = OptionInstruction(op="close", underlying="NVDA", reason="r",
                          positions_at_trigger=[OptionPositionRef(
                              occ_symbol="NVDA261016C00220000", qty=2)])
    assert i.positions_at_trigger[0].qty == 2


def test_unknown_op_rejected():
    with pytest.raises(ValidationError):
        OptionInstruction(op="sell_to_open", underlying="NVDA", reason="r")
```

Append to `tests/test_execution.py`:

```python
def test_option_approval_result_defaults():
    from allpath_trade.execution import OptionApprovalResult
    r = OptionApprovalResult(submitted=False, summary="nothing to close")
    assert r.reasons == [] and r.results == []
```

- [ ] **Step 2: Run — expect ImportError.** `uv run pytest tests/test_broker_base.py tests/test_execution.py -q`

- [ ] **Step 3: Implement** the two models exactly as in Interfaces. Add a `field_validator("underlying")` that strips, upper-cases and rejects empty (same style as `OptionIntent._upper`), and a `model_validator(mode="after")`:

```python
    @model_validator(mode="after")
    def _buy_fields_present(self) -> OptionInstruction:
        if self.op == "buy":
            missing = [n for n in ("right", "min_dte", "otm_pct", "budget",
                                   "spot_at_trigger") if getattr(self, n) is None]
            if missing:
                raise ValueError(f"buy instruction missing: {', '.join(missing)}")
        return self
```

Add the needed imports (`Literal`, `Field`, `model_validator`) matching the file's existing import style.

- [ ] **Step 4: Run tests + full suite.** Expected: pass.

- [ ] **Step 5: Commit** — `feat: OptionInstruction and OptionApprovalResult models`

---

### Task 3: ReviewQueue — queue and approve `option_order`

**Files:**
- Modify: `allpath_trade/store/reviews.py` (new `add_option_order`, `approve()` allow-list, new `_approve_option_order` + helpers)
- Test: `tests/test_reviews.py`

**Interfaces:**
- Consumes: `OptionInstruction`, `OptionPositionRef`, `OptionIntent`, `OrderSide`, `parse_occ_symbol` (broker/base.py); `OptionApprovalResult`, `ExecutionError` (execution.py); `market_hours.is_us_market_open` (Task 1). Executor attributes used: `data.get_quote(ticker).price`, `options_backend.pick_contract(underlying, right, min_dte, otm_pct, budget, spot) -> OptionPick | None`, `broker.get_positions() -> list[Position]`, `execute_option(OptionIntent) -> ExecutionResult`.
- Produces:
  - `ReviewQueue.add_option_order(*, strategy_id: str, rule_id: str, ticker: str, rule_type: str, condition: str, action: str, snapshot: dict, instruction: OptionInstruction) -> ReviewHandle` — inserts `kind="option_order"`, `source="sentinel"`, token issued like `add()`.
  - `ReviewQueue.approve(review_id)` returns `OptionApprovalResult` for `option_order` rows.
  - Module constant `OPTION_MARKET_CLOSED_MESSAGE = "market is closed — option orders can only be approved during regular hours (Mon–Fri 09:30–16:00 ET); the item is still pending"`.

- [ ] **Step 1: Failing tests** — append to `tests/test_reviews.py`

```python
from datetime import date

from allpath_trade.broker.base import (
    OptionInstruction, OptionPositionRef, Position,
)
from allpath_trade.broker.options_mcp import OptionPick
from allpath_trade.execution import ExecutionError, ExecutionResult, OptionApprovalResult
from allpath_trade.risk.gate import RiskDecision
from allpath_trade.store.reviews import OPTION_MARKET_CLOSED_MESSAGE

OPT_PICK = OptionPick(occ_symbol="NVDA261016C00220000", expiry=date(2026, 10, 16),
                      strike=Decimal("220"), ask=Decimal("4.65"), qty=2,
                      est_premium=Decimal("930"))


class _Quote:
    def __init__(self, price):
        self.price = Decimal(price)


class _Data:
    def __init__(self, price="211"):
        self.price = price

    def get_quote(self, ticker):
        return _Quote(self.price)


class _Backend:
    def __init__(self, pick=OPT_PICK):
        self.pick = pick
        self.calls = []

    def pick_contract(self, underlying, right, min_dte, otm_pct, budget, spot):
        self.calls.append((underlying, right, min_dte, otm_pct, budget, spot))
        return self.pick


class _Broker:
    def __init__(self, positions=()):
        self.positions = list(positions)

    def get_positions(self):
        return self.positions


class OptionExecutor:
    def __init__(self, *, pick=OPT_PICK, positions=(), reject=None, raise_on=None,
                 backend=True):
        self.data = _Data()
        self.options_backend = _Backend(pick) if backend else None
        self.broker = _Broker(positions)
        self.reject = reject
        self.raise_on = raise_on
        self.option_calls = []

    def execute_option(self, intent):
        if self.raise_on == intent.occ_symbol:
            raise ExecutionError("broker down")
        self.option_calls.append(intent)
        if self.reject:
            return ExecutionResult(submitted=False, order=None,
                                   decision=RiskDecision(approved=False, reasons=[self.reject]))
        return ExecutionResult(submitted=True, order=None,
                               decision=RiskDecision(approved=True))


def _opt_pos(occ, qty="2"):
    return Position(ticker=occ, qty=Decimal(qty), avg_entry_price=Decimal("4.65"),
                    market_value=Decimal("930"), unrealized_pl=Decimal(0))


BUY_INSTR = OptionInstruction(op="buy", underlying="NVDA", reason="dip", strategy_id="s1",
                              right="call", min_dte=30, otm_pct=Decimal("0.05"),
                              budget=Decimal("1000"), spot_at_trigger=Decimal("210"))
CLOSE_INSTR = OptionInstruction(op="close", underlying="NVDA", reason="stop", strategy_id="s1",
                                positions_at_trigger=[OptionPositionRef(
                                    occ_symbol="NVDA261016C00220000", qty=2)])


def _opt_queue(tmp_path, executor):
    return ReviewQueue(connect(tmp_path / "o.db"), executor)


def _add_opt(q, instr=BUY_INSTR, preview=None):
    return q.add_option_order(
        strategy_id="s1", rule_id="entry-call", ticker="NVDA", rule_type="hard",
        condition="price < 212", action="buy_call $1000 dte>=30 otm=5%",
        snapshot={"price": "210", "preview": preview or json.loads(OPT_PICK.model_dump_json())},
        instruction=instr)


@pytest.fixture()
def market_open(monkeypatch):
    monkeypatch.setattr("allpath_trade.market_hours.is_us_market_open", lambda now=None: True)


@pytest.fixture()
def market_closed(monkeypatch):
    monkeypatch.setattr("allpath_trade.market_hours.is_us_market_open", lambda now=None: False)


def test_add_option_order_row_shape(tmp_path):
    q = _opt_queue(tmp_path, OptionExecutor())
    rid = _add_opt(q)
    row = q.get(rid)
    assert row["kind"] == "option_order" and row["status"] == "pending"
    assert row["ticker"] == "NVDA" and row["source"] == "sentinel"
    assert OptionInstruction.model_validate_json(row["intent"]) == BUY_INSTR
    assert row["approval_token_hash"]


def test_approve_option_buy_repicks_with_live_spot_and_executes(tmp_path, market_open):
    ex = OptionExecutor()
    q = _opt_queue(tmp_path, ex)
    rid = _add_opt(q)
    result = q.approve(rid)
    assert isinstance(result, OptionApprovalResult) and result.submitted
    assert ex.options_backend.calls == [("NVDA", "call", 30, Decimal("0.05"),
                                        Decimal("1000"), Decimal("211"))]
    [intent] = ex.option_calls
    assert intent.occ_symbol == "NVDA261016C00220000" and intent.qty == 2
    row = q.get(rid)
    assert row["status"] == "approved"
    er = json.loads(row["execution_result"])
    assert er["op"] == "buy" and er["preview"]["occ_symbol"] == "NVDA261016C00220000"
    assert er["picked"]["occ_symbol"] == "NVDA261016C00220000"
    assert er["spot_at_approval"] == "211"


def test_approve_option_buy_no_affordable_contract_at_approval(tmp_path, market_open):
    ex = OptionExecutor(pick=None)
    q = _opt_queue(tmp_path, ex)
    rid = _add_opt(q)
    result = q.approve(rid)
    assert result.submitted is False and "no affordable" in result.summary
    assert ex.option_calls == []
    assert json.loads(q.get(rid)["execution_result"])["picked"] is None


def test_approve_option_buy_risk_gate_rejection(tmp_path, market_open):
    ex = OptionExecutor(reject="order value exceeds max_order_value")
    q = _opt_queue(tmp_path, ex)
    result = q.approve(_add_opt(q))
    assert result.submitted is False
    assert result.reasons == ["order value exceeds max_order_value"]


def test_approve_option_close_rereads_positions(tmp_path, market_open):
    ex = OptionExecutor(positions=[_opt_pos("NVDA261016C00220000"),
                                   _opt_pos("AMD261016C00500000")])
    q = _opt_queue(tmp_path, ex)
    result = q.approve(_add_opt(q, CLOSE_INSTR, preview=[{"occ_symbol": "NVDA261016C00220000", "qty": 2}]))
    assert result.submitted
    [intent] = ex.option_calls
    assert intent.occ_symbol == "NVDA261016C00220000" and intent.side.value == "sell"


def test_approve_option_close_nothing_left(tmp_path, market_open):
    ex = OptionExecutor(positions=[])
    q = _opt_queue(tmp_path, ex)
    result = q.approve(_add_opt(q, CLOSE_INSTR, preview=[]))
    assert result.submitted is False and "no option positions left" in result.summary


def test_approve_option_close_one_failure_does_not_stop_the_rest(tmp_path, market_open):
    ex = OptionExecutor(positions=[_opt_pos("NVDA261016C00220000"),
                                   _opt_pos("NVDA261120C00230000")],
                        raise_on="NVDA261016C00220000")
    q = _opt_queue(tmp_path, ex)
    result = q.approve(_add_opt(q, CLOSE_INSTR, preview=[]))
    assert result.submitted
    assert [i.occ_symbol for i in ex.option_calls] == ["NVDA261120C00230000"]
    assert any("NVDA261016C00220000" in r for r in result.reasons)


def test_approve_option_while_market_closed_stays_pending(tmp_path, market_closed):
    ex = OptionExecutor()
    q = _opt_queue(tmp_path, ex)
    rid = _add_opt(q)
    with pytest.raises(ReviewError, match="market is closed"):
        q.approve(rid)
    row = q.get(rid)
    assert row["status"] == "pending" and row["approval_token_hash"]
    assert ex.option_calls == []
    assert OPTION_MARKET_CLOSED_MESSAGE.startswith("market is closed")


def test_approve_option_without_backend_stays_pending(tmp_path, market_open):
    q = _opt_queue(tmp_path, OptionExecutor(backend=False))
    rid = _add_opt(q)
    with pytest.raises(ReviewError):
        q.approve(rid)
    assert q.get(rid)["status"] == "pending"


def test_approve_option_corrupt_instruction_stays_pending(tmp_path, market_open):
    q = _opt_queue(tmp_path, OptionExecutor())
    rid = _add_opt(q)
    q._conn.execute("UPDATE pending_reviews SET intent='{\"op\":\"buy\"}' WHERE id=?", (rid,))
    q._conn.commit()
    with pytest.raises(ReviewError, match="corrupt option instruction"):
        q.approve(rid)
    assert q.get(rid)["status"] == "pending"


def test_approve_option_twice_second_is_refused(tmp_path, market_open):
    q = _opt_queue(tmp_path, OptionExecutor())
    rid = _add_opt(q)
    q.approve(rid)
    with pytest.raises(ReviewError):
        q.approve(rid)


def test_approve_option_quote_failure_after_claim_raises_execution_error(tmp_path, market_open):
    ex = OptionExecutor()

    def boom(ticker):
        raise RuntimeError("quote down")
    ex.data.get_quote = boom
    q = _opt_queue(tmp_path, ex)
    rid = _add_opt(q)
    with pytest.raises(ExecutionError):
        q.approve(rid)
    row = q.get(rid)
    assert row["status"] == "approved"
    assert "quote down" in json.loads(row["execution_result"])["error"]
```

- [ ] **Step 2: Run — expect failures** (`add_option_order` missing). `uv run pytest tests/test_reviews.py -q`

- [ ] **Step 3: Implement.** Imports at the top of `store/reviews.py`:

```python
from allpath_trade import market_hours
from allpath_trade.broker.base import (
    OptionInstruction, OptionIntent, OrderIntent, OrderSide, parse_occ_symbol,
)
from allpath_trade.execution import (
    ExecutionError, ExecutionResult, Executor, OptionApprovalResult,
)
```

Constant after `TOKEN_TTL_SECONDS`:

```python
OPTION_MARKET_CLOSED_MESSAGE = (
    "market is closed — option orders can only be approved during regular "
    "hours (Mon–Fri 09:30–16:00 ET); the item is still pending")
```

`add_option_order` (after `add`):

```python
    def add_option_order(self, *, strategy_id: str, rule_id: str, ticker: str,
                         rule_type: str, condition: str, action: str,
                         snapshot: dict, instruction: OptionInstruction) -> ReviewHandle:
        """Queue an option trade from a confirm strategy for approval. `intent`
        holds the option INSTRUCTION, not a contract -- the contract is
        re-picked (buys) or positions re-read (closes) at approval time."""
        token, token_hash, expires = self._issue_token()
        cur = self._conn.execute(
            "INSERT INTO pending_reviews (account, ts, strategy_id, rule_id, ticker,"
            " rule_type, condition, action, snapshot, intent, source, kind,"
            " approval_token_hash, token_expires_ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self._account, datetime.now(UTC).isoformat(), strategy_id, rule_id,
             ticker, rule_type, condition, action,
             json.dumps(snapshot, default=_json_default),
             instruction.model_dump_json(), "sentinel", "option_order",
             token_hash, expires))
        self._conn.commit()
        return ReviewHandle(cur.lastrowid, token)
```

In `approve()`, add before the final `raise`:

```python
        if row["kind"] == "option_order":
            return self._approve_option_order(review_id)
```

and widen the return annotation to `ExecutionResult | OptionApprovalResult | None`.

`_approve_option_order` and helpers (after `_approve_order`):

```python
    def _approve_option_order(self, review_id: int) -> OptionApprovalResult:
        row = self.get(review_id)
        if row["status"] != "pending":
            raise ReviewError(f"review {review_id} is {row['status']}, not pending")
        try:
            instruction = OptionInstruction.model_validate_json(row["intent"] or "")
        except (ValidationError, ValueError) as exc:
            raise ReviewError(
                f"review {review_id} has corrupt option instruction: {exc}") from exc
        # Before claiming: a closed-market approval must leave the row pending
        # (Alpaca's option venue rejects closed-market orders -- incident
        # 2026-08-28).
        if not market_hours.is_us_market_open():
            raise ReviewError(OPTION_MARKET_CLOSED_MESSAGE)
        executor = self._executor
        if executor is None or getattr(executor, "options_backend", None) is None:
            raise ReviewError("approve requires the options backend "
                              "(options trading is disabled)")

        resolved_ts = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            "UPDATE pending_reviews SET status=?, resolved_ts=?,"
            " approval_token_hash=NULL, token_expires_ts=NULL "
            "WHERE id=? AND status=? AND account=?",
            ("approved", resolved_ts, review_id, "pending", self._account))
        self._conn.commit()
        if cur.rowcount == 0:
            row = self.get(review_id)
            raise ReviewError(f"review {review_id} is {row['status']}, not pending")

        snapshot = json.loads(row["snapshot"]) if row["snapshot"] else {}
        record: dict = {"op": instruction.op, "preview": snapshot.get("preview")}
        try:
            if instruction.op == "buy":
                result = self._run_option_buy(executor, instruction, record)
            else:
                result = self._run_option_close(executor, instruction)
        except ExecutionError as exc:
            record["error"] = str(exc)
            self._write_execution_result(review_id, record)
            raise
        record["summary"] = result.summary
        record["reasons"] = result.reasons
        record["results"] = [json.loads(r.model_dump_json()) for r in result.results]
        self._write_execution_result(review_id, record)
        return result

    def _write_execution_result(self, review_id: int, record: dict) -> None:
        self._conn.execute(
            "UPDATE pending_reviews SET execution_result=? WHERE id=? AND account=?",
            (json.dumps(record, default=_json_default), review_id, self._account))
        self._conn.commit()

    @staticmethod
    def _run_option_buy(executor, instruction: OptionInstruction,
                        record: dict) -> OptionApprovalResult:
        try:
            spot = executor.data.get_quote(instruction.underlying).price
            pick = executor.options_backend.pick_contract(
                instruction.underlying, instruction.right, instruction.min_dte,
                instruction.otm_pct, instruction.budget, spot)
        except Exception as exc:  # noqa: BLE001 — claimed; must surface as ExecutionError
            raise ExecutionError(f"could not re-price the option at approval: {exc}") from exc
        record["spot_at_approval"] = str(spot)
        if pick is None:
            record["picked"] = None
            return OptionApprovalResult(
                submitted=False,
                summary="no affordable option contract at approval — nothing bought")
        record["picked"] = json.loads(pick.model_dump_json())
        intent = OptionIntent(
            underlying=instruction.underlying, right=instruction.right,
            occ_symbol=pick.occ_symbol, side=OrderSide.BUY, qty=pick.qty,
            est_premium=pick.est_premium, reason=instruction.reason,
            strategy_id=instruction.strategy_id)
        res = executor.execute_option(intent)
        if res.submitted:
            return OptionApprovalResult(
                submitted=True,
                summary=f"bought {pick.qty}x {pick.occ_symbol} (est. ${pick.est_premium:,.2f})",
                results=[res])
        reasons = list(res.decision.reasons)
        return OptionApprovalResult(
            submitted=False, summary="rejected by the risk gate: " + "; ".join(reasons),
            reasons=reasons, results=[res])

    @staticmethod
    def _run_option_close(executor, instruction: OptionInstruction) -> OptionApprovalResult:
        try:
            positions = executor.broker.get_positions()
        except Exception as exc:  # noqa: BLE001 — claimed; must surface as ExecutionError
            raise ExecutionError(f"could not read positions at approval: {exc}") from exc
        held = [p for p in positions
                if (parts := parse_occ_symbol(p.ticker)) is not None
                and parts.root == instruction.underlying]
        if not held:
            return OptionApprovalResult(submitted=False,
                                        summary="no option positions left to close")
        closed: list[str] = []
        issues: list[str] = []
        results: list[ExecutionResult] = []
        for p in held:
            try:
                parts = parse_occ_symbol(p.ticker)
                intent = OptionIntent(
                    underlying=instruction.underlying, right=parts.right,
                    occ_symbol=p.ticker, side=OrderSide.SELL, qty=int(p.qty),
                    est_premium=Decimal(0), reason=instruction.reason,
                    strategy_id=instruction.strategy_id)
                res = executor.execute_option(intent)
            except Exception as exc:  # noqa: BLE001 — one bad close must not stop the rest
                issues.append(f"{p.ticker}: {exc}")
                continue
            results.append(res)
            if res.submitted:
                closed.append(f"{p.ticker} x{int(p.qty)}")
            else:
                issues.append(f"{p.ticker}: " + "; ".join(res.decision.reasons))
        pieces = []
        if closed:
            pieces.append("closed " + ", ".join(closed))
        if issues:
            pieces.append("problems: " + "; ".join(issues))
        return OptionApprovalResult(submitted=bool(closed), summary="; ".join(pieces),
                                    reasons=issues, results=results)
```

Check `_json_default` and `Decimal` are already imported in the module (they are at the top of `store/reviews.py`).

- [ ] **Step 4: Run tests + full suite.** Expected: pass.

- [ ] **Step 5: Commit** — `feat: option_order review kind — queue, re-price and approve option trades`

---

### Task 4: Queued-review notification text for option orders

**Files:**
- Modify: `allpath_trade/notify/events.py` (`review_queued`)
- Test: `tests/test_notify_events.py`

**Interfaces:**
- Produces: `events.review_queued(..., kind: str = "order", option_preview: str = "")`. When `option_preview` is non-empty, the body gains the line `Option order at trigger: {option_preview} — re-priced and re-checked when you approve`. No other line changes for any kind.

- [ ] **Step 1: Failing tests** — append to `tests/test_notify_events.py`

```python
def test_review_queued_option_preview_line():
    _subject, body = events.review_queued(
        account="paper", review_id=7, ticker="NVDA",
        action="buy_call $1000 dte>=30 otm=5%", strategy_id="nvda-momentum-swing",
        trigger_price="$209.36", kind="option_order",
        option_preview="2x NVDA261016C00220000 ≈ $930.00")
    assert ("Option order at trigger: 2x NVDA261016C00220000 ≈ $930.00 — "
            "re-priced and re-checked when you approve") in body
    assert "shares at that price" not in body


def test_review_queued_without_option_preview_unchanged():
    _s, body = events.review_queued(
        account="paper", review_id=7, ticker="NVDA", action="buy $3500",
        strategy_id="s", trigger_price="$209.36", est_shares="16.72")
    assert "Option order at trigger" not in body
```

- [ ] **Step 2: Run — expect TypeError** on the unknown kwarg.

- [ ] **Step 3: Implement.** Add the parameter; after the `est_shares` line block add:

```python
    if option_preview:
        lines.append(f"Option order at trigger: {option_preview} — re-priced and "
                     "re-checked when you approve")
```

Add one sentence to the docstring naming `option_preview`.

- [ ] **Step 4: Run tests + full suite.**

- [ ] **Step 5: Commit** — `feat: option preview line in queued-review notifications`

---

### Task 5: Sentinel — queue option trades on confirm strategies

**Files:**
- Modify: `allpath_trade/sentinel.py` (`_dispatch` call site ~line 391, `_dispatch_option`, new `_queue_option_buy`, `_queue_option_close`, `_queued_option_review`, `_breaker_tripped`; `_agent_review` and `_notify_queued` signatures)
- Modify: `allpath_trade/agent/context.py` (`OPTIONS_ACTIONS_NOTE`)
- Test: `tests/test_sentinel.py`, `tests/test_context.py`

**Interfaces:**
- Consumes: `ReviewQueue.add_option_order` (Task 3), `OptionInstruction`/`OptionPositionRef` (Task 2), `events.review_queued(option_preview=...)` (Task 4), `DrawdownBreaker.tripped_at()`.
- Produces:
  - `_dispatch_option(self, doc, rule_id, condition, rule_type, spec, price, positions, reason, action: str)` — new trailing `action` parameter; the `_dispatch` call site passes `action=action`.
  - `_agent_review(..., intent: OrderIntent | None, *, price=None, kind: str = "order", option_preview: str = "")`; the autonomous-execute branch additionally requires `kind == "order"`.
  - `_notify_queued(..., intent=None, kind: str = "order", option_preview: str = "")` forwarding both to `events.review_queued`.

Behavior:

| authorization | action | result |
|---|---|---|
| auto | buy_call/buy_put (hard) | execute (unchanged) |
| not auto | buy_call/buy_put (hard) | pick preview → `pick is None` skipped / backend error → error (as today) → else queue |
| any | option action on a soft rule | skipped (as today) |
| auto, or breaker tripped | close_options (hard) | execute (unchanged path `_dispatch_close_options`) |
| not auto, breaker not tripped | close_options (hard) | no matching option positions → skipped (as today) → else queue |

- [ ] **Step 1: Failing tests** — append to `tests/test_sentinel.py`. Helpers already in that file: `strategy_yaml(auth=..., rule_type=..., condition="price < 250", action=...)` (ticker AAPL, rule id `r1`), `make_option(...)` (no breaker, price 200 → condition fires), `_breaker_make_option(...)` (breaker trips on the first pass; returns a 7-tuple), `_PICK`, `_occ_symbol`, `_occ_position`, `FakeOptionsBackend`. An autouse fixture near the top already patches `allpath_trade.sentinel._us_market_open_now` to open.

```python
def test_confirm_buy_call_queues_option_order_with_preview(tmp_path):
    backend = FakeOptionsBackend(pick=_PICK)
    yaml_text = strategy_yaml(auth="confirm", action="buy_call $500")
    s, _store, ex, q, n = make_option(tmp_path, yaml_text, backend=backend)

    report = s.run_once()

    [o] = report.outcomes
    assert o.disposition == "queued"
    assert ex.option_calls == []
    [row] = q.list()
    assert row["kind"] == "option_order" and row["ticker"] == "AAPL"
    assert json.loads(row["intent"])["op"] == "buy"
    assert json.loads(row["snapshot"])["preview"]["occ_symbol"] == _PICK.occ_symbol
    assert any("Option order at trigger" in body for _s, body in n.sent)


def test_confirm_buy_call_with_no_affordable_contract_still_skips(tmp_path):
    backend = FakeOptionsBackend(pick=None)
    yaml_text = strategy_yaml(auth="confirm", action="buy_call $500")
    s, _store, _ex, q, _n = make_option(tmp_path, yaml_text, backend=backend)
    [o] = s.run_once().outcomes
    assert o.disposition == "skipped" and q.list() == []


def test_confirm_close_options_queues_when_breaker_not_tripped(tmp_path):
    occ = _occ_symbol("AAPL", date.today() + timedelta(days=60))
    yaml_text = strategy_yaml(auth="confirm", action="close_options")
    s, _store, ex, q, _n = make_option(
        tmp_path, yaml_text, backend=FakeOptionsBackend(pick=_PICK),
        extra_positions=[_occ_position(occ, "2")])
    [o] = s.run_once().outcomes
    assert o.disposition == "queued" and ex.option_calls == []
    [row] = q.list()
    intent = json.loads(row["intent"])
    assert intent["op"] == "close"
    assert intent["positions_at_trigger"] == [{"occ_symbol": occ, "qty": 2}]


def test_confirm_close_options_executes_when_breaker_tripped(tmp_path):
    occ = _occ_symbol("AAPL", date.today() + timedelta(days=60))
    yaml_text = strategy_yaml(auth="confirm", action="close_options")
    s, _store, ex, q, _n, _breaker, _app_state = _breaker_make_option(
        tmp_path, yaml_text, backend=FakeOptionsBackend(pick=_PICK),
        extra_positions=[_occ_position(occ, "2")])
    report = s.run_once()
    assert any("drawdown breaker" in e for e in report.errors)
    [o] = report.outcomes
    assert o.disposition == "executed"
    assert [i.occ_symbol for i in ex.option_calls] == [occ]
    assert q.list() == []


def test_auto_buy_call_still_executes_immediately(tmp_path):
    s, _store, ex, q, _n = make_option(
        tmp_path, strategy_yaml(action="buy_call $500"),
        backend=FakeOptionsBackend(pick=_PICK))
    [o] = s.run_once().outcomes
    assert o.disposition == "executed" and len(ex.option_calls) == 1 and q.list() == []
```

(Import `json`, `date`, `timedelta` at the top of the test file if not already imported.)

Update the existing `test_breaker_demoted_auto_option_strategy_still_loads_buy_skipped_close_executes` (line ~935): rename it `..._buy_queued_close_executes`; change `outcomes["entry"].disposition == "skipped"` / `"authorization: auto" in outcomes["entry"].detail` to `outcomes["entry"].disposition == "queued"` plus one pending `option_order` row in `_q.list()` (rename `_q` to `q`); `exit` still executes and `ex.option_calls` still holds only the one sell. Rewrite its long comment to say: loading stays tolerant of the demotion; a demoted strategy's option buy now waits for approval, and its close still executes because the breaker has tripped.

In `tests/test_context.py`, next to the existing options-note assertions, add:

```python
    assert "waits in Pending for your approval" in prompt
```

- [ ] **Step 2: Run — expect failures.** `uv run pytest tests/test_sentinel.py tests/test_context.py -q`

- [ ] **Step 3: Implement.**

Imports: add `OptionInstruction, OptionPositionRef` to the `broker.base` import block; `import json` at the top.

Call site in `_dispatch` (~line 289): append `action` to the `_dispatch_option(...)` call, and replace the comment above it (it still claims the loader forbids option actions off `auto`) with: option actions go to `_dispatch_option`, which handles auto vs confirm itself.

Replace the authorization handling inside `_dispatch_option` (keep the `options_backend is None` guard at the top unchanged):

```python
        if spec.kind == ActionKind.CLOSE_OPTIONS:
            if rule_type != RuleType.HARD:
                self._notify_rule(doc, rule_id, condition, "skipped")
                return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                      disposition="skipped",
                                      detail="close_options requires rule type: hard")
            # auto executes as before. A confirm strategy's close waits for the
            # user -- EXCEPT once the drawdown breaker has tripped: a halt must
            # never strand an exit behind an approval (spec 2026-09-14).
            if doc.authorization == Authorization.AUTO or self._breaker_tripped():
                return self._dispatch_close_options(doc, rule_id, condition, positions, reason)
            return self._queue_option_close(doc, rule_id, rule_type, condition, action,
                                            positions, price, reason)
        # BUY_CALL / BUY_PUT
        if rule_type != RuleType.HARD:
            self._notify_rule(doc, rule_id, condition, "skipped")
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="skipped",
                                  detail="option buys require rule type: hard")
        if doc.authorization == Authorization.AUTO:
            return self._dispatch_option_buy(doc, rule_id, condition, spec, price, reason)
        return self._queue_option_buy(doc, rule_id, rule_type, condition, action,
                                      spec, price, reason)
```

Replace the long Finding-1b comments in that method with a short note that points to the spec, keeping the reasoning that loading never enforces authoring checks.

New helpers:

```python
    def _breaker_tripped(self) -> bool:
        return self.breaker is not None and self.breaker.tripped_at() is not None

    def _queue_option_buy(self, doc: StrategyDoc, rule_id: str, rule_type: RuleType,
                          condition: str, action: str, spec: ActionSpec,
                          price: Decimal, reason: str) -> TriggerOutcome:
        right = "call" if spec.kind == ActionKind.BUY_CALL else "put"
        min_dte = spec.min_dte if spec.min_dte is not None else 7
        otm_pct = spec.otm_pct if spec.otm_pct is not None else Decimal("0.02")
        underlying = doc.position.ticker
        try:
            pick = self.options_backend.pick_contract(
                underlying, right, min_dte, otm_pct, spec.amount, price)
        except OptionsBackendError as exc:
            self._notify_rule(doc, rule_id, condition, "error")
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="error", detail=str(exc))
        if pick is None:
            self._notify_rule(doc, rule_id, condition, "skipped")
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="skipped",
                                  detail="no affordable option contract")
        instruction = OptionInstruction(
            op="buy", underlying=underlying, reason=reason, strategy_id=doc.id,
            right=right, min_dte=min_dte, otm_pct=otm_pct, budget=spec.amount,
            spot_at_trigger=price)
        snapshot = {"price": str(price), "preview": json.loads(pick.model_dump_json())}
        rid = self.queue.add_option_order(
            strategy_id=doc.id, rule_id=rule_id, ticker=underlying,
            rule_type=rule_type.value, condition=condition, action=action,
            snapshot=snapshot, instruction=instruction)
        preview = f"{pick.qty}x {pick.occ_symbol} ≈ ${pick.est_premium:,.2f}"
        return self._queued_option_review(rid, doc, rule_id, rule_type, condition,
                                          action, price, preview)

    def _queue_option_close(self, doc: StrategyDoc, rule_id: str, rule_type: RuleType,
                            condition: str, action: str,
                            positions: dict[str, Position], price: Decimal,
                            reason: str) -> TriggerOutcome:
        underlying = doc.position.ticker
        refs = [OptionPositionRef(occ_symbol=p.ticker, qty=int(p.qty))
                for p in positions.values()
                if (parts := parse_occ_symbol(p.ticker)) is not None
                and parts.root == underlying and int(p.qty) >= 1]
        if not refs:
            self._notify_rule(doc, rule_id, condition, "skipped")
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="skipped",
                                  detail="no option positions to close")
        instruction = OptionInstruction(op="close", underlying=underlying, reason=reason,
                                        strategy_id=doc.id, positions_at_trigger=refs)
        snapshot = {"price": str(price),
                    "preview": [json.loads(r.model_dump_json()) for r in refs]}
        rid = self.queue.add_option_order(
            strategy_id=doc.id, rule_id=rule_id, ticker=underlying,
            rule_type=rule_type.value, condition=condition, action=action,
            snapshot=snapshot, instruction=instruction)
        preview = "close " + ", ".join(f"{r.occ_symbol} x{r.qty}" for r in refs)
        return self._queued_option_review(rid, doc, rule_id, rule_type, condition,
                                          action, price, preview)

    def _queued_option_review(self, rid: int, doc: StrategyDoc, rule_id: str,
                              rule_type: RuleType, condition: str, action: str,
                              price: Decimal, preview: str) -> TriggerOutcome:
        ticker = doc.position.ticker
        if self.review_agent is None:
            self._notify_queued(doc, rid, ticker, action, "", price=price,
                                kind="option_order", option_preview=preview)
            return TriggerOutcome(strategy_id=doc.id, rule_id=rule_id,
                                  disposition="queued")
        return self._agent_review(rid, doc, rule_id, rule_type, condition, action,
                                  None, price=price, kind="option_order",
                                  option_preview=preview)
```

`_agent_review`: change `intent: OrderIntent` to `intent: OrderIntent | None`, add `kind: str = "order", option_preview: str = ""`, pass `kind=kind, option_preview=option_preview` into every `_notify_queued` call in it, and change `autonomous = (...)` to also require `and kind == "order"`.

`_notify_queued`: add `kind: str = "order", option_preview: str = ""` and pass both into `events.review_queued(...)`.

`agent/context.py` — append to `OPTIONS_ACTIONS_NOTE`, right after the sentence about `confirm` strategies:

```
On a `confirm` strategy, an option rule that fires waits in Pending for your approval like a stock trade; the contract is re-priced when it is approved.
```

- [ ] **Step 4: Run** `uv run pytest tests/test_sentinel.py tests/test_context.py tests/test_reviews.py -q`, then the full suite.

- [ ] **Step 5: Commit** — `feat: sentinel queues option trades on confirm strategies`

---

### Task 6: Web approval surfaces — Pending page, review card, approve-by-link

**Files:**
- Modify: `allpath_trade/web/routes/reviews.py` (`approve` handler, ~line 433)
- Modify: `allpath_trade/web/templates/_review_card.html` (intent block ~line 86, resolved block ~line 185)
- Modify: `allpath_trade/web/routes/approve.py` (`_resolve` ~line 238, `_confirm_context` ~line 80)
- Test: `tests/test_web_reviews.py`, `tests/test_web_approve.py`

**Interfaces:**
- Consumes: `ReviewQueue.add_option_order`, `ReviewQueue.approve -> OptionApprovalResult` for `option_order` (Task 3); `OPTION_MARKET_CLOSED_MESSAGE`; `market_hours.is_us_market_open()` (Task 1); test fakes `OptionExecutor`, `BUY_INSTR`, `CLOSE_INSTR`, `OPT_PICK` exported from `tests/test_reviews.py` (Task 3).

Existing behavior to rely on: the web `approve` handler already maps `ReviewError` → `"Not processed: {exc}"` (row untouched) and `ExecutionError` → `"Review claimed, but execution failed: {exc}"`. `_resolve` (approve-by-link) burns the token via `consume_token` BEFORE `approve()`, so a closed-market check must run before that burn.

- [ ] **Step 1: Failing tests.**

Append to `tests/test_web_reviews.py` (fixtures `client`, and helper `queue_one` already exist there):

```python
import json as _json

from tests.test_reviews import BUY_INSTR, OPT_PICK, OptionExecutor


def queue_option(client, instruction=BUY_INSTR):
    q = client.app.state.holder.get().queue
    return q.add_option_order(
        strategy_id="s1", rule_id="entry-call", ticker="NVDA", rule_type="hard",
        condition="price < 212", action="buy_call $1000 dte>=30 otm=5%",
        snapshot={"price": "210", "preview": _json.loads(OPT_PICK.model_dump_json())},
        instruction=instruction)


def _market(monkeypatch, is_open):
    monkeypatch.setattr("allpath_trade.market_hours.is_us_market_open",
                        lambda now=None: is_open)


def test_pending_option_order_card_shows_preview(client):
    queue_option(client)
    body = client.get("/reviews").text
    assert "NVDA261016C00220000" in body and "re-priced at approval" in body


def test_approve_option_order_during_market_hours(client, monkeypatch):
    _market(monkeypatch, True)
    q = client.app.state.holder.get().queue
    monkeypatch.setattr(q, "_executor", OptionExecutor())
    rid = queue_option(client)
    r = client.post(f"/reviews/{rid}/approve", follow_redirects=True)
    assert "bought 2x NVDA261016C00220000" in r.text
    assert q.get(rid)["status"] == "approved"
    # resolved card renders the stored summary
    assert "bought 2x NVDA261016C00220000" in client.get("/reviews?status=approved").text \
        or "bought 2x" in client.get("/reviews").text


def test_approve_option_order_while_market_closed_stays_pending(client, monkeypatch):
    _market(monkeypatch, False)
    q = client.app.state.holder.get().queue
    monkeypatch.setattr(q, "_executor", OptionExecutor())
    rid = queue_option(client)
    r = client.post(f"/reviews/{rid}/approve", follow_redirects=True)
    assert "market is closed" in r.text
    assert q.get(rid)["status"] == "pending"


def test_resolved_option_order_failure_renders_error(client, monkeypatch):
    _market(monkeypatch, True)
    q = client.app.state.holder.get().queue
    ex = OptionExecutor()

    def boom(ticker):
        raise RuntimeError("quote down")
    ex.data.get_quote = boom
    monkeypatch.setattr(q, "_executor", ex)
    rid = queue_option(client)
    r = client.post(f"/reviews/{rid}/approve", follow_redirects=True)
    assert "execution failed" in r.text and "quote down" in r.text
```

Before writing the resolved-card assertion, read how `tests/test_web_reviews.py` already lists resolved rows (grep `status=` / `history` in that file) and use that exact URL instead of the `or` fallback shown above.

Append to `tests/test_web_approve.py`, mirroring `test_post_approve_with_token_in_body_executes_and_burns_the_token` (line ~183) for how it posts the token (form field `k`) and reads the plaintext token off the `ReviewHandle`:

```python
from tests.test_reviews import BUY_INSTR, OPT_PICK, OptionExecutor


def _queue_option_link(client):
    import json
    q = client.app.state.holder.get().queue
    handle = q.add_option_order(
        strategy_id="s1", rule_id="entry-call", ticker="NVDA", rule_type="hard",
        condition="price < 212", action="buy_call $1000 dte>=30 otm=5%",
        snapshot={"price": "210", "preview": json.loads(OPT_PICK.model_dump_json())},
        instruction=BUY_INSTR)
    return q, int(handle), handle.token


def test_option_link_while_market_closed_keeps_the_link_alive(client, monkeypatch):
    monkeypatch.setattr("allpath_trade.market_hours.is_us_market_open", lambda now=None: False)
    q, rid, token = _queue_option_link(client)
    monkeypatch.setattr(q, "_executor", OptionExecutor())
    r = client.post(f"/a/{rid}/approve", data={"k": token})
    assert "market is closed" in r.text
    row = q.get(rid)
    assert row["status"] == "pending" and row["approval_token_hash"]


def test_option_link_during_market_hours_approves(client, monkeypatch):
    monkeypatch.setattr("allpath_trade.market_hours.is_us_market_open", lambda now=None: True)
    q, rid, token = _queue_option_link(client)
    monkeypatch.setattr(q, "_executor", OptionExecutor())
    r = client.post(f"/a/{rid}/approve", data={"k": token})
    assert "bought 2x NVDA261016C00220000" in r.text
    assert q.get(rid)["status"] == "approved"


def test_option_link_confirm_page_shows_preview(client):
    _q, rid, token = _queue_option_link(client)
    r = client.get(f"/a/{rid}?k={token}")
    assert "NVDA261016C00220000" in r.text and "re-priced at approval" in r.text
```

(Check `ReviewHandle`'s token attribute name at `allpath_trade/store/reviews.py:27` and the `/a/` prefix constant `PREFIX` in `approve.py`; adjust the two names if they differ.)

- [ ] **Step 2: Run — expect failures.** `uv run pytest tests/test_web_reviews.py tests/test_web_approve.py -q`

- [ ] **Step 3: Implement.**

`web/routes/reviews.py` `approve` handler — insert after the `if kind == "shadow_edit":` block and before `if not result.submitted:`:

```python
    if kind == "option_order":
        # approve() returns OptionApprovalResult here -- no `.decision`;
        # the queue already wrote preview-vs-actual into execution_result.
        _echo_resolution(request, account, review_id, row_source, result.summary)
        if result.submitted:
            return _back_to_reviews_ok(f"Approved #{review_id} — {result.summary}")
        return _back_to_reviews(
            f"Approved #{review_id}, but no option order was placed: {result.summary}")
```

`_review_card.html` — replace the intent block (currently `{% if item['intent'] %} … {{ item['intent']['ticker'] }} </p> {% endif %}`) with:

```jinja
  {% if item['kind'] == 'option_order' and item['intent'] %}
  {% set oi = item['intent'] %}
  {% set pv = item['snapshot'].get('preview') if item['snapshot'] else none %}
  <p style="font-family:var(--mono);font-size:14px;margin:6px 0">
    {% if oi['op'] == 'buy' %}
    buy {{ oi['right'] }} · {{ oi['underlying'] }} · budget {{ oi['budget'] | money }}
    {% if pv %}<br><span class="muted">At trigger: {{ pv['qty'] }}x {{ pv['occ_symbol'] }} ≈ {{ pv['est_premium'] | money }} — re-priced at approval</span>{% endif %}
    {% else %}
    close options · {{ oi['underlying'] }}
    <br><span class="muted">At trigger: {% for p in oi['positions_at_trigger'] %}{{ p['occ_symbol'] }} x{{ p['qty'] }}{% if not loop.last %}, {% endif %}{% endfor %} — re-checked at approval</span>
    {% endif %}
  </p>
  {% elif item['intent'] %}
  <p style="font-family:var(--mono);font-size:14px;margin:6px 0">
    {{ item['intent']['side'] }}
    {% if item['intent']['qty'] %}{{ item['intent']['qty'] }} shares{% else %}{{ item['intent']['notional'] | money }}{% endif %}
    {{ item['intent']['ticker'] }}
  </p>
  {% endif %}
```

In the resolved block, add this branch right after the `{% elif item['kind'] == 'shadow_edit' %} … {% endif %}` branch and before `{% elif er and er.get('error') %}`:

```jinja
    {% elif item['kind'] == 'option_order' %}
    {% if er and er.get('error') %}
    Execution failed (review claimed, order status unknown) — {{ er['error'] }}
    {% elif er and er.get('summary') %}
    {{ er['summary'] }}
    {% else %}
    {{ item['status'] }}
    {% endif %}
```

`web/routes/approve.py`:

1. Imports: `from allpath_trade import market_hours` and add `OPTION_MARKET_CLOSED_MESSAGE` to the existing `allpath_trade.store.reviews` import.
2. In `_resolve`, inside the existing `if not reject:` block, right after the M6 `preview["kind"] == "order"` check (still before `consume_token`):

```python
        if (preview is not None and preview["kind"] == "option_order"
                and not market_hours.is_us_market_open()):
            return _result_page(
                request, ok=False, burned=False, account=b.account,
                message=(f"Not processed: {OPTION_MARKET_CLOSED_MESSAGE}. "
                         "This link still works during regular hours."))
```

3. After the `if row["kind"] == "shadow_edit":` result branch, before `if not result.submitted:`:

```python
    if row["kind"] == "option_order":
        return _result_page(request, ok=result.submitted, account=b.account,
                            message=f"Approved #{review_id} — {result.summary}")
```

4. In `_confirm_context`, after the `strategy_revision` branch's `return ctx` and before `intent = json.loads(...)`:

```python
    if kind == "option_order":
        # No stock price context: approve_confirm.html gates the price block
        # on kind == "order". The contract is re-picked at approval.
        instruction = json.loads(row["intent"]) if row["intent"] else {}
        snapshot = json.loads(row["snapshot"]) if row["snapshot"] else {}
        preview = snapshot.get("preview")
        if instruction.get("op") == "buy":
            ctx["side"] = f"Buy {instruction.get('right', 'option')}"
            if isinstance(preview, dict):
                ctx["amount_label"] = (
                    f"At trigger: {preview['qty']}x {preview['occ_symbol']} ≈ "
                    f"${Decimal(str(preview['est_premium'])):,.2f} — re-priced at approval")
        else:
            ctx["side"] = "Close options"
            refs = instruction.get("positions_at_trigger", [])
            ctx["amount_label"] = ("At trigger: "
                                   + ", ".join(f"{p['occ_symbol']} x{p['qty']}" for p in refs)
                                   + " — re-checked at approval")
        return ctx
```

Then read `approve_confirm.html` around its `{% if kind == "order" %}` block (line ~33) and confirm `side`/`amount_label` render outside that gate; if they render only inside it, add an `{% elif kind == "option_order" %}` branch that renders `{{ side }}` and `{{ amount_label }}` with the same markup.

- [ ] **Step 4: Run** the two test files, then the full suite. Expected: pass.

- [ ] **Step 5: Commit** — `feat: option_order on the Pending page and approve-by-link`

---

### Task 7: Telegram buttons and CLI

**Files:**
- Modify: `allpath_trade/telegram.py` (`_resolve_review_callback`, ~line 1081)
- Modify: `allpath_trade/cli.py` (`reviews approve`, ~line 220)
- Test: `tests/test_telegram_poller.py`, `tests/test_cli_phase2.py`

**Interfaces:**
- Consumes: `OptionApprovalResult`, `OPTION_MARKET_CLOSED_MESSAGE`, `market_hours.is_us_market_open()`; test fakes from `tests/test_reviews.py` (`OptionExecutor`, `BUY_INSTR`, `OPT_PICK`).

Existing behavior to rely on: Telegram does NOT call `consume_token`; it compares the button nonce (`approval_token_hash[:16]`) and then calls `queue.approve()`, which clears the token only when it claims the row. `_resolve_review_callback` returns `(outcome_line, toast, acted)`; `acted=False` means "leave the buttons and send no follow-up message" (only the toast pops up). The CLI's outer `except ReviewError` already prints `error: {exc}` to stderr and returns 1.

- [ ] **Step 1: Failing tests.**

Append to `tests/test_telegram_poller.py` (helpers `make_app_state`, `pair`, `FakeTelegramAPI`, `FakeChatService`, `_callback_update`, `make_review_poller`, `nonce_for` exist there; `connect` and `ReviewQueue` are already imported by `make_order_queue`):

```python
def _option_review(tmp_path):
    import json
    from tests.test_reviews import BUY_INSTR, OPT_PICK, OptionExecutor
    queue = ReviewQueue(connect(tmp_path / "reviews.db"), OptionExecutor())
    rid = int(queue.add_option_order(
        strategy_id="s1", rule_id="entry-call", ticker="NVDA", rule_type="hard",
        condition="price < 212", action="buy_call $1000 dte>=30 otm=5%",
        snapshot={"price": "210", "preview": json.loads(OPT_PICK.model_dump_json())},
        instruction=BUY_INSTR))
    return queue, rid


def test_option_approve_callback_while_market_closed_keeps_buttons(tmp_path, monkeypatch):
    monkeypatch.setattr("allpath_trade.market_hours.is_us_market_open", lambda now=None: False)
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    queue, rid = _option_review(tmp_path)
    nonce = nonce_for(queue, rid)
    api = FakeTelegramAPI(batches=[[
        _callback_update(1, 111, 111, f"rv:approve:{rid}:{nonce}", message_id=42)]])
    poller = make_review_poller(api, FakeChatService(), app_state, queue)

    poller.poll_once()

    assert queue.get(rid)["status"] == "pending"
    assert nonce_for(queue, rid) == nonce
    assert api.edited_markups == []
    assert "Market closed" in api.answered_callbacks[0][1]


def test_option_approve_callback_during_market_hours(tmp_path, monkeypatch):
    monkeypatch.setattr("allpath_trade.market_hours.is_us_market_open", lambda now=None: True)
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    queue, rid = _option_review(tmp_path)
    nonce = nonce_for(queue, rid)
    api = FakeTelegramAPI(batches=[[
        _callback_update(1, 111, 111, f"rv:approve:{rid}:{nonce}", message_id=42)]])
    poller = make_review_poller(api, FakeChatService(), app_state, queue)

    poller.poll_once()

    assert queue.get(rid)["status"] == "approved"
    assert api.edited_markups == [("111", 42, None)]
    assert any("bought 2x NVDA261016C00220000" in html for _cid, html in api.sent_messages)
```

Append to `tests/test_cli_phase2.py` (`setup_env`, `main`, `FakeBroker` exist there; `main` builds its components from `tmp_path/allpath-trade.db`, and with `FakeBroker` no options backend is wired):

```python
def _queue_option_cli(tmp_path):
    import json
    from allpath_trade.store.db import connect
    from allpath_trade.store.reviews import ReviewQueue
    from tests.test_reviews import BUY_INSTR, OPT_PICK
    conn = connect(tmp_path / "allpath-trade.db")
    rid = int(ReviewQueue(conn, executor=None).add_option_order(
        strategy_id="t", rule_id="entry-call", ticker="NVDA", rule_type="hard",
        condition="price < 212", action="buy_call $1000 dte>=30 otm=5%",
        snapshot={"price": "210", "preview": json.loads(OPT_PICK.model_dump_json())},
        instruction=BUY_INSTR))
    conn.close()
    return rid


def test_reviews_approve_option_order_while_market_closed(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    monkeypatch.setattr("allpath_trade.market_hours.is_us_market_open", lambda now=None: False)
    rid = _queue_option_cli(tmp_path)
    code = main(["reviews", "approve", str(rid)], broker_factory=lambda s: FakeBroker())
    assert code == 1
    assert "market is closed" in capsys.readouterr().err


def test_reviews_approve_option_order_prints_summary(tmp_path, capsys, monkeypatch):
    from tests.test_reviews import OptionExecutor
    setup_env(tmp_path, monkeypatch)
    monkeypatch.setattr("allpath_trade.market_hours.is_us_market_open", lambda now=None: True)
    rid = _queue_option_cli(tmp_path)
    monkeypatch.setattr("allpath_trade.store.reviews.ReviewQueue._executor",
                        OptionExecutor(), raising=False)
    code = main(["reviews", "approve", str(rid)], broker_factory=lambda s: FakeBroker())
    assert code == 0
    assert "bought 2x NVDA261016C00220000" in capsys.readouterr().out
```

The second CLI test patches a class attribute that `__init__` overwrites, so it will not work as written. Before implementing, read `cmd_reviews` in `allpath_trade/cli.py` (~line 170-265) and how `main` builds the queue (~line 700-790). Then inject `OptionExecutor` at that seam: monkeypatch the name `main` uses to build components so its `queue._executor` is the fake, or call `cmd_reviews` directly with a bare `ReviewQueue(conn, OptionExecutor())` if its signature takes a queue. Keep the assertions unchanged.

- [ ] **Step 2: Run — expect failures.** `uv run pytest tests/test_telegram_poller.py tests/test_cli_phase2.py -q`

- [ ] **Step 3: Implement.**

`telegram.py` — imports: `from allpath_trade import market_hours` and add `OPTION_MARKET_CLOSED_MESSAGE` to the existing `allpath_trade.store.reviews` import. In `_resolve_review_callback`, directly after the `# action == "approve"` comment and before `try: result = queue.approve(review_id)`:

```python
        if row["kind"] == "option_order" and not market_hours.is_us_market_open():
            # Checked before approve(): nothing is claimed and the nonce stays
            # valid, so acted=False keeps this message's buttons for a retry
            # during regular hours. The toast is the only feedback.
            return (f"{prefix}Not processed: {OPTION_MARKET_CLOSED_MESSAGE}",
                    _toast("Market closed — approve 09:30–16:00 ET"), False)
```

After the `if row["kind"] == "shadow_edit":` branch and before `if not result.submitted:`:

```python
        if row["kind"] == "option_order":
            _echo(result.summary)
            if result.submitted:
                return (f"{prefix}✅ Approved #{review_id} — {result.summary}",
                        _toast("Approved"), True)
            return (f"{prefix}⚠️ Approved #{review_id}, but no option order was "
                    f"placed: {result.summary}", _toast("No order placed"), True)
```

`cli.py` `reviews approve` — add before the final `else:` of the kind branches:

```python
            elif kind == "option_order":
                # approve() returns OptionApprovalResult here (no .decision).
                print(result.summary)
```

(Exit code stays 0 for any approved outcome, same as a stock order the risk gate rejected; a closed market or missing options backend raises `ReviewError`, which the existing handler turns into exit 1.)

- [ ] **Step 4: Run the two test files, then the full suite.**

- [ ] **Step 5: Commit** — `feat: option_order approvals from Telegram and the CLI`

---

### Task 8: Docs

**Files:**
- Modify: `CHANGELOG.md`, `docs/TODO.md`, `docs/experiment-autonomous-run.md`

- [ ] **Step 1:** `CHANGELOG.md` — one entry, in the file's existing style: option trades on `confirm` strategies now queue as `option_order` (preview at trigger; re-priced, risk-checked and market-hours-guarded at approval; closes execute immediately once the drawdown breaker trips; expiry sweep unchanged); shared `market_hours` helper.
- [ ] **Step 2:** `docs/TODO.md` — mark the "pending-review-queue support for option intents" known limitation as done (same done-annotation style the file uses), and note the remaining gap: no US holiday calendar for market hours.
- [ ] **Step 3:** `docs/experiment-autonomous-run.md` — add a short "Human-verify baseline (2026-09-14)" note: paper strategies are `confirm`; option trades wait in Pending; approvals are refused outside regular hours; the DTE≤1 expiry sweep and breaker-tripped closes stay automatic (safety exceptions to document in the paper).
- [ ] **Step 4:** Full suite green.
- [ ] **Step 5: Commit** — `docs: option pending queue — changelog, TODO, experiment runbook`
