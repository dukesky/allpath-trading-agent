from decimal import Decimal

import pytest
from pydantic import ValidationError

from allpath_trade.broker.base import Broker, OrderIntent, OrderSide


def test_intent_requires_exactly_one_of_qty_or_notional():
    with pytest.raises(ValidationError):
        OrderIntent(ticker="AAPL", side=OrderSide.BUY, reason="x")
    with pytest.raises(ValidationError):
        OrderIntent(
            ticker="AAPL", side=OrderSide.BUY, qty=Decimal(1),
            notional=Decimal(100), reason="x",
        )
    ok = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=Decimal(100), reason="x")
    assert ok.notional == Decimal(100)


def test_intent_uppercases_ticker_and_rejects_nonpositive():
    i = OrderIntent(ticker="aapl", side=OrderSide.SELL, qty=Decimal(2), reason="x")
    assert i.ticker == "AAPL"
    with pytest.raises(ValidationError):
        OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=Decimal(0), reason="x")
    with pytest.raises(ValidationError):
        OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=Decimal(-5), reason="x")


def test_broker_is_abstract():
    with pytest.raises(TypeError):
        Broker()  # type: ignore[abstract]


def test_decimal_fields_coerce_floats_precision_safely():
    """Regression test: pydantic v2 coerces floats to Decimal via str() for precision safety.
    E.g., 1.1 becomes Decimal("1.1"), not Decimal(1.1) with binary float artifact."""
    from allpath_trade.broker.base import Account
    acct = Account(equity=1.1, cash=2.2, buying_power=3.3)
    assert acct.equity == Decimal("1.1")  # str-based coercion, no binary float artifact
    assert acct.cash == Decimal("2.2")
    assert acct.buying_power == Decimal("3.3")


def test_order_intent_notional_coerces_float_precision_safely():
    """Regression test: OrderIntent.notional accepts float and coerces via str() for precision."""
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=1.1, reason="test")
    assert intent.notional == Decimal("1.1")


def test_intent_rejects_empty_or_whitespace_ticker():
    with pytest.raises(ValidationError):
        OrderIntent(ticker="", side=OrderSide.BUY, notional=Decimal(100), reason="x")
    with pytest.raises(ValidationError):
        OrderIntent(ticker="   ", side=OrderSide.BUY, notional=Decimal(100), reason="x")


def test_order_filled_at_defaults_to_none():
    from datetime import UTC, datetime

    from allpath_trade.broker.base import Order

    order = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=Decimal(1),
                  notional=None, status="submitted", filled_qty=Decimal(0),
                  filled_avg_price=None,
                  submitted_at=datetime(2026, 8, 9, 20, 27, tzinfo=UTC))
    assert order.filled_at is None


def test_intent_rejects_magnitude_over_max():
    """Infinity/NaN are already rejected by pydantic's finite_number check
    (Decimal fields), but a huge-but-finite value like 1e400 sailed straight
    through -- exactly as ledger-bricking downstream as the infinite case
    once it hits equity/market_value arithmetic. Same cap shadow_tools'
    _parse_money already enforces for the shadow ledger's own numeric
    inputs."""
    from allpath_trade.broker.base import _MAX_MAGNITUDE

    with pytest.raises(ValidationError):
        OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=Decimal("1e400"), reason="x")
    with pytest.raises(ValidationError):
        OrderIntent(ticker="AAPL", side=OrderSide.BUY,
                    notional=_MAX_MAGNITUDE * 2, reason="x")
    # Exactly at the cap is still fine -- only strictly over it is rejected.
    ok = OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=_MAX_MAGNITUDE, reason="x")
    assert ok.qty == _MAX_MAGNITUDE


def test_order_filled_at_accepts_a_datetime():
    from datetime import UTC, datetime

    from allpath_trade.broker.base import Order

    filled_at = datetime(2026, 8, 10, 13, 34, tzinfo=UTC)
    order = Order(id="o1", ticker="AAPL", side=OrderSide.BUY, qty=Decimal(1),
                  notional=None, status="filled", filled_qty=Decimal(1),
                  filled_avg_price=Decimal("332.01"),
                  submitted_at=datetime(2026, 8, 9, 20, 27, tzinfo=UTC),
                  filled_at=filled_at)
    assert order.filled_at == filled_at
