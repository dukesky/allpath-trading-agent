from decimal import Decimal

import pytest

from allpath_trade.broker.base import OrderSide, Position
from allpath_trade.strategy.actions import (
    ActionError,
    ActionKind,
    is_option_action,
    parse_action,
    to_order_intent,
)
from allpath_trade.strategy.model import PositionPlan, StrategyDoc

STRAT = StrategyDoc(id="s", name="s",
                    position=PositionPlan(ticker="AAPL", target_weight="15%"))
POS = Position(ticker="AAPL", qty=Decimal(10), avg_entry_price=Decimal(180),
               market_value=Decimal(2000), unrealized_pl=Decimal(200))


@pytest.mark.parametrize("text,kind,amount", [
    ("sell all", ActionKind.SELL_ALL, None),
    ("Sell 50%", ActionKind.SELL_PCT, Decimal(50)),
    ("sell $5,000", ActionKind.SELL_VALUE, Decimal(5000)),
    ("buy $3000", ActionKind.BUY_VALUE, Decimal(3000)),
    ("buy to target_weight", ActionKind.BUY_TO_TARGET, None),
])
def test_parse(text, kind, amount):
    spec = parse_action(text)
    assert spec.kind == kind and spec.amount == amount


@pytest.mark.parametrize("bad", ["sell", "buy 50%", "sell -10%", "hold", "buy $0", "sell 0%"])
def test_parse_rejects(bad):
    with pytest.raises(ActionError):
        parse_action(bad)


@pytest.mark.parametrize("bad", ["sell .%", "sell ,%", "sell $.", "buy $,", "buy $1..2"])
def test_degenerate_numbers_raise_action_error(bad):
    with pytest.raises(ActionError):
        parse_action(bad)


def kw(**over):
    base = {"strategy": STRAT, "rule_id": "r", "price": Decimal(200),
                "position": POS, "equity": Decimal(10000), "reason": "t"}
    base.update(over)
    return base


def test_sell_all_uses_position_qty():
    intent = to_order_intent(parse_action("sell all"), **kw())
    assert intent.side == OrderSide.SELL and intent.qty == Decimal(10)
    assert intent.strategy_id == "s"


def test_sell_pct_quantizes_qty():
    intent = to_order_intent(parse_action("sell 33%"), **kw())
    assert intent.qty == Decimal("3.3000")


def test_sell_with_no_position_returns_none():
    assert to_order_intent(parse_action("sell all"), **kw(position=None)) is None


def test_sell_value_passes_notional():
    intent = to_order_intent(parse_action("sell $1500"), **kw())
    assert intent.notional == Decimal(1500)


def test_buy_value():
    intent = to_order_intent(parse_action("buy $3000"), **kw())
    assert intent.side == OrderSide.BUY and intent.notional == Decimal(3000)


def test_buy_to_target_computes_gap():
    # target 15% of 10000 = 1500; fresh position value = 10 * 200 = 2000 -> at/above target
    assert to_order_intent(parse_action("buy to target_weight"), **kw()) is None
    # with smaller position: 10*100=1000 < 1500 -> buy 500
    intent = to_order_intent(parse_action("buy to target_weight"), **kw(price=Decimal(100)))
    assert intent.notional == Decimal("500.00")


def test_buy_to_target_with_value_mode():
    strat = StrategyDoc(id="v", name="v",
                        position=PositionPlan(ticker="AAPL", target_value="$5000"))
    intent = to_order_intent(parse_action("buy to target_weight"),
                             **kw(strategy=strat))
    assert intent.notional == Decimal("3000.00")  # 5000 - 10*200


# --- option action grammar -------------------------------------------------

@pytest.mark.parametrize("text,kind", [
    ("buy_call $1500", ActionKind.BUY_CALL),
    ("BUY_CALL $1500", ActionKind.BUY_CALL),
    ("buy_put $1500", ActionKind.BUY_PUT),
    ("Buy_Put $1500", ActionKind.BUY_PUT),
])
def test_parse_option_buy_bare(text, kind):
    spec = parse_action(text)
    assert spec.kind == kind
    assert spec.amount == Decimal(1500)
    assert spec.min_dte is None
    assert spec.otm_pct is None


@pytest.mark.parametrize("text,kind", [
    ("buy_call $1500 dte>=10 otm=3%", ActionKind.BUY_CALL),
    ("buy_put $1500 dte>=10 otm=3%", ActionKind.BUY_PUT),
])
def test_parse_option_buy_with_params(text, kind):
    spec = parse_action(text)
    assert spec.kind == kind
    assert spec.amount == Decimal(1500)
    assert spec.min_dte == 10
    assert spec.otm_pct == Decimal("0.03")


def test_parse_close_options():
    spec = parse_action("close_options")
    assert spec.kind == ActionKind.CLOSE_OPTIONS
    assert spec.amount is None


def test_parse_close_options_case_insensitive():
    spec = parse_action("Close_Options")
    assert spec.kind == ActionKind.CLOSE_OPTIONS


@pytest.mark.parametrize("bad", [
    "buy_call",              # missing $ amount
    "buy_call $0",           # non-positive amount
    "buy_call $1500 otm=0%",  # otm out of bounds (must be > 0)
    "buy_call $1500 otm=51%",  # otm out of bounds (must be <= 50)
    "buy_call $1500 dte>=-1",  # negative dte not a valid grammar token
    "buy_call $1500 otm=3",   # missing % sign
    "close_options now",      # close_options takes no params
])
def test_parse_option_actions_rejects(bad):
    with pytest.raises(ActionError):
        parse_action(bad)


def test_is_option_action():
    assert is_option_action(parse_action("buy_call $1500")) is True
    assert is_option_action(parse_action("buy_put $1500")) is True
    assert is_option_action(parse_action("close_options")) is True
    assert is_option_action(parse_action("sell all")) is False
    assert is_option_action(parse_action("buy $3000")) is False


def test_to_order_intent_rejects_option_spec():
    with pytest.raises(ActionError):
        to_order_intent(parse_action("buy_call $1500"), **kw())
