import threading
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from allpath_trade import execution as execution_module
from allpath_trade.broker.base import (
    Account,
    Broker,
    OptionIntent,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Position,
)
from allpath_trade.broker.options_mcp import OptionsBackendError
from allpath_trade.data.base import DataSource, Quote
from allpath_trade.execution import ExecutionError, Executor, refresh_pending_fills
from allpath_trade.risk.gate import RiskDecision, RiskGate, RiskLimits
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal


class FakeData(DataSource):
    def get_quote(self, ticker):
        return Quote(ticker=ticker, price=Decimal(200),
                     as_of=datetime.now(UTC))

    def get_bars(self, ticker, days=365):
        return []


class FailingData(DataSource):
    def get_quote(self, ticker):
        raise RuntimeError("network down")

    def get_bars(self, ticker, days=365):
        return []


class FakeBroker(Broker):
    name = "fake"
    is_paper = True

    def __init__(self, fail=False, refill=None, refill_error=False,
                fill_immediately=False, refill_map=None):
        self.fail = fail
        self.submitted = []
        # Controls for the post-submit refresh poll (Executor.execute):
        # `refill` is the Order get_order() should return; `refill_error`
        # makes get_order() raise instead; `fill_immediately` makes
        # submit_order() itself return a FILLED order (refresh should then
        # be skipped entirely -- get_order stays NotImplementedError).
        # `refill_map` is {order_id: Order | Exception}, for tests that need
        # per-order-id behavior (the ongoing refresh_pending_fills sweep,
        # which polls several rows in one pass) rather than the single
        # blanket `refill`/`refill_error` the post-submit poll needed.
        self.refill = refill
        self.refill_error = refill_error
        self.fill_immediately = fill_immediately
        self.refill_map = refill_map or {}
        self.get_order_calls = []

    def get_account(self):
        return Account(equity=Decimal(10000), cash=Decimal(8000),
                       buying_power=Decimal(10000))

    def get_positions(self):
        return [Position(ticker="AAPL", qty=Decimal(10),
                         avg_entry_price=Decimal(190),
                         market_value=Decimal(2000),
                         unrealized_pl=Decimal(100))]

    def get_order(self, order_id):
        self.get_order_calls.append(order_id)
        if order_id in self.refill_map:
            result = self.refill_map[order_id]
            if isinstance(result, Exception):
                raise result
            return result
        if self.refill_error:
            raise RuntimeError("get_order failed")
        if self.refill is not None:
            return self.refill
        raise NotImplementedError

    def get_orders(self, open_only=True):
        return []

    def submit_order(self, intent):
        if self.fail:
            raise RuntimeError("alpaca 500")
        self.submitted.append(intent)
        status = OrderStatus.FILLED if self.fill_immediately else OrderStatus.SUBMITTED
        return Order(id="o1", ticker=intent.ticker, side=intent.side,
                     qty=intent.qty, notional=intent.notional,
                     status=status, filled_qty=Decimal(0),
                     filled_avg_price=None,
                     submitted_at=datetime.now(UTC))

    def cancel_order(self, order_id):
        pass


class FailingAccountBroker(FakeBroker):
    """get_account raises -- used to exercise execute_option's data-fetch
    failure path (mirrors FailingData for the stock path, but the option
    path never touches DataSource at all)."""

    def get_account(self):
        raise RuntimeError("broker down")


class FakeOptionsBackend:
    """Minimal `OptionsBackend` fake for the executor tests. `pick_contract`
    is unused by execute_option and deliberately left unimplemented."""

    def __init__(self, fail=False, payload=None):
        self.fail = fail
        self.payload = payload if payload is not None else {
            "id": "opt1", "status": "filled", "qty": "1",
            "filled_qty": "1", "filled_avg_price": "5.25",
        }
        self.calls = []

    def pick_contract(self, underlying, right, min_dte, otm_pct, budget, spot):
        raise NotImplementedError

    def place_option_order(self, occ_symbol, side, qty, position_intent):
        self.calls.append((occ_symbol, side, qty, position_intent))
        if self.fail:
            raise OptionsBackendError("mcp down")
        return self.payload

    def stop(self):
        pass


