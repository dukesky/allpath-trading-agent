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
from tradewind.risk.gate import RiskGate, RiskLimits
from tradewind.store.db import connect
from tradewind.store.journal import TradeJournal


class FakeData(DataSource):
    def get_quote(self, ticker):
        return Quote(ticker=ticker, price=Decimal(200),
                     as_of=datetime.now(UTC))

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
    assert row["status"] == "rejected"
    assert "alpaca 500" in row["risk_reasons"]
