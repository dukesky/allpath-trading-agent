"""Spent one-shot stops (ops 2026-11-06, item 6): a stop-loss without
`rearm` fires once and stays `triggered`, while re-arming buy rules rebuild
the position. On 11-06 four paper positions (NVDA, TSM, MU, AMD, ~54% of
equity) had no armed stop. These helpers make that visible; they never
change a rule's state."""

from __future__ import annotations

from decimal import Decimal

import pytest

from allpath_trade.broker.base import Position
from allpath_trade.store.db import connect
from allpath_trade.strategy.model import Rule, RuleState
from allpath_trade.strategy.stops import (
    is_stop_rule,
    no_armed_stop_line,
    stop_warning,
    unprotected_positions,
)
from allpath_trade.strategy.store import StrategyStore


def rule(condition, action, state=RuleState.ARMED):
    return Rule(id="r", type="hard", condition=condition, action=action, state=state)


@pytest.mark.parametrize("condition,action,expected", [
    ("price < 205", "sell all", True),
    ("price <= 205", "sell 50%", True),
    ("pnl_pct < -8", "sell all", True),
    ("205 > price", "sell all", True),
    ("price < 205 and position_weight > 0", "sell $1000", True),
    ("price > 248", "sell all", False),            # take-profit
    ("price < 213", "buy $3000", False),           # dip buy
    ("price < 205", "close_options", False),       # options, not the stock
    ("position_weight > 0.2", "sell 10%", False),  # trim, no price floor
])
def test_is_stop_rule(condition, action, expected):
    assert is_stop_rule(rule(condition, action)) is expected


NVDA = """
name: "N"
status: active
authorization: confirm
position: {ticker: NVDA, target_weight: 15%}
rules:
  - {id: dip-buy-2, type: hard, condition: "price < 213", action: "buy $3000", rearm: 90m}
  - {id: stop-loss, type: hard, condition: "price < 205", action: "sell all"}
"""

AAPL = """
name: "A"
status: active
authorization: confirm
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: stop-loss, type: hard, condition: "price < 150", action: "sell all"}
"""

TSLA_NO_STOP = """
name: "T"
status: active
authorization: confirm
position: {ticker: TSLA, target_weight: 15%}
rules:
  - {id: buy, type: hard, condition: "price < 300", action: "buy $3000"}
"""


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "strategies"
    d.mkdir()
    (d / "nvda.yaml").write_text(NVDA)
    (d / "aapl.yaml").write_text(AAPL)
    (d / "tsla.yaml").write_text(TSLA_NO_STOP)
    return StrategyStore(d, connect(tmp_path / "t.db"))


def spend(store, strategy_id, rule_id="stop-loss"):
    from datetime import UTC, datetime

    store.record_fire(strategy_id, rule_id, datetime(2026, 10, 30, 16, 16, tzinfo=UTC))
    store.set_rule_state(strategy_id, rule_id, RuleState.TRIGGERED)


def test_no_warning_while_the_stop_is_armed(store):
    assert stop_warning(store.load("nvda"), store) == ""


def test_warning_names_the_spent_stop_and_when_it_fired(store):
    spend(store, "nvda")
    text = stop_warning(store.load("nvda"), store)
    assert text.startswith("No armed stop:")
    assert "nvda/stop-loss (price < 205)" in text
    assert "2026-10-30" in text


def test_a_strategy_that_never_had_a_stop_gets_no_warning(store):
    # Only SPENT stops are flagged; "no stop by design" (most shadow
    # strategies) is not this bug.
    assert stop_warning(store.load("tsla"), store) == ""


def positions():
    return [
        Position(ticker="NVDA", qty=Decimal("88.93"), avg_entry_price=Decimal(202),
                 market_value=Decimal(16738), unrealized_pl=Decimal(0)),
        Position(ticker="AAPL", qty=Decimal(10), avg_entry_price=Decimal(300),
                 market_value=Decimal(3000), unrealized_pl=Decimal(0)),
        Position(ticker="TSLA", qty=Decimal(5), avg_entry_price=Decimal(300),
                 market_value=Decimal(1500), unrealized_pl=Decimal(0)),
    ]


def test_unprotected_positions_lists_held_tickers_with_only_spent_stops(store):
    spend(store, "nvda")
    rows = unprotected_positions(store, positions(), Decimal(90000))
    assert [r["ticker"] for r in rows] == ["NVDA"]
    [nvda] = rows
    assert nvda["weight"] == Decimal(16738) / Decimal(90000)
    assert nvda["rules"] == ["nvda/stop-loss (price < 205), spent 2026-10-30"]


def test_no_armed_stop_line(store):
    spend(store, "nvda")
    line = no_armed_stop_line(store, positions(), Decimal(90000))
    assert line.startswith("⚠ No armed stop on 1 held position(s):")
    assert "NVDA 88.93 sh (18.6%)" in line
    assert "nvda/stop-loss (price < 205), spent 2026-10-30" in line


def test_no_armed_stop_line_is_empty_when_everything_is_covered(store):
    assert no_armed_stop_line(store, positions(), Decimal(90000)) == ""