def make_executor(tmp_path, fail=False, limits=None):
    journal = TradeJournal(connect(tmp_path / "t.db"))
    broker = FakeBroker(fail=fail)
    ex = Executor(broker, RiskGate(limits or RiskLimits()), journal, FakeData())
    return ex, broker, journal


def make_option_executor(tmp_path, options_backend=None, limits=None, broker=None):
    journal = TradeJournal(connect(tmp_path / "t.db"))
    broker = broker if broker is not None else FakeBroker()
    ex = Executor(broker, RiskGate(limits or RiskLimits()), journal, FakeData(),
                 options_backend=options_backend)
    return ex, broker, journal


def buy(notional="500"):
    return OrderIntent(ticker="AAPL", side=OrderSide.BUY,
                       notional=Decimal(notional), reason="t")


def buy_qty(qty="1"):
    return OrderIntent(ticker="AAPL", side=OrderSide.BUY,
                       qty=Decimal(qty), reason="t")


CALL_OCC = "META260918C00600000"


def opt_buy(occ=CALL_OCC, premium="500", qty=1):
    return OptionIntent(underlying="META", right="call", occ_symbol=occ,
                        side=OrderSide.BUY, qty=qty, est_premium=Decimal(premium), reason="t")


def opt_sell(occ=CALL_OCC, premium="0", qty=1):
    return OptionIntent(underlying="META", right="call", occ_symbol=occ,
                        side=OrderSide.SELL, qty=qty, est_premium=Decimal(premium), reason="t")


def test_approved_intent_is_submitted_and_journaled(tmp_path):
    ex, broker, journal = make_executor(tmp_path)
    res = ex.execute(buy())
    assert res.submitted and res.order.id == "o1"
    assert len(broker.submitted) == 1
    [row] = journal.recent()
    assert row["status"] == "submitted"


def test_rejected_intent_never_reaches_broker(tmp_path):
    ex, broker, journal = make_executor(tmp_path)
    res = ex.execute(buy("6000"))  # over max_order_value
    assert not res.submitted and res.order is None
    assert broker.submitted == []
    [row] = journal.recent()
    assert row["status"] == "rejected"


def test_broker_failure_is_journaled_and_raised(tmp_path):
    ex, _, journal = make_executor(tmp_path, fail=True)
    with pytest.raises(ExecutionError):
        ex.execute(buy())
    [row] = journal.recent()
    assert row["status"] == "error"
    assert "alpaca 500" in row["risk_reasons"]


def test_qty_intent_data_failure_is_journaled_and_raised(tmp_path):
    journal = TradeJournal(connect(tmp_path / "t.db"))
    broker = FakeBroker()
    ex = Executor(broker, RiskGate(RiskLimits()), journal, FailingData())
    with pytest.raises(ExecutionError):
        ex.execute(buy_qty())
    [row] = journal.recent()
    assert row["status"] == "error"
    assert "data error" in row["risk_reasons"]
    assert broker.submitted == []


def test_notional_intent_data_failure_never_calls_get_quote(tmp_path):
    journal = TradeJournal(connect(tmp_path / "t.db"))
    broker = FakeBroker()
    ex = Executor(broker, RiskGate(RiskLimits()), journal, FailingData())
    res = ex.execute(buy())  # notional intent: no price lookup needed
    assert res.submitted
    assert broker.submitted == [buy()]  # would have raised if get_quote were called


def test_qty_intent_price_flows_into_gate(tmp_path):
    # qty=1 * price=200 (from FakeData) = 200, which exceeds max_order_value=100
    ex, broker, _journal = make_executor(tmp_path, limits=RiskLimits(max_order_value=Decimal(100)))
    res = ex.execute(buy_qty("1"))
    assert not res.submitted
    assert broker.submitted == []
    assert any("exceeds max_order_value" in r for r in res.decision.reasons)


