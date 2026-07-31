from decimal import Decimal

import pytest

from tradewind.strategy.conditions import (
    ConditionError, evaluate_condition, parse_condition,
)

CTX = {
    "price": Decimal("200"),
    "position_weight": Decimal("0.10"),
    "position_qty": Decimal("5"),
    "avg_entry_price": Decimal("180"),
    "pnl_pct": Decimal("11.1"),
    "target_weight": Decimal("0.15"),
}


@pytest.mark.parametrize("expr,expected", [
    ("price < 185", False),
    ("price >= 200", True),
    ("price == 200", True),
    ("pnl_pct > 10 and position_weight < target_weight", True),
    ("pnl_pct > 20 or price < 250", True),
    ("not (price > 300)", True),
    ("price > -5", True),
    ("position_qty <= 5 and (price < 100 or pnl_pct > 5)", True),
])
def test_evaluate(expr, expected):
    assert evaluate_condition(expr, CTX) is expected


@pytest.mark.parametrize("bad", [
    "__import__('os')",          # call
    "price + 1 < 2",             # arithmetic not in v1
    "foo < 1",                   # unknown variable
    "price < '185'",             # string literal
    "[1,2]",                     # list
    "price",                     # not boolean
    "lambda: 1",
    "price < 1; price > 2",
])
def test_rejects_bad_expressions(bad):
    with pytest.raises(ConditionError):
        parse_condition(bad)


def test_missing_context_key_raises():
    with pytest.raises(ConditionError):
        evaluate_condition("price < 1", {})


def test_decimal_precision():
    assert evaluate_condition("price == 0.1", {"price": Decimal("0.1")}) is True
