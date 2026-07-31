from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradewind.broker.base import (
    Account,
    Broker,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Position,
)
from tradewind.data.base import DataSource, Quote
from tradewind.execution import ExecutionError, Executor
from tradewind.risk.gate import RiskDecision, RiskGate, RiskLimits
from tradewind.store.db import connect
from tradewind.store.journal import TradeJournal


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

    def __init__(self, fail=False):
        self.fail = fail
        self.submitted = []

    def get_account(self):
        return Account(equity=Decimal(10000), cash=Decimal(8000),
                       buying_power=Decimal(10000))

    def get_positions(self):
        return [Position(ticker="AAPL", qty=Decimal(10),
                         avg_entry_price=Decimal(190),
                         market_value=Decimal(2000),
                         unrealized_pl=Decimal(100))]

    def get_order(self, order_id):
        raise NotImplementedError

    def get_orders(self, open_only=True):
        return []

    def submit_order(self, intent):
        if self.fail:
            raise RuntimeError("alpaca 500")
        self.submitted.append(intent)
        return Order(id="o1", ticker=intent.ticker, side=intent.side,
                     qty=intent.qty, notional=intent.notional,
                     status=OrderStatus.SUBMITTED, filled_qty=Decimal(0),
                     filled_avg_price=None,
                     submitted_at=datetime.now(UTC))

    def cancel_order(self, order_id):
        pass


def make_executor(tmp_path, fail=False, limits=None):
    journal = TradeJournal(connect(tmp_path / "t.db"))
    broker = FakeBroker(fail=fail)
    ex = Executor(broker, RiskGate(limits or RiskLimits()), journal, FakeData())
    return ex, broker, journal


def buy(notional="500"):
    return OrderIntent(ticker="AAPL", side=OrderSide.BUY,
                       notional=Decimal(notional), reason="t")


def buy_qty(qty="1"):
    return OrderIntent(ticker="AAPL", side=OrderSide.BUY,
                       qty=Decimal(qty), reason="t")


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