def test_successful_submit_refreshes_fill_when_not_filled(tmp_path):
    ex, broker, journal = make_executor(tmp_path)
    broker.refill = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                          notional=Decimal(500), status=OrderStatus.FILLED,
                          filled_qty=Decimal("2.5"), filled_avg_price=Decimal(200),
                          submitted_at=datetime.now(UTC))
    res = ex.execute(buy())
    assert res.submitted
    assert broker.get_order_calls == ["o1"]
    [row] = journal.recent()
    assert row["status"] == "filled"
    assert row["filled_qty"] == "2.5"
    assert row["filled_avg_price"] == "200"


def test_refresh_failure_leaves_submitted_row_untouched(tmp_path):
    ex, broker, journal = make_executor(tmp_path)
    broker.refill_error = True
    res = ex.execute(buy())
    assert res.submitted  # the failed refresh poll must not fail the submit
    [row] = journal.recent()
    assert row["status"] == "submitted"
    assert row["filled_qty"] == "0"  # as-submitted, not backfilled by the failed poll
    assert row["filled_avg_price"] is None


def test_refresh_write_failure_leaves_submitted_row_untouched(tmp_path, monkeypatch):
    """refresh_fill DB write failure must not escape execute().

    The comment at line 76 promises: "a failed poll ... leave the
    as-submitted row alone rather than retrying or raising". That guarantee
    extends to the write: poll + write degrade together, not separately.
    If refresh_fill() raises, the order stays journaled as submitted."""
    ex, broker, journal = make_executor(tmp_path)
    refreshed_order = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                            notional=Decimal(500), status=OrderStatus.FILLED,
                            filled_qty=Decimal("2.5"), filled_avg_price=Decimal(200),
                            submitted_at=datetime.now(UTC))
    broker.refill = refreshed_order

    # Monkeypatch refresh_fill to raise a DB error
    def failing_refresh_fill(trade_id, order):
        raise RuntimeError("db locked")
    monkeypatch.setattr(journal, "refresh_fill", failing_refresh_fill)

    # execute() must return success, not raise
    res = ex.execute(buy())
    assert res.submitted
    assert res.order.id == "o1"

    # Journal row must be as-submitted, not updated
    [row] = journal.recent()
    assert row["status"] == "submitted"
    assert row["filled_qty"] == "0"
    assert row["filled_avg_price"] is None


def test_already_filled_order_skips_refresh_poll(tmp_path):
    ex, broker, journal = make_executor(tmp_path)
    broker.fill_immediately = True
    res = ex.execute(buy())
    assert res.submitted
    assert broker.get_order_calls == []  # no round trip needed
    [row] = journal.recent()
    assert row["status"] == "filled"


def test_refresh_pending_fills_updates_a_stale_submitted_row(tmp_path):
    journal = TradeJournal(connect(tmp_path / "t.db"))
    submitted = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                      notional=Decimal(500), status=OrderStatus.SUBMITTED,
                      filled_qty=Decimal(0), filled_avg_price=None,
                      submitted_at=datetime(2026, 8, 9, 20, 27, tzinfo=UTC))
    trade_id = journal.record(buy(), RiskDecision(approved=True), submitted)

    filled = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                   notional=Decimal(500), status=OrderStatus.FILLED,
                   filled_qty=Decimal(1), filled_avg_price=Decimal("332.01"),
                   submitted_at=submitted.submitted_at,
                   filled_at=datetime(2026, 8, 10, 13, 34, tzinfo=UTC))
    broker = FakeBroker(refill_map={"o1": filled})

    refresh_pending_fills(journal, broker)

    [row] = journal.recent()
    assert row["id"] == trade_id
    assert row["status"] == "filled"
    assert row["filled_avg_price"] == "332.01"
    assert row["filled_at"] == "2026-08-10T13:34:00+00:00"


