from datetime import UTC, datetime
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
              submitted_at=datetime(2026, 7, 30, 15, 0, tzinfo=UTC))


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
