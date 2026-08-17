from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from alpaca.trading.enums import QueryOrderStatus

from allpath_trade.broker.alpaca import AlpacaBroker
from allpath_trade.broker.base import OrderIntent, OrderSide, OrderStatus


def _raw_order(**over):
    base = {
        "id": "oid-1", "symbol": "AAPL", "side": SimpleNamespace(value="buy"),
        "qty": "5", "notional": None, "status": SimpleNamespace(value="filled"),
        "filled_qty": "5", "filled_avg_price": "200.5",
        "submitted_at": datetime(2026, 7, 30, 14, 0, tzinfo=UTC),
        "filled_at": datetime(2026, 7, 30, 14, 0, 5, tzinfo=UTC),
    }
    base.update(over)
    return SimpleNamespace(**base)


class StubClient:
    def __init__(self):
        self.submitted = []
        self.canceled = None
        self.last_orders_filter = None

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
        self.last_orders_filter = filter
        return [_raw_order()]

    def cancel_order_by_id(self, order_id):
        self.canceled = order_id


def make_broker():
    stub = StubClient()
    return AlpacaBroker("k", "s", paper=True, client=stub), stub


def test_get_account_maps_decimals():
    b, _ = make_broker()
    acct = b.get_account()
    assert acct.equity == Decimal(10000)
    assert acct.cash == Decimal(4000)


def test_get_positions_maps_fields():
    b, _ = make_broker()
    [p] = b.get_positions()
    assert p.ticker == "AAPL" and p.qty == Decimal(5)
    assert p.market_value == Decimal("1002.5")


def test_submit_qty_order():
    b, stub = make_broker()
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=Decimal(5), reason="test")
    order = b.submit_order(intent)
    assert order.status == OrderStatus.FILLED
    assert order.filled_avg_price == Decimal("200.5")
    req = stub.submitted[0]
    assert req.symbol == "AAPL" and req.qty == 5.0 and req.notional is None


def test_submit_notional_order():
    b, stub = make_broker()
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=Decimal(500), reason="t")
    b.submit_order(intent)
    req = stub.submitted[0]
    assert req.notional == 500.0 and req.qty is None


@pytest.mark.parametrize("status_value,expected_order_status", [
    ("filled", OrderStatus.FILLED),
    ("partially_filled", OrderStatus.PARTIALLY_FILLED),
    ("canceled", OrderStatus.CANCELED),
    ("expired", OrderStatus.CANCELED),
    ("rejected", OrderStatus.REJECTED),
    ("new", OrderStatus.SUBMITTED),  # unknown value defaults to SUBMITTED
])
def test_status_map_coverage(status_value, expected_order_status):
    raw = _raw_order(status=SimpleNamespace(value=status_value))
    order = AlpacaBroker._to_order(raw)
    assert order.status == expected_order_status


def test_get_order():
    b, _ = make_broker()
    order = b.get_order("order-123")
    assert order.id == "order-123"
    assert order.status == OrderStatus.FILLED


def test_get_order_maps_filled_at():
    b, _ = make_broker()
    order = b.get_order("order-123")
    assert order.filled_at == datetime(2026, 7, 30, 14, 0, 5, tzinfo=UTC)


def test_get_order_maps_filled_at_none_when_unfilled():
    b, _ = make_broker()
    stub = b._client
    stub.get_order_by_id = lambda order_id: _raw_order(
        id=order_id, status=SimpleNamespace(value="submitted"),
        filled_qty="0", filled_avg_price=None, filled_at=None)
    order = b.get_order("order-789")
    assert order.filled_at is None


def test_cancel_order():
    b, stub = make_broker()
    b.cancel_order("order-456")
    assert stub.canceled == "order-456"


def test_get_orders_open_only_true():
    b, stub = make_broker()
    b.get_orders(open_only=True)
    assert stub.last_orders_filter is not None
    assert stub.last_orders_filter.status == QueryOrderStatus.OPEN


def test_get_orders_open_only_false():
    b, stub = make_broker()
    b.get_orders(open_only=False)
    assert stub.last_orders_filter is not None
    assert stub.last_orders_filter.status == QueryOrderStatus.ALL


def test_get_positions_with_none_fields():
    b, stub = make_broker()
    # Override the stub to return position with None fields
    stub.get_all_positions = lambda: [
        SimpleNamespace(symbol="AAPL", qty="5", avg_entry_price="190",
                        market_value=None, unrealized_pl=None)
    ]
    [p] = b.get_positions()
    assert p.ticker == "AAPL"
    assert p.market_value == Decimal(0)
    assert p.unrealized_pl == Decimal(0)


# --- Minor 6: get_equity_history -------------------------------------------

def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


class PortfolioHistoryClient(StubClient):
    """Adds `get_portfolio_history` on top of StubClient's existing stubs --
    a separate subclass rather than growing StubClient itself, since only
    these tests care about the shape of a portfolio-history response."""

    def __init__(self, timestamps, equities):
        super().__init__()
        self._history = SimpleNamespace(timestamp=timestamps, equity=equities)

    def get_portfolio_history(self, request):
        return self._history


def test_get_equity_history_filters_zero_equity_days():
    # Pre-funding days report equity as exactly 0.0 -- not a real data
    # point, and would otherwise wreck the chart's y-axis scale.
    now = datetime.now(UTC)
    timestamps = [_epoch(now - timedelta(days=2)), _epoch(now - timedelta(days=1)),
                  _epoch(now)]
    equities = [0.0, 10000.0, 10500.0]
    client = PortfolioHistoryClient(timestamps, equities)
    broker = AlpacaBroker("k", "s", paper=True, client=client)

    points = broker.get_equity_history(days=30)

    assert len(points) == 2
    assert all(v > 0 for _, v in points)


def test_get_equity_history_respects_days_cutoff():
    now = datetime.now(UTC)
    timestamps = [_epoch(now - timedelta(days=40)), _epoch(now - timedelta(days=1))]
    equities = [9000.0, 10000.0]
    client = PortfolioHistoryClient(timestamps, equities)
    broker = AlpacaBroker("k", "s", paper=True, client=client)

    points = broker.get_equity_history(days=30)

    assert len(points) == 1
    assert points[0][1] == Decimal("10000.0")


def test_get_equity_history_raises_on_mismatched_lengths():
    # zip(..., strict=True) is what's actually doing the guarding here --
    # this proves it's wired up, not silently truncating to the shorter
    # list on a malformed response.
    client = PortfolioHistoryClient([1, 2, 3], [10000.0, 10500.0])
    broker = AlpacaBroker("k", "s", paper=True, client=client)

    with pytest.raises(ValueError):
        broker.get_equity_history(days=30)