def test_refresh_pending_fills_one_bad_row_does_not_break_the_others(tmp_path):
    journal = TradeJournal(connect(tmp_path / "t.db"))
    submitted_at = datetime(2026, 8, 9, 20, 27, tzinfo=UTC)

    def make_submitted(order_id):
        return Order(id=order_id, ticker="AAPL", side=OrderSide.BUY, qty=None,
                    notional=Decimal(500), status=OrderStatus.SUBMITTED,
                    filled_qty=Decimal(0), filled_avg_price=None,
                    submitted_at=submitted_at)

    bad_id = journal.record(buy(), RiskDecision(approved=True), make_submitted("bad"))
    good_id = journal.record(buy(), RiskDecision(approved=True), make_submitted("good"))

    filled_good = Order(id="good", ticker="AAPL", side=OrderSide.BUY, qty=None,
                        notional=Decimal(500), status=OrderStatus.FILLED,
                        filled_qty=Decimal(1), filled_avg_price=Decimal("332.01"),
                        submitted_at=submitted_at,
                        filled_at=datetime(2026, 8, 10, 13, 34, tzinfo=UTC))
    broker = FakeBroker(refill_map={
        "bad": RuntimeError("dead broker"),
        "good": filled_good,
    })

    refresh_pending_fills(journal, broker)  # must not raise

    rows = {r["id"]: r for r in journal.recent()}
    assert rows[bad_id]["status"] == "submitted"  # untouched, not crashed
    assert rows[good_id]["status"] == "filled"


def test_refresh_pending_fills_caps_at_twenty_rows(tmp_path):
    journal = TradeJournal(connect(tmp_path / "t.db"))
    submitted_at = datetime(2026, 8, 9, 20, 27, tzinfo=UTC)
    for i in range(25):
        journal.record(buy(), RiskDecision(approved=True), Order(
            id=f"o{i}", ticker="AAPL", side=OrderSide.BUY, qty=None,
            notional=Decimal(500), status=OrderStatus.SUBMITTED,
            filled_qty=Decimal(0), filled_avg_price=None, submitted_at=submitted_at))

    broker = FakeBroker(refill_error=True)  # would raise for every id polled
    refresh_pending_fills(journal, broker)

    assert len(broker.get_order_calls) == 20


def test_refresh_pending_fills_reports_one_line_of_failures_to_stderr(tmp_path, capsys):
    # M1: per-row failures are silent otherwise -- one summary line per
    # sweep (not one per row, which would spam the log during a broker
    # outage) is the only signal an operator gets.
    journal = TradeJournal(connect(tmp_path / "t.db"))
    submitted_at = datetime(2026, 8, 9, 20, 27, tzinfo=UTC)
    journal.record(buy(), RiskDecision(approved=True), Order(
        id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
        notional=Decimal(500), status=OrderStatus.SUBMITTED,
        filled_qty=Decimal(0), filled_avg_price=None, submitted_at=submitted_at))

    broker = FakeBroker(refill_error=True)
    refresh_pending_fills(journal, broker)

    err = capsys.readouterr().err
    assert "[fill-refresh] 1 of 1 refreshes failed" in err


def test_refresh_pending_fills_no_failures_prints_nothing(tmp_path, capsys):
    journal = TradeJournal(connect(tmp_path / "t.db"))
    submitted_at = datetime(2026, 8, 9, 20, 27, tzinfo=UTC)
    trade_id = journal.record(buy(), RiskDecision(approved=True), Order(
        id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
        notional=Decimal(500), status=OrderStatus.SUBMITTED,
        filled_qty=Decimal(0), filled_avg_price=None, submitted_at=submitted_at))
    filled = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                   notional=Decimal(500), status=OrderStatus.FILLED,
                   filled_qty=Decimal(1), filled_avg_price=Decimal("332.01"),
                   submitted_at=submitted_at, filled_at=submitted_at)
    broker = FakeBroker(refill_map={"o1": filled})

    refresh_pending_fills(journal, broker)

    assert capsys.readouterr().err == ""
    assert journal.recent()[0]["id"] == trade_id  # sanity: it did run


