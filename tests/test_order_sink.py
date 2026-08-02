from decimal import Decimal

import pytest

from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.risk.gate import RiskGate, RiskLimits
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.web.order_sink import QueueingOrderSink
from tests.test_sentinel import FakeBroker


class FakeData:
    def get_price(self, ticker: str) -> Decimal:
        return Decimal(100)


@pytest.fixture
def sink(tmp_path):
    conn = connect(tmp_path / "t.db")
    queue = ReviewQueue(conn, None)
    broker = FakeBroker()
    return QueueingOrderSink(queue, RiskGate(RiskLimits()), broker, FakeData(),
                             TradeJournal(conn), conversation_id=7), queue


def test_proposal_is_queued_not_executed(sink):
    s, queue = sink
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional="1000",
                         reason="rebalance")
    message = s.propose(intent)
    rows = queue.list("pending")
    assert len(rows) == 1
    assert rows[0]["source"] == "chat"
    assert rows[0]["conversation_id"] == 7
    assert "#" in message


def test_risk_preview_is_recorded(sink):
    s, queue = sink
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional="999999",
                         reason="too big")
    s.propose(intent)
    row = queue.list("pending")[0]
    assert "max_order_value" in row["risk_preview"]


def test_preview_failure_still_queues_the_proposal(sink, monkeypatch):
    s, queue = sink

    def boom(*a, **k):
        raise RuntimeError("no quote")

    monkeypatch.setattr(s.data, "get_price", boom)
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional="100",
                         reason="x")
    s.propose(intent)
    assert len(queue.list("pending")) == 1
