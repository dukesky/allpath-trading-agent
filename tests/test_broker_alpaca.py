from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from tradewind.broker.alpaca import AlpacaBroker
from tradewind.broker.base import OrderIntent, OrderSide, OrderStatus


def _raw_order(**over):
    base = dict(
        id="oid-1", symbol="AAPL", side=SimpleNamespace(value="buy"),
        qty="5", notional=None, status=SimpleNamespace(value="filled"),
        filled_qty="5", filled_avg_price="200.5",
        submitted_at=datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc),
    )
    base.update(over)
    return SimpleNamespace(**base)


class StubClient:
    def __init__(self):
        self.submitted = []

    def get_account(self):
        return SimpleNamespace(equity="10000", cash="4000", buying_power="8000")

    def get_all_positions(self):
        return [SimpleNamespace(symbol="AAPL", qty="5", avg_entry_price="190",
                                market_value="1002.5", unrealized_pl="52.5")]

    def submit_order(self, req):
        self.submitted.append(req)
        return _raw_order()

    def get_order_by_id(self, order_id):
        return _raw_order(id=order_id)

    def get_orders(self, filter=None):
        return [_raw_order()]

    def cancel_order_by_id(self, order_id):
        self.canceled = order_id


def make_broker():
    stub = StubClient()
    return AlpacaBroker("k", "s", paper=True, client=stub), stub


def test_get_account_maps_decimals():
    b, _ = make_broker()
    acct = b.get_account()
    assert acct.equity == Decimal("10000")
    assert acct.cash == Decimal("4000")


def test_get_positions_maps_fields():
    b, _ = make_broker()
    [p] = b.get_positions()
    assert p.ticker == "AAPL" and p.qty == Decimal("5")
    assert p.market_value == Decimal("1002.5")


def test_submit_qty_order():
    b, stub = make_broker()
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=Decimal("5"), reason="test")
    order = b.submit_order(intent)
    assert order.status == OrderStatus.FILLED
    assert order.filled_avg_price == Decimal("200.5")
    req = stub.submitted[0]
    assert req.symbol == "AAPL" and req.qty == 5.0 and req.notional is None


def test_submit_notional_order():
    b, stub = make_broker()
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=Decimal("500"), reason="t")
    b.submit_order(intent)
    req = stub.submitted[0]
    assert req.notional == 500.0 and req.qty is None