def test_refresh_pending_fills_converges_an_expired_row_to_canceled(tmp_path):
    # I2: a DAY order that expired at the broker long ago must stop
    # affirmatively claiming "fill pending" forever. AlpacaBroker already
    # collapses the broker's "expired" onto OrderStatus.CANCELED (see
    # broker/alpaca.py's _STATUS_MAP), so the row this sweep writes back is
    # CANCELED with its fill columns left NULL -- not fabricated as a fill.
    journal = TradeJournal(connect(tmp_path / "t.db"))
    old_submitted_at = datetime(2026, 8, 2, 14, 0, tzinfo=UTC)  # days before "now"
    trade_id = journal.record(buy(), RiskDecision(approved=True), Order(
        id="stale-nvda", ticker="AAPL", side=OrderSide.BUY, qty=None,
        notional=Decimal(500), status=OrderStatus.SUBMITTED,
        filled_qty=Decimal(0), filled_avg_price=None, submitted_at=old_submitted_at))

    expired = Order(id="stale-nvda", ticker="AAPL", side=OrderSide.BUY, qty=None,
                    notional=Decimal(500), status=OrderStatus.CANCELED,
                    filled_qty=Decimal(0), filled_avg_price=None,
                    submitted_at=old_submitted_at)
    broker = FakeBroker(refill_map={"stale-nvda": expired})

    refresh_pending_fills(journal, broker)

    [row] = journal.recent()
    assert row["id"] == trade_id
    assert row["status"] == "canceled"
    assert row["filled_qty"] == "0"
    assert row["filled_avg_price"] is None
    # A row this old used to be permanently excluded from unfilled_recent's
    # window and could never converge -- proving it's gone from the still-
    # unresolved set now is the real regression guard.
    assert journal.unfilled_recent() == []


def test_refresh_pending_fills_still_never_regresses_a_filled_row(tmp_path):
    # The FILLED guard (TradeJournal.refresh_fill) must survive both this
    # round's transaction() wrap and the bounded-poll changes.
    journal = TradeJournal(connect(tmp_path / "t.db"))
    submitted_at = datetime(2026, 8, 9, 20, 27, tzinfo=UTC)
    filled_order = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                         notional=Decimal(500), status=OrderStatus.FILLED,
                         filled_qty=Decimal("2.5"), filled_avg_price=Decimal(200),
                         submitted_at=submitted_at, filled_at=submitted_at)
    trade_id = journal.record(buy(), RiskDecision(approved=True), filled_order)  # already filled

    # unfilled_recent won't even select this row (status != submitted/
    # partially_filled), so refresh_fill's guard is exercised directly here,
    # same as test_journal.py's own coverage of the guard.
    stale = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                  notional=Decimal(500), status=OrderStatus.SUBMITTED,
                  filled_qty=Decimal(0), filled_avg_price=None,
                  submitted_at=submitted_at)
    journal.refresh_fill(trade_id, stale)

    [row] = journal.recent()
    assert row["status"] == "filled"
    assert row["filled_avg_price"] == "200"


