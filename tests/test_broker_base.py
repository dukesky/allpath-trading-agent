from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from allpath_trade.broker.base import (
    Broker, OrderIntent, OrderSide, OptionIntent, OccParts, parse_occ_symbol
)


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


def test_parse_occ_symbol_call():
    """Test parsing a call option symbol."""
    result = parse_occ_symbol("META260918C00600000")
    assert result == OccParts(root="META", expiry=date(2026, 9, 18),
                              right="call", strike=Decimal("600"))


def test_parse_occ_symbol_put_with_fractional_strike():
    """Test parsing a put option symbol with fractional strike."""
    result = parse_occ_symbol("META260918P00123500")
    assert result == OccParts(root="META", expiry=date(2026, 9, 18),
                              right="put", strike=Decimal("123.5"))
    # Verify Decimal equality works as expected
    assert result.strike == Decimal("123.500")


def test_parse_occ_symbol_rejects_plain_stock_ticker():
    """Test that plain stock tickers return None."""
    assert parse_occ_symbol("META") is None
    assert parse_occ_symbol("BRKB") is None
    assert parse_occ_symbol("AAPL") is None


def test_parse_occ_symbol_rejects_invalid_occ():
    """Test that invalid OCC symbols return None."""
    assert parse_occ_symbol("META260918") is None  # missing right and strike
    assert parse_occ_symbol("META260918C006") is None  # strike too short
    assert parse_occ_symbol("meta260918C00600000") is None  # lowercase root
    assert parse_occ_symbol("META260918X00600000") is None  # invalid right


def test_option_intent_valid():
    """Test creating a valid OptionIntent."""
    opt = OptionIntent(
        underlying="META",
        right="call",
        occ_symbol="META260918C00600000",
        side=OrderSide.BUY,
        qty=2,
        est_premium=Decimal("1000"),
        reason="test"
    )
    assert opt.underlying == "META"
    assert opt.right == "call"
    assert opt.qty == 2
    assert opt.est_premium == Decimal("1000")


def test_option_intent_underlying_uppercases_and_validates():
    """Test that underlying is uppercased and non-empty is validated."""
    opt = OptionIntent(
        underlying="meta",
        right="put",
        occ_symbol="META260918P00123500",
        side=OrderSide.SELL,
        qty=1,
        est_premium=Decimal("500"),
        reason="test"
    )
    assert opt.underlying == "META"

    with pytest.raises(ValidationError):
        OptionIntent(
            underlying="",
            right="call",
            occ_symbol="META260918C00600000",
            side=OrderSide.BUY,
            qty=1,
            est_premium=Decimal("1000"),
            reason="test"
        )

    with pytest.raises(ValidationError):
        OptionIntent(
            underlying="   ",
            right="call",
            occ_symbol="META260918C00600000",
            side=OrderSide.BUY,
            qty=1,
            est_premium=Decimal("1000"),
            reason="test"
        )


def test_option_intent_right_validates():
    """Test that right must be 'call' or 'put'."""
    opt_call = OptionIntent(
        underlying="META",
        right="call",
        occ_symbol="META260918C00600000",
        side=OrderSide.BUY,
        qty=1,
        est_premium=Decimal("1000"),
        reason="test"
    )
    assert opt_call.right == "call"

    opt_put = OptionIntent(
        underlying="META",
        right="Put",
        occ_symbol="META260918P00123500",
        side=OrderSide.SELL,
        qty=1,
        est_premium=Decimal("500"),
        reason="test"
    )
    assert opt_put.right == "put"

    with pytest.raises(ValidationError):
        OptionIntent(
            underlying="META",
            right="invalid",
            occ_symbol="META260918C00600000",
            side=OrderSide.BUY,
            qty=1,
            est_premium=Decimal("1000"),
            reason="test"
        )


def test_option_intent_qty_validates():
    """Test that qty must be >= 1."""
    with pytest.raises(ValidationError):
        OptionIntent(
            underlying="META",
            right="call",
            occ_symbol="META260918C00600000",
            side=OrderSide.BUY,
            qty=0,
            est_premium=Decimal("1000"),
            reason="test"
        )

    with pytest.raises(ValidationError):
        OptionIntent(
            underlying="META",
            right="call",
            occ_symbol="META260918C00600000",
            side=OrderSide.BUY,
            qty=-1,
            est_premium=Decimal("1000"),
            reason="test"
        )

    ok = OptionIntent(
        underlying="META",
        right="call",
        occ_symbol="META260918C00600000",
        side=OrderSide.BUY,
        qty=1,
        est_premium=Decimal("1000"),
        reason="test"
    )
    assert ok.qty == 1


def test_option_intent_est_premium_validates():
    """Test that est_premium must be >= 0."""
    with pytest.raises(ValidationError):
        OptionIntent(
            underlying="META",
            right="call",
            occ_symbol="META260918C00600000",
            side=OrderSide.BUY,
            qty=1,
            est_premium=Decimal("-1"),
            reason="test"
        )

    ok_zero = OptionIntent(
        underlying="META",
        right="put",
        occ_symbol="META260918P00123500",
        side=OrderSide.SELL,
        qty=1,
        est_premium=Decimal("0"),
        reason="test close"
    )
    assert ok_zero.est_premium == Decimal("0")

    ok_positive = OptionIntent(
        underlying="META",
        right="call",
        occ_symbol="META260918C00600000",
        side=OrderSide.BUY,
        qty=1,
        est_premium=Decimal("1000.50"),
        reason="test"
    )
    assert ok_positive.est_premium == Decimal("1000.50")
