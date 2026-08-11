from datetime import UTC, datetime, timedelta
from decimal import Decimal

from allpath_trade.broker.base import Order, OrderIntent, OrderSide, OrderStatus
from allpath_trade.risk.gate import RiskDecision
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal


def make_journal(tmp_path):
    return TradeJournal(connect(tmp_path / "t.db"))


INTENT = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=Decimal(500),
                     reason="dip buy", strategy_id="aapl-long")
ORDER = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
              notional=Decimal(500), status=OrderStatus.FILLED,
              filled_qty=Decimal("2.5"), filled_avg_price=Decimal(200),
              submitted_at=datetime(2026, 7, 30, 15, 0, tzinfo=UTC),
              filled_at=datetime(2026, 7, 30, 15, 0, tzinfo=UTC))


def test_record_submitted_and_recent(tmp_path):
    j = make_journal(tmp_path)
    rid = j.record(INTENT, RiskDecision(approved=True), ORDER)
    assert rid == 1
    [row] = j.recent()
    assert row["ticker"] == "AAPL"
    assert row["status"] == "filled"
    assert row["broker_order_id"] == "o1"
    assert row["strategy_id"] == "aapl-long"


def test_record_rejected(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=False, reasons=["too big"]), None)
    [row] = j.recent()
    assert row["status"] == "rejected"
    assert "too big" in row["risk_reasons"]


def test_trades_today_counts_only_executed_today(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=True), ORDER)
    j.record(INTENT, RiskDecision(approved=False, reasons=["x"]), None)  # rejected: not counted
    assert j.trades_today() == 1


def test_record_status_override_wins(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=False, reasons=["boom"]), None,
              status_override="error")
    [row] = j.recent()
    assert row["status"] == "error"
    assert "boom" in row["risk_reasons"]


def test_trades_today_excludes_error_status(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=True), ORDER)
    j.record(INTENT, RiskDecision(approved=False, reasons=["data error: boom"]), None,
              status_override="error")  # errored: not counted
    assert j.trades_today() == 1


def test_schema_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    connect(path)
    connect(path)  # second connect must not fail


def test_record_persists_fill_details_from_order(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=True), ORDER)
    [row] = j.recent()
    assert row["filled_qty"] == "2.5"
    assert row["filled_avg_price"] == "200"


def test_record_fill_details_are_null_without_order(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=False, reasons=["x"]), None)
    [row] = j.recent()
    assert row["filled_qty"] is None
    assert row["filled_avg_price"] is None


def test_refresh_fill_updates_row(tmp_path):
    j = make_journal(tmp_path)
    submitted = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                      notional=Decimal(500), status=OrderStatus.SUBMITTED,
                      filled_qty=Decimal(0), filled_avg_price=None,
                      submitted_at=datetime(2026, 7, 30, 15, 0, tzinfo=UTC))
    trade_id = j.record(INTENT, RiskDecision(approved=True), submitted)
    [row] = j.recent()
    assert row["status"] == "submitted"
    assert row["filled_qty"] == "0"
    assert row["filled_avg_price"] is None

    filled = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                   notional=Decimal(500), status=OrderStatus.FILLED,
                   filled_qty=Decimal("2.5"), filled_avg_price=Decimal(200),
                   submitted_at=submitted.submitted_at)
    j.refresh_fill(trade_id, filled)
    [row] = j.recent()
    assert row["status"] == "filled"
    assert row["filled_qty"] == "2.5"
    assert row["filled_avg_price"] == "200"


def test_record_persists_filled_at_from_order(tmp_path):
    j = make_journal(tmp_path)
    j.record(INTENT, RiskDecision(approved=True), ORDER)
    [row] = j.recent()
    assert row["filled_at"] == "2026-07-30T15:00:00+00:00"


def test_record_filled_at_is_null_without_a_fill(tmp_path):
    j = make_journal(tmp_path)
    submitted = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                      notional=Decimal(500), status=OrderStatus.SUBMITTED,
                      filled_qty=Decimal(0), filled_avg_price=None,
                      submitted_at=datetime(2026, 7, 30, 15, 0, tzinfo=UTC))
    j.record(INTENT, RiskDecision(approved=True), submitted)
    [row] = j.recent()
    assert row["filled_at"] is None


def test_refresh_fill_writes_filled_at(tmp_path):
    j = make_journal(tmp_path)
    submitted = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                      notional=Decimal(500), status=OrderStatus.SUBMITTED,
                      filled_qty=Decimal(0), filled_avg_price=None,
                      submitted_at=datetime(2026, 8, 9, 20, 27, tzinfo=UTC))
    trade_id = j.record(INTENT, RiskDecision(approved=True), submitted)

    filled = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                   notional=Decimal(500), status=OrderStatus.FILLED,
                   filled_qty=Decimal(1), filled_avg_price=Decimal("332.01"),
                   submitted_at=submitted.submitted_at,
                   filled_at=datetime(2026, 8, 10, 13, 34, tzinfo=UTC))
    j.refresh_fill(trade_id, filled)
    [row] = j.recent()
    assert row["filled_at"] == "2026-08-10T13:34:00+00:00"


def test_refresh_fill_never_regresses_an_already_filled_row(tmp_path):
    # Task 1 P6 review: a stale/out-of-order poll must not downgrade a row
    # that already reads FILLED back to submitted or wipe its fill data.
    j = make_journal(tmp_path)
    trade_id = j.record(INTENT, RiskDecision(approved=True), ORDER)  # already filled

    stale = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                  notional=Decimal(500), status=OrderStatus.SUBMITTED,
                  filled_qty=Decimal(0), filled_avg_price=None,
                  submitted_at=ORDER.submitted_at)
    j.refresh_fill(trade_id, stale)
    [row] = j.recent()
    assert row["status"] == "filled"
    assert row["filled_qty"] == "2.5"
    assert row["filled_avg_price"] == "200"


def test_unfilled_recent_returns_only_submitted_rows_with_a_broker_order_id(tmp_path):
    j = make_journal(tmp_path)
    submitted = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=None,
                      notional=Decimal(500), status=OrderStatus.SUBMITTED,
                      filled_qty=Decimal(0), filled_avg_price=None,
                      submitted_at=datetime.now(UTC))
    j.record(INTENT, RiskDecision(approved=True), submitted)  # submitted, unfilled
    j.record(INTENT, RiskDecision(approved=True), ORDER)      # already filled
    j.record(INTENT, RiskDecision(approved=False, reasons=["x"]), None)  # rejected

    rows = j.unfilled_recent()
    assert len(rows) == 1
    assert rows[0]["broker_order_id"] == "o1"
    assert rows[0]["status"] == "submitted"


def test_unfilled_recent_excludes_rows_outside_the_window(tmp_path):
    j = make_journal(tmp_path)
    old_ts = (datetime.now(UTC) - timedelta(hours=72)).isoformat()
    j._conn.execute(
        "INSERT INTO trades (ts, ticker, side, qty, notional, status, reason,"
        " strategy_id, risk_reasons, broker_order_id, filled_qty, filled_avg_price)"
        " VALUES (?, 'AAPL', 'buy', '1', NULL, 'submitted', 'old', NULL, '[]',"
        " 'o-old', '0', NULL)",
        (old_ts,))
    j._conn.commit()

    assert j.unfilled_recent(hours=48) == []