def test_refresh_pending_fills_bounds_each_row_by_a_wall_clock_timeout(tmp_path, monkeypatch, capsys):
    # I4: alpaca-py passes no timeout to its underlying requests call and
    # retries 429/504 three times with 3s sleeps -- a single get_order()
    # can hang well past what's acceptable on the scheduler thread, which
    # runs this sweep unconditionally on every sentinel tick (market open
    # or closed). A short, injected per-row timeout (not the real 10s
    # production one) keeps this test fast; mirrors test_models_catalog.py's
    # identical pattern for the same underlying problem.
    journal = TradeJournal(connect(tmp_path / "t.db"))
    submitted_at = datetime(2026, 8, 9, 20, 27, tzinfo=UTC)
    trade_id = journal.record(buy(), RiskDecision(approved=True), Order(
        id="stuck", ticker="AAPL", side=OrderSide.BUY, qty=None,
        notional=Decimal(500), status=OrderStatus.SUBMITTED,
        filled_qty=Decimal(0), filled_avg_price=None, submitted_at=submitted_at))

    deadline = 0.2
    monkeypatch.setattr(execution_module, "_ORDER_POLL_TIMEOUT_SECONDS", deadline)

    finished = threading.Event()

    class HangingBroker(FakeBroker):
        def get_order(self, order_id):
            self.get_order_calls.append(order_id)
            time.sleep(deadline * 5)
            finished.set()
            return Order(id=order_id, ticker="AAPL", side=OrderSide.BUY, qty=None,
                        notional=Decimal(500), status=OrderStatus.FILLED,
                        filled_qty=Decimal(1), filled_avg_price=Decimal(200),
                        submitted_at=submitted_at)

    broker = HangingBroker()

    started = time.monotonic()
    refresh_pending_fills(journal, broker)
    elapsed = time.monotonic() - started

    # Generous bound (2x the deadline) to avoid flakiness under CI
    # scheduling jitter, while still proving the wait is bounded rather
    # than open-ended -- the hanging broker sleeps 5x the deadline, so this
    # can only pass if .result(timeout=...) actually cut the wait short.
    assert elapsed < deadline * 2
    [row] = journal.recent()
    assert row["id"] == trade_id
    assert row["status"] == "submitted"  # untouched: the poll never returned in time

    err = capsys.readouterr().err
    assert "[fill-refresh] 1 of 1 refreshes failed" in err

    # Let the abandoned background poll actually finish before this test
    # returns: _poll_pool is a module-level singleton, so a still-running
    # thread here could still be executing when the next test submits its
    # own work to the same single-worker pool.
    assert finished.wait(timeout=deadline * 20)


def test_refresh_pending_fills_respects_a_whole_sweep_deadline(tmp_path, monkeypatch):
    # I4: bounding each row individually still leaves a worst case of
    # row_count x per_row_timeout for one sweep if the broker is merely
    # slow rather than hung -- too long for something running
    # unconditionally on every sentinel tick. A whole-sweep deadline caps
    # that: once it passes, remaining rows wait for the next pass instead
    # of being processed now.
    journal = TradeJournal(connect(tmp_path / "t.db"))
    submitted_at = datetime(2026, 8, 9, 20, 27, tzinfo=UTC)
    ids = [f"o{i}" for i in range(3)]
    for order_id in ids:
        journal.record(buy(), RiskDecision(approved=True), Order(
            id=order_id, ticker="AAPL", side=OrderSide.BUY, qty=None,
            notional=Decimal(500), status=OrderStatus.SUBMITTED,
            filled_qty=Decimal(0), filled_avg_price=None, submitted_at=submitted_at))
    refill_map = {
        order_id: Order(id=order_id, ticker="AAPL", side=OrderSide.BUY, qty=None,
                        notional=Decimal(500), status=OrderStatus.FILLED,
                        filled_qty=Decimal(1), filled_avg_price=Decimal(200),
                        submitted_at=submitted_at)
        for order_id in ids
    }
    broker = FakeBroker(refill_map=refill_map)

    monkeypatch.setattr(execution_module, "_SWEEP_DEADLINE_SECONDS", 20)
    # A deterministic fake clock, not a real sleep: first call sets the
    # deadline (0 + 20 = 20); the second call (the first per-row check)
    # reads 15 (< 20, one row processed); the third call reads 30 (>= 20,
    # loop breaks before a second row).
    fake_times = iter([0, 15, 30])
    monkeypatch.setattr(execution_module.time, "monotonic", lambda: next(fake_times))

    refresh_pending_fills(journal, broker)

    assert len(broker.get_order_calls) == 1  # the other two waited for the next pass


def test_trades_today_from_journal_feeds_daily_cap(tmp_path):
    ex, broker, journal = make_executor(tmp_path, limits=RiskLimits(max_daily_trades=1))
    filled = Order(id="o0", ticker="AAPL", side=OrderSide.BUY, qty=None,
                   notional=Decimal(500), status=OrderStatus.FILLED,
                   filled_qty=Decimal("2.5"), filled_avg_price=Decimal(200),
                   submitted_at=datetime.now(UTC))
    journal.record(buy(), RiskDecision(approved=True), filled)
    res = ex.execute(buy())
    assert not res.submitted
    assert broker.submitted == []
    assert any("daily trade limit" in r for r in res.decision.reasons)


