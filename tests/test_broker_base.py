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
