from datetime import UTC, datetime
from decimal import Decimal

from tradewind.broker.base import Order, OrderIntent, OrderSide, OrderStatus
from tradewind.risk.gate import RiskDecision
from tradewind.store.db import connect
from tradewind.store.journal import TradeJournal


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


def test_schema_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    connect(path)
    connect(path)  # second connect must not fail