# -- execute_option -----------------------------------------------------------

def test_approved_option_buy_is_submitted_and_journaled(tmp_path):
    backend = FakeOptionsBackend()
    ex, _broker, journal = make_option_executor(tmp_path, options_backend=backend)
    res = ex.execute_option(opt_buy(premium="500"))
    assert res.submitted
    assert res.order.id == "opt1"
    assert backend.calls == [(CALL_OCC, "buy", 1, "buy_to_open")]
    [row] = journal.recent()
    assert row["status"] == "filled"
    assert row["ticker"] == CALL_OCC
    assert row["side"] == "buy"
    assert row["qty"] == "1"
    assert row["notional"] is None


def test_approved_option_sell_uses_sell_to_close(tmp_path):
    backend = FakeOptionsBackend()
    ex, _broker, journal = make_option_executor(tmp_path, options_backend=backend)
    res = ex.execute_option(opt_sell(premium="0"))
    assert res.submitted
    assert backend.calls == [(CALL_OCC, "sell", 1, "sell_to_close")]
    [row] = journal.recent()
    assert row["side"] == "sell"


def test_option_gate_rejection_never_reaches_backend(tmp_path):
    backend = FakeOptionsBackend()
    ex, _broker, journal = make_option_executor(
        tmp_path, options_backend=backend, limits=RiskLimits(max_order_value=Decimal(100)))
    res = ex.execute_option(opt_buy(premium="500"))  # exceeds max_order_value
    assert not res.submitted and res.order is None
    assert backend.calls == []
    [row] = journal.recent()
    assert row["status"] == "rejected"


def test_option_sell_close_bypasses_value_caps_but_gate_still_runs(tmp_path):
    # A close is exempt from the premium/exposure caps but still goes
    # through the gate (and, e.g., the daily-trade cap still applies).
    backend = FakeOptionsBackend()
    ex, _broker, journal = make_option_executor(
        tmp_path, options_backend=backend, limits=RiskLimits(max_order_value=Decimal(1)))
    res = ex.execute_option(opt_sell(premium="99999"))
    assert res.submitted
    [row] = journal.recent()
    assert row["status"] == "filled"


def test_option_disabled_backend_raises(tmp_path):
    ex, _broker, journal = make_option_executor(tmp_path, options_backend=None)
    with pytest.raises(ExecutionError):
        ex.execute_option(opt_buy(premium="500"))
    [row] = journal.recent()
    assert row["status"] == "error"
    assert "options trading disabled" in row["risk_reasons"]


def test_option_backend_error_is_journaled_and_raised(tmp_path):
    backend = FakeOptionsBackend(fail=True)
    ex, _broker, journal = make_option_executor(tmp_path, options_backend=backend)
    with pytest.raises(ExecutionError):
        ex.execute_option(opt_buy(premium="500"))
    [row] = journal.recent()
    assert row["status"] == "error"
    assert "mcp down" in row["risk_reasons"]


def test_option_data_failure_is_journaled_and_raised(tmp_path):
    backend = FakeOptionsBackend()
    ex, _broker, journal = make_option_executor(
        tmp_path, options_backend=backend, broker=FailingAccountBroker())
    with pytest.raises(ExecutionError):
        ex.execute_option(opt_buy(premium="500"))
    [row] = journal.recent()
    assert row["status"] == "error"
    assert "data error" in row["risk_reasons"]
    assert backend.calls == []


def test_option_execute_never_fetches_a_stock_quote(tmp_path):
    # An OCC symbol like "META260918C00600000" would break a stock-quote
    # lookup (yfinance) -- execute_option must never call self.data at all.
    journal = TradeJournal(connect(tmp_path / "t.db"))
    broker = FakeBroker()
    backend = FakeOptionsBackend()
    ex = Executor(broker, RiskGate(RiskLimits()), journal, FailingData(),
                 options_backend=backend)
    res = ex.execute_option(opt_buy(premium="500"))
    assert res.submitted  # would have raised if get_quote were called


