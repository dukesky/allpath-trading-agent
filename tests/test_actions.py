from decimal import Decimal

import pytest

from tradewind.broker.base import OrderSide, Position
from tradewind.strategy.actions import (
    ActionError, ActionKind, parse_action, to_order_intent,
)
from tradewind.strategy.model import PositionPlan, StrategyDoc

STRAT = StrategyDoc(id="s", name="s",
                    position=PositionPlan(ticker="AAPL", target_weight="15%"))
POS = Position(ticker="AAPL", qty=Decimal("10"), avg_entry_price=Decimal("180"),
               market_value=Decimal("2000"), unrealized_pl=Decimal("200"))


@pytest.mark.parametrize("text,kind,amount", [
    ("sell all", ActionKind.SELL_ALL, None),
    ("Sell 50%", ActionKind.SELL_PCT, Decimal("50")),
    ("sell $5,000", ActionKind.SELL_VALUE, Decimal("5000")),
    ("buy $3000", ActionKind.BUY_VALUE, Decimal("3000")),
    ("buy to target_weight", ActionKind.BUY_TO_TARGET, None),
])
def test_parse(text, kind, amount):
    spec = parse_action(text)
    assert spec.kind == kind and spec.amount == amount


@pytest.mark.parametrize("bad", ["sell", "buy 50%", "sell -10%", "hold", "buy $0", "sell 0%"])
def test_parse_rejects(bad):
    with pytest.raises(ActionError):
        parse_action(bad)


def kw(**over):
    base = dict(strategy=STRAT, rule_id="r", price=Decimal("200"),
                position=POS, equity=Decimal("10000"), reason="t")
    base.update(over)
    return base


def test_sell_all_uses_position_qty():
    intent = to_order_intent(parse_action("sell all"), **kw())
    assert intent.side == OrderSide.SELL and intent.qty == Decimal("10")
    assert intent.strategy_id == "s"


def test_sell_pct_quantizes_qty():
    intent = to_order_intent(parse_action("sell 33%"), **kw())
    assert intent.qty == Decimal("3.3000")


def test_sell_with_no_position_returns_none():
    assert to_order_intent(parse_action("sell all"), **kw(position=None)) is None


def test_sell_value_passes_notional():
    intent = to_order_intent(parse_action("sell $1500"), **kw())
    assert intent.notional == Decimal("1500")


def test_buy_value():
    intent = to_order_intent(parse_action("buy $3000"), **kw())
    assert intent.side == OrderSide.BUY and intent.notional == Decimal("3000")


def test_buy_to_target_computes_gap():
    # target 15% of 10000 = 1500; fresh position value = 10 * 200 = 2000 -> at/above target
    assert to_order_intent(parse_action("buy to target_weight"), **kw()) is None
    # with smaller position: 10*100=1000 < 1500 -> buy 500
    intent = to_order_intent(parse_action("buy to target_weight"), **kw(price=Decimal("100")))
    assert intent.notional == Decimal("500.00")


def test_buy_to_target_with_value_mode():
    strat = StrategyDoc(id="v", name="v",
                        position=PositionPlan(ticker="AAPL", target_value="$5000"))
    intent = to_order_intent(parse_action("buy to target_weight"),
                             **kw(strategy=strat))
    assert intent.notional == Decimal("3000.00")  # 5000 - 10*200
