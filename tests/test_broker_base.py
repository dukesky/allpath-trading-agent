from decimal import Decimal

import pytest
from pydantic import ValidationError

from tradewind.broker.base import Broker, OrderIntent, OrderSide


def test_intent_requires_exactly_one_of_qty_or_notional():
    with pytest.raises(ValidationError):
        OrderIntent(ticker="AAPL", side=OrderSide.BUY, reason="x")
    with pytest.raises(ValidationError):
        OrderIntent(
            ticker="AAPL", side=OrderSide.BUY, qty=Decimal("1"),
            notional=Decimal("100"), reason="x",
        )
    ok = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=Decimal("100"), reason="x")
    assert ok.notional == Decimal("100")


def test_intent_uppercases_ticker_and_rejects_nonpositive():
    i = OrderIntent(ticker="aapl", side=OrderSide.SELL, qty=Decimal("2"), reason="x")
    assert i.ticker == "AAPL"
    with pytest.raises(ValidationError):
        OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=Decimal("0"), reason="x")
    with pytest.raises(ValidationError):
        OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=Decimal("-5"), reason="x")


def test_broker_is_abstract():
    with pytest.raises(TypeError):
        Broker()  # type: ignore[abstract]


def test_decimal_fields_coerce_floats_precision_safely():
    """Regression test: pydantic v2 coerces floats to Decimal via str() for precision safety.
    E.g., 1.1 becomes Decimal("1.1"), not Decimal(1.1) with binary float artifact."""
    from tradewind.broker.base import Account
    acct = Account(equity=1.1, cash=2.2, buying_power=3.3)
    assert acct.equity == Decimal("1.1")  # str-based coercion, no binary float artifact
    assert acct.cash == Decimal("2.2")
    assert acct.buying_power == Decimal("3.3")


def test_order_intent_notional_coerces_float_precision_safely():
    """Regression test: OrderIntent.notional accepts float and coerces via str() for precision."""
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=1.1, reason="test")
    assert intent.notional == Decimal("1.1")
