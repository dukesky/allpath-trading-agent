from decimal import Decimal
from pathlib import Path

import pytest

from allpath_trade.risk.breaker import BreakerTrip, DrawdownBreaker
from allpath_trade.store.app_state import AppState
from allpath_trade.store.db import connect
from allpath_trade.strategy.model import Authorization
from allpath_trade.strategy.store import StrategyStore

AUTO_STRAT = """\
name: "A"
status: active
version: 1
authorization: auto
thesis: "Test strategy"
position: {ticker: AAPL, target_weight: 10%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""

NOTIFY_STRAT = """\
name: "N"
status: active
version: 1
authorization: notify
thesis: "Test strategy"
position: {ticker: MSFT, target_weight: 10%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""


def _breaker(tmp_path: Path, halt: str = "0.15", account: str = "paper",
             strategies_yaml: dict[str, str] | None = None):
    """Returns (breaker, app_state, store); strategies_yaml is a dict of
    id -> yaml text written into the store directory first."""
    strategies_yaml = strategies_yaml or {}

    # Set up database and app state
    conn = connect(tmp_path / "t.db")
    app_state = AppState(conn)

    # Set up strategy store
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir(exist_ok=True)
    for strategy_id, yaml_text in strategies_yaml.items():
        (strategies_dir / f"{strategy_id}.yaml").write_text(yaml_text)

    store = StrategyStore(strategies_dir, conn, account=account)

    # Create breaker
    breaker = DrawdownBreaker(
        app_state=app_state,
        strategies=store,
        halt_pct=Decimal(halt),
        account=account
    )

    return breaker, app_state, store


def test_no_trip_below_threshold(tmp_path):
    b, state, _ = _breaker(tmp_path)
    assert b.check(Decimal("100000")) is None       # sets peak
    assert b.check(Decimal("90000")) is None        # -10% < 15%
    assert state.get("drawdown_peak:paper") == "100000"


def test_peak_ratchets_up(tmp_path):
    b, state, _ = _breaker(tmp_path)
    b.check(Decimal("100000"))
    b.check(Decimal("120000"))
    assert state.get("drawdown_peak:paper") == "120000"


def test_trip_demotes_auto_strategies_once(tmp_path):
    b, state, store = _breaker(tmp_path, strategies_yaml={
        "a": AUTO_STRAT, "n": NOTIFY_STRAT})
    b.check(Decimal("100000"))
    trip = b.check(Decimal("80000"))                # -20%
    assert trip is not None
    assert trip.demoted == ["a"]
    assert store.load("a").authorization == Authorization.CONFIRM
    assert store.load("n").authorization == Authorization.NOTIFY  # untouched
    assert b.tripped_at() is not None
    assert b.check(Decimal("70000")) is None        # already tripped: silent


def test_disabled_at_zero(tmp_path):
    b, state, _ = _breaker(tmp_path, halt="0")
    assert b.check(Decimal("100000")) is None
    assert state.get("drawdown_peak:paper") is None  # fully inert


def test_reset_clears_peak_and_tripped(tmp_path):
    b, state, _ = _breaker(tmp_path, strategies_yaml={"a": AUTO_STRAT})
    b.check(Decimal("100000")); b.check(Decimal("80000"))
    b.reset()
    assert state.get("drawdown_peak:paper") is None
    assert b.tripped_at() is None
    # after reset the next check starts a fresh peak
    assert b.check(Decimal("80000")) is None


def test_accounts_are_isolated(tmp_path):
    # paper trips; a shadow breaker sharing the same app_state does not
    # Create separate directories for each account (like the real app does)
    conn = connect(tmp_path / "t.db")
    state = AppState(conn)

    paper_dir = tmp_path / "strategies" / "paper"
    shadow_dir = tmp_path / "strategies" / "shadow"
    paper_dir.mkdir(parents=True)
    shadow_dir.mkdir(parents=True)

    (paper_dir / "a.yaml").write_text(AUTO_STRAT)
    (shadow_dir / "a.yaml").write_text(AUTO_STRAT)

    paper_store = StrategyStore(paper_dir, conn, account="paper")
    shadow_store = StrategyStore(shadow_dir, conn, account="shadow")

    paper_b = DrawdownBreaker(state, paper_store, Decimal("0.15"), "paper")
    shadow_b = DrawdownBreaker(state, shadow_store, Decimal("0.15"), "shadow")

    # Paper's state: peak 100k, then drop to 80k triggers the breaker
    paper_b.check(Decimal("100000"))
    trip = paper_b.check(Decimal("80000"))
    assert trip is not None
    assert trip.demoted == ["a"]

    # Shadow's keys must be untouched
    assert state.get("drawdown_peak:shadow") is None
    assert state.get("drawdown_tripped:shadow") is None
    # And shadow's store must still have the strategy in auto mode
    assert shadow_store.load("a").authorization == Authorization.AUTO

    # Paper's keys must be set
    assert state.get("drawdown_peak:paper") == "100000"
    assert state.get("drawdown_tripped:paper") is not None