def test_option_order_built_defensively_from_minimal_payload(tmp_path):
    backend = FakeOptionsBackend(payload={"id": "opt2", "status": "submitted"})
    ex, _broker, journal = make_option_executor(tmp_path, options_backend=backend)
    before = datetime.now(UTC)
    res = ex.execute_option(opt_buy(qty=3, premium="500"))
    assert res.submitted
    assert res.order.status == OrderStatus.SUBMITTED
    assert res.order.qty == Decimal(3)  # falls back to intent.qty
    assert res.order.filled_qty == Decimal(0)
    assert res.order.filled_avg_price is None
    assert res.order.submitted_at >= before  # falls back to now() since payload has none
    [row] = journal.recent()
    assert row["status"] == "submitted"
    assert row["filled_qty"] is None or row["filled_qty"] == "0"


def test_option_unknown_payload_status_defaults_to_submitted(tmp_path):
    backend = FakeOptionsBackend(payload={"id": "opt3", "status": "new"})
    ex, _broker, journal = make_option_executor(tmp_path, options_backend=backend)
    res = ex.execute_option(opt_buy(premium="500"))
    assert res.order.status == OrderStatus.SUBMITTED
    [row] = journal.recent()
    assert row["status"] == "submitted"


def test_option_malformed_payload_field_journals_error_and_raises(tmp_path):
    # Reviewer-flagged Critical (task-5 fix round): the broker call already
    # succeeded here -- a real order was placed -- before a present-but-
    # unparseable numeric field (filled_qty="N/A") blows up Decimal(). That
    # must not escape as a raw, unjournaled exception: the trade WAS placed
    # and needs to be counted (daily cap, exposure) even though we can't
    # read its fill details.
    backend = FakeOptionsBackend(
        payload={"id": "opt4", "status": "filled", "filled_qty": "N/A"})
    ex, _broker, journal = make_option_executor(tmp_path, options_backend=backend)
    with pytest.raises(ExecutionError, match="unparseable"):
        ex.execute_option(opt_buy(premium="500"))
    [row] = journal.recent()
    assert row["status"] == "error"
    assert "order placed but response unparseable" in row["risk_reasons"]
    # The order was genuinely submitted to the broker -- confirm the fake
    # actually received the call, distinguishing this from a rejection.
    assert backend.calls == [(CALL_OCC, "buy", 1, "buy_to_open")]


def test_option_filled_status_without_filled_qty_degrades_to_submitted(tmp_path):
    # Reviewer-flagged Important (task-5 fix round): status="filled" with no
    # usable filled_qty (missing or under a field name we don't read) would
    # otherwise journal a self-contradictory filled/0 row. Degrading to
    # SUBMITTED keeps the row honest -- fill-refresh reconciliation can
    # still correct it later once real fill data is available.
    backend = FakeOptionsBackend(payload={"id": "opt5", "status": "filled"})
    ex, _broker, journal = make_option_executor(tmp_path, options_backend=backend)
    res = ex.execute_option(opt_buy(premium="500"))
    assert res.submitted
    assert res.order.status == OrderStatus.SUBMITTED
    assert res.order.filled_qty == Decimal(0)
    [row] = journal.recent()
    assert row["status"] == "submitted"


def test_option_trades_today_shares_cap_with_stock_trades(tmp_path):
    backend = FakeOptionsBackend()
    ex, _broker, journal = make_option_executor(
        tmp_path, options_backend=backend, limits=RiskLimits(max_daily_trades=1))
    filled = Order(id="o0", ticker="AAPL", side=OrderSide.BUY, qty=None,
                   notional=Decimal(500), status=OrderStatus.FILLED,
                   filled_qty=Decimal("2.5"), filled_avg_price=Decimal(200),
                   submitted_at=datetime.now(UTC))
    journal.record(buy(), RiskDecision(approved=True), filled)
    res = ex.execute_option(opt_buy(premium="100"))
    assert not res.submitted
    assert backend.calls == []
    assert any("daily trade limit" in r for r in res.decision.reasons)
