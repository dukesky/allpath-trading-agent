import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from allpath_trade.broker.base import Position
from allpath_trade.config import Settings
from allpath_trade.data.base import Quote
from allpath_trade.scheduler import SENTINEL_HEARTBEAT_KEY, SENTINEL_MARKET_OPEN_KEY
from allpath_trade.strategy.model import (
    Authorization,
    PositionPlan,
    Rule,
    RuleState,
    RuleType,
    StrategyDoc,
    StrategyStatus,
)
from allpath_trade.web.app import create_app
from allpath_trade.web.routes import dashboard as dashboard_route
from allpath_trade.web.routes.dashboard import (
    sentinel_heartbeat_status,
    summarize_strategy,
)
from tests.helpers import assert_english_only
from tests.test_sentinel import FakeBroker

STRAT = """
name: "Semis core"
status: active
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""


class FakeDataSource:
    """Deterministic stand-in for YFinanceSource -- the real one hits the
    network on every call, which the dashboard now does on every page
    load. Tests must never depend on network access."""

    def __init__(self, price: str = "210.00"):
        self.price = Decimal(price)
        self.calls: list[str] = []
        self.fail = False

    def get_quote(self, ticker: str) -> Quote:
        self.calls.append(ticker)
        if self.fail:
            raise RuntimeError("no price available")
        return Quote(ticker=ticker, price=self.price, as_of=datetime.now(UTC))

    def get_bars(self, ticker: str, days: int = 365):
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _clear_quote_cache():
    # The cache is module-level so it survives across *requests*, which is
    # the point -- but that means it also survives across *tests* sharing
    # the same ticker (AAPL) unless cleared, making assertions depend on
    # test order.
    dashboard_route._quote_cache.clear()
    yield
    dashboard_route._quote_cache.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "semis.yaml").write_text(STRAT)
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        # /login redirects to "/" on success and TestClient follows
        # redirects by default -- that first dashboard render must already
        # see the fake data source, or it hits the real (network-backed)
        # YFinanceSource and poisons the quote cache with a live price
        # before any test gets a chance to control it.
        monkeypatch.setattr(c.app.state.holder.get(), "data", FakeDataSource())
        c.post("/login", data={"token": "secret"})
        yield c


def test_dashboard_shows_account_and_strategies(client):
    body = client.get("/").text
    assert "Dashboard" in body
    assert "Semis core" in body


def test_dashboard_is_english_only(client):
    assert_english_only(client.get("/").text)


def test_broker_outage_does_not_break_the_page(client, monkeypatch):
    holder = client.app.state.holder

    def boom():
        raise RuntimeError("broker down")

    monkeypatch.setattr(holder.get().broker, "get_account", boom)
    r = client.get("/")
    assert r.status_code == 200
    assert "unavailable" in r.text.lower()


def test_a_hung_broker_call_degrades_to_the_banner_instead_of_holding_the_page(
        client, monkeypatch):
    # A5: a sync FastAPI handler runs in a bounded thread pool -- a broker
    # call that just hangs (a phone that keeps reloading against a stalled
    # Alpaca connection) must not be able to hold that worker, and enough
    # concurrent hangs would otherwise starve every other page (login, chat,
    # reviews) of workers too. The request must come back promptly with the
    # existing "Broker unavailable" banner, not hang for as long as the
    # broker call does.
    monkeypatch.setattr(dashboard_route, "BROKER_TIMEOUT_SECONDS", 0.1)
    release = threading.Event()
    started = threading.Event()

    def hang():
        started.set()
        release.wait(timeout=5)

    holder = client.app.state.holder
    monkeypatch.setattr(holder.get().broker, "get_account", hang)

    start = time.monotonic()
    r = client.get("/")
    elapsed = time.monotonic() - start

    assert r.status_code == 200
    assert "unavailable" in r.text.lower()
    # Bounded by the (patched, short) timeout, not by how long the broker
    # call actually takes to return -- proves the request thread gave up
    # rather than blocking on the hang.
    assert elapsed < 2
    assert started.is_set()  # the call did actually reach the broker
    release.set()  # let the background call finish so it doesn't linger


def test_dashboard_heading_is_strategies_not_active(client):
    body = client.get("/").text
    assert "<h2>Strategies</h2>" in body
    assert "Active strategies" not in body


def test_position_at_zero_pl_has_no_color_class(client, monkeypatch):
    from decimal import Decimal

    from allpath_trade.broker.base import Position

    holder = client.app.state.holder
    broker = holder.get().broker

    def get_positions_with_zero_pl():
        return [Position(ticker="TEST", qty=Decimal(10),
                         avg_entry_price=Decimal(100),
                         market_value=Decimal(1000),
                         unrealized_pl=Decimal(0))]

    monkeypatch.setattr(broker, "get_positions", get_positions_with_zero_pl)
    body = client.get("/").text
    # The row should contain the ticker and P/L value but not 'up' or 'down' class
    assert "TEST" in body
    # Check that the P/L cell doesn't have up or down class for zero
    import re
    # Look for the P/L cell: should have class="num" but not "num up" or "num down"
    # Ensure neither 'up' nor 'down' class appears for zero P/L
    assert 'class="num">$0.00</td>' in body or 'class="num ">$0.00</td>' in body
    # Make sure 'up' or 'down' are NOT in the zero P/L row
    zero_pl_section = re.search(r'<tr>.*?TEST.*?</tr>', body, re.DOTALL)
    assert zero_pl_section is not None
    zero_pl_row = zero_pl_section.group()
    assert ' up' not in zero_pl_row and ' down' not in zero_pl_row


# --- summarize_strategy: pure function, no HTML parsing needed -------------

def _doc(rules=None, target_weight="15%", status=StrategyStatus.ACTIVE,
         authorization=Authorization.CONFIRM) -> StrategyDoc:
    return StrategyDoc(
        id="semis", name="Semis core", status=status, authorization=authorization,
        position=PositionPlan(ticker="AAPL", target_weight=target_weight),
        rules=rules or [])


def _quote(price: str = "210.00") -> Quote:
    return Quote(ticker="AAPL", price=Decimal(price), as_of=datetime.now(UTC))


def test_summarize_strategy_hard_sell_below_price_is_a_stop():
    rule = Rule(id="r1", type=RuleType.HARD, condition="price < 185", action="sell all")
    result = summarize_strategy(_doc(rules=[rule]), None, None)
    assert result["key_levels"] == ["stop < 185"]


def test_summarize_strategy_sell_above_price_is_a_target():
    rule = Rule(id="r1", type=RuleType.HARD, condition="price > 220", action="sell half")
    result = summarize_strategy(_doc(rules=[rule]), None, None)
    assert result["key_levels"] == ["target > 220"]


def test_summarize_strategy_buy_below_price_is_an_add_zone():
    rule = Rule(id="r1", type=RuleType.SOFT, condition="price < 150", action="buy $3000")
    result = summarize_strategy(_doc(rules=[rule]), None, None)
    assert result["key_levels"] == ["add zone < 150"]


def test_summarize_strategy_omits_unparseable_condition():
    rule = Rule(id="r1", type=RuleType.SOFT, condition="rsi < 30", action="buy $1000")
    result = summarize_strategy(_doc(rules=[rule]), None, None)
    assert result["key_levels"] == []


def test_summarize_strategy_omits_combos_outside_the_three_labeled_patterns():
    # Soft sell below price, and buy above price, are not among the three
    # combinations the brief labels -- never guess a label for them.
    soft_sell = Rule(id="r1", type=RuleType.SOFT, condition="price < 185", action="sell all")
    buy_above = Rule(id="r2", type=RuleType.HARD, condition="price > 220", action="buy $500")
    result = summarize_strategy(_doc(rules=[soft_sell, buy_above]), None, None)
    assert result["key_levels"] == []


def test_summarize_strategy_current_weight_from_position_and_equity():
    position = Position(ticker="AAPL", qty=Decimal(10), avg_entry_price=Decimal(180),
                         market_value=Decimal(2000), unrealized_pl=Decimal(0))
    result = summarize_strategy(_doc(), position, None, equity=Decimal(10000))
    assert result["current_weight_pct"] == 20.0


def test_summarize_strategy_current_weight_none_without_position():
    result = summarize_strategy(_doc(), None, None, equity=Decimal(10000))
    assert result["current_weight_pct"] is None


def test_summarize_strategy_current_weight_none_without_equity():
    position = Position(ticker="AAPL", qty=Decimal(10), avg_entry_price=Decimal(180),
                         market_value=Decimal(2000), unrealized_pl=Decimal(0))
    result = summarize_strategy(_doc(), position, None, equity=None)
    assert result["current_weight_pct"] is None


def test_summarize_strategy_target_weight_pct_from_fraction():
    result = summarize_strategy(_doc(target_weight="15%"), None, None)
    assert result["target_weight_pct"] == 15.0


def test_summarize_strategy_target_weight_pct_none_when_target_is_a_value():
    doc = StrategyDoc(id="s", name="S", position=PositionPlan(ticker="AAPL",
                       target_value="9000"), rules=[])
    result = summarize_strategy(doc, None, None)
    assert result["target_weight_pct"] is None


def test_summarize_strategy_price_from_quote():
    result = summarize_strategy(_doc(), None, _quote("212.50"))
    assert result["price"] == Decimal("212.50")


def test_summarize_strategy_price_none_without_quote():
    result = summarize_strategy(_doc(), None, None)
    assert result["price"] is None


def test_summarize_strategy_price_class_is_neutral():
    # Quote has no previous-close field (see allpath_trade/data/base.py) --
    # there is nothing to compare the current price against, so direction
    # must degrade to neutral rather than being invented.
    result = summarize_strategy(_doc(), None, _quote("212.50"))
    assert result["price_class"] == ""


def test_summarize_strategy_alerts_rule_triggered():
    rule = Rule(id="r1", type=RuleType.HARD, condition="price < 185",
                action="sell all", state=RuleState.TRIGGERED)
    result = summarize_strategy(_doc(rules=[rule]), None, None)
    assert "rule triggered" in result["alerts"]


def test_summarize_strategy_alerts_pending_review():
    result = summarize_strategy(_doc(), None, None, has_pending=True)
    assert "pending review" in result["alerts"]


def test_summarize_strategy_no_alerts_when_clean():
    result = summarize_strategy(_doc(), None, None)
    assert result["alerts"] == []


def test_summarize_strategy_status_and_auth_and_id():
    result = summarize_strategy(_doc(status=StrategyStatus.PAUSED,
                                 authorization=Authorization.AUTO), None, None)
    assert result["status"] == "paused"
    assert result["auth"] == "auto"
    assert result["id"] == "semis"
    assert result["ticker"] == "AAPL"
    assert result["name"] == "Semis core"


# --- sentinel_heartbeat_status: pure function, no HTML/store needed --------

_FROZEN_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def test_sentinel_heartbeat_status_never_ran_when_row_absent():
    result = sentinel_heartbeat_status(None, 60, now=_FROZEN_NOW)
    assert result == {"text": "Sentinel: never ran (daemon not running?)", "warn": False}


def test_sentinel_heartbeat_status_shows_minutes_ago():
    raw = (_FROZEN_NOW - timedelta(minutes=12)).isoformat()
    result = sentinel_heartbeat_status(raw, 60, now=_FROZEN_NOW)
    assert result["text"] == "Sentinel: last check 12m ago · interval 60m"
    assert result["warn"] is False


def test_sentinel_heartbeat_status_warns_past_twice_the_interval():
    raw = (_FROZEN_NOW - timedelta(minutes=121)).isoformat()  # > 2 * 60
    result = sentinel_heartbeat_status(raw, 60, now=_FROZEN_NOW)
    assert result["warn"] is True


def test_sentinel_heartbeat_status_no_warn_at_exactly_twice_the_interval():
    raw = (_FROZEN_NOW - timedelta(minutes=120)).isoformat()  # exactly 2 * 60
    result = sentinel_heartbeat_status(raw, 60, now=_FROZEN_NOW)
    assert result["warn"] is False


def test_sentinel_heartbeat_status_tolerates_a_naive_timestamp():
    # AppState always writes an aware ISO string, but a stray naive value
    # (e.g. hand-edited, or written by some future caller) must not 500 the
    # dashboard -- treat it as UTC like every other timestamp in this
    # codebase (see fmt.ago).
    raw = (_FROZEN_NOW - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    result = sentinel_heartbeat_status(raw, 60, now=_FROZEN_NOW)
    assert result["text"] == "Sentinel: last check 5m ago · interval 60m"


def test_sentinel_heartbeat_status_tolerates_a_non_utc_offset():
    # A heartbeat written with a different UTC offset representation (e.g.
    # a future non-UTC writer) must still diff correctly against `now`.
    from zoneinfo import ZoneInfo

    et_time = _FROZEN_NOW.astimezone(ZoneInfo("America/New_York")) - timedelta(minutes=8)
    result = sentinel_heartbeat_status(et_time.isoformat(), 60, now=_FROZEN_NOW)
    assert result["text"] == "Sentinel: last check 8m ago · interval 60m"


def test_sentinel_heartbeat_status_malformed_value_degrades_to_never_ran():
    result = sentinel_heartbeat_status("not-a-timestamp", 60, now=_FROZEN_NOW)
    assert result == {"text": "Sentinel: never ran (daemon not running?)", "warn": False}


def test_sentinel_heartbeat_status_market_closed_says_paused_not_checked():
    # Finding 2 (final review, phase5.5-ui-polish): the scheduler ticks --
    # and writes the heartbeat -- every interval regardless of market hours,
    # so "last check Nm ago" outside market hours (a weekend, after close)
    # falsely claims an evaluation that never happened.
    raw = (_FROZEN_NOW - timedelta(minutes=5)).isoformat()
    result = sentinel_heartbeat_status(raw, 60, market_open_raw="false", now=_FROZEN_NOW)
    assert result["text"] == "Sentinel: monitoring paused (market closed) · last tick 5m ago"


def test_sentinel_heartbeat_status_market_open_keeps_last_check_copy():
    raw = (_FROZEN_NOW - timedelta(minutes=5)).isoformat()
    result = sentinel_heartbeat_status(raw, 60, market_open_raw="true", now=_FROZEN_NOW)
    assert result["text"] == "Sentinel: last check 5m ago · interval 60m"


def test_sentinel_heartbeat_status_missing_market_flag_keeps_last_check_copy():
    # A heartbeat written before SENTINEL_MARKET_OPEN_KEY existed (or by a
    # caller that never set it) must not be misread as "market closed" --
    # default to today's copy rather than guessing.
    raw = (_FROZEN_NOW - timedelta(minutes=5)).isoformat()
    result = sentinel_heartbeat_status(raw, 60, now=_FROZEN_NOW)
    assert result["text"] == "Sentinel: last check 5m ago · interval 60m"


def test_sentinel_heartbeat_status_staleness_warning_still_keys_off_tick_age_when_paused():
    # Finding 2's "update the staleness warning to use tick age regardless"
    # -- a paused (market-closed) heartbeat that is nonetheless stale (the
    # daemon itself may have died) must still warn.
    raw = (_FROZEN_NOW - timedelta(minutes=200)).isoformat()
    result = sentinel_heartbeat_status(raw, 60, market_open_raw="false", now=_FROZEN_NOW)
    assert result["warn"] is True


# --- rendered dashboard page -----------------------------------------------

def test_dashboard_strategy_card_shows_compact_summary(client):
    body = client.get("/").text
    assert "AAPL" in body
    assert "stop &lt; 100" in body  # Jinja auto-escapes "<" in rendered HTML
    assert '/strategies/' in body
    assert "$210.00" in body


def test_dashboard_strategy_card_links_to_detail_page(client):
    body = client.get("/").text
    assert 'href="/strategies/' in body


def test_dashboard_no_longer_lists_rule_ids_directly(client):
    # The detailed rule listing (id + state per rule) moved to the
    # Strategies page -- the dashboard only shows the compact card.
    body = client.get("/").text
    assert "r1: armed" not in body


def test_dashboard_quote_failure_renders_dash_not_error(client):
    data = client.app.state.holder.get().data
    data.fail = True
    # The fixture's own login-redirect already rendered "/" once and cached
    # a successful quote -- clear it so this request actually re-hits (and
    # fails against) the now-failing data source.
    dashboard_route._quote_cache.clear()
    r = client.get("/")
    assert r.status_code == 200
    assert "—" in r.text


def test_dashboard_caches_quotes_across_requests(client):
    data = client.app.state.holder.get().data
    client.get("/")
    client.get("/")
    assert data.calls.count("AAPL") == 1


def test_dashboard_refetches_quote_after_cache_expires(client, monkeypatch):
    data = client.app.state.holder.get().data
    client.get("/")
    # Simulate 60+ seconds passing without a real sleep.
    ts, quote = dashboard_route._quote_cache["AAPL"]
    dashboard_route._quote_cache["AAPL"] = (ts - 61, quote)
    client.get("/")
    assert data.calls.count("AAPL") == 2


def test_dashboard_pending_review_shows_alert(client):
    holder = client.app.state.holder
    c = holder.get()
    c.queue.add(strategy_id="semis", rule_id="r1", ticker="AAPL", rule_type="hard",
                condition="price < 100", action="sell all", snapshot={}, intent=None)
    body = client.get("/").text
    assert "pending review" in body


def test_dashboard_is_english_only_with_strategy_cards(client):
    assert_english_only(client.get("/").text)


# --- rendered sentinel heartbeat line ---------------------------------------

def test_dashboard_sentinel_heartbeat_never_ran_on_fresh_install(client):
    # No sentinel pass has ever recorded a heartbeat -- a fresh install, or
    # a user who has only ever run one-shot CLI commands (never `serve` or
    # `run`), must not be told a stale time; the copy must make clear the
    # daemon may simply never have started.
    body = client.get("/").text
    assert "Sentinel: never ran (daemon not running?)" in body


def test_dashboard_sentinel_heartbeat_shows_minutes_ago(client, monkeypatch):
    frozen_now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(dashboard_route, "_utcnow", lambda: frozen_now)
    c = client.app.state.holder.get()
    c.app_state.set(SENTINEL_HEARTBEAT_KEY,
                    (frozen_now - timedelta(minutes=12)).isoformat())

    body = client.get("/").text

    assert "Sentinel: last check 12m ago" in body
    # Settings() defaults sentinel_interval_minutes to 60 -- see config.py.
    assert "interval 60m" in body


def test_dashboard_sentinel_heartbeat_stale_carries_the_warn_class(client, monkeypatch):
    frozen_now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(dashboard_route, "_utcnow", lambda: frozen_now)
    c = client.app.state.holder.get()
    # Default interval is 60min, so 2x is 120min -- 200min is well past it.
    c.app_state.set(SENTINEL_HEARTBEAT_KEY,
                    (frozen_now - timedelta(minutes=200)).isoformat())

    body = client.get("/").text

    assert "Sentinel: last check 200m ago" in body
    assert '<p class="warn">Sentinel: last check 200m ago' in body


def test_dashboard_sentinel_heartbeat_recent_does_not_carry_the_warn_class(client, monkeypatch):
    frozen_now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(dashboard_route, "_utcnow", lambda: frozen_now)
    c = client.app.state.holder.get()
    c.app_state.set(SENTINEL_HEARTBEAT_KEY,
                    (frozen_now - timedelta(minutes=12)).isoformat())

    body = client.get("/").text

    assert '<p class="muted">Sentinel: last check 12m ago' in body


def test_dashboard_sentinel_heartbeat_shows_paused_copy_when_market_was_closed(
        client, monkeypatch):
    # Finding 2: on a Sunday (or any tick recorded while the market was
    # closed) the dashboard must say monitoring was paused, not that a check
    # ran.
    frozen_now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)  # 2026-08-09 is a Sunday
    monkeypatch.setattr(dashboard_route, "_utcnow", lambda: frozen_now)
    c = client.app.state.holder.get()
    c.app_state.set(SENTINEL_HEARTBEAT_KEY,
                    (frozen_now - timedelta(minutes=5)).isoformat())
    c.app_state.set(SENTINEL_MARKET_OPEN_KEY, "false")

    body = client.get("/").text

    assert "Sentinel: monitoring paused (market closed) · last tick 5m ago" in body
    assert "Sentinel: last check" not in body


# --- Finding 1 (final review, phase5.5-ui-polish): heartbeat hoisted above
# the strategy/quote loop, and one shared budget bounds total quote time ----

def test_dashboard_heartbeat_line_renders_even_when_every_quote_hangs(client, monkeypatch):
    # 1a: sentinel_status must be computed before the strategy/quote loop
    # runs, so the "is my system wedged?" line is never itself held hostage
    # by a hanging data source.
    monkeypatch.setattr(dashboard_route, "QUOTES_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(dashboard_route, "BROKER_TIMEOUT_SECONDS", 0.2)
    data = client.app.state.holder.get().data
    release = threading.Event()

    def hang(ticker):
        release.wait(timeout=5)
        raise RuntimeError("never resolves in time")

    monkeypatch.setattr(data, "get_quote", hang)
    dashboard_route._quote_cache.clear()

    body = client.get("/").text

    assert "Sentinel: never ran (daemon not running?)" in body
    release.set()  # let the background call finish so it doesn't linger


def test_dashboard_quote_budget_bounds_total_time_across_many_strategies(
        client, monkeypatch, tmp_path):
    # 1b: without a shared budget, N strategies against a hanging data
    # source cost N * BROKER_TIMEOUT_SECONDS; with the budget, only the
    # calls that start before the deadline ever block at all -- total time
    # stays close to one BROKER_TIMEOUT_SECONDS, not N of them.
    for i in range(4):
        (tmp_path / "strategies" / f"extra{i}.yaml").write_text(f"""
name: "Extra {i}"
status: active
position: {{ticker: TICK{i}, target_weight: 5%}}
rules: []
""")
    monkeypatch.setattr(dashboard_route, "BROKER_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(dashboard_route, "QUOTES_BUDGET_SECONDS", 0.05)
    data = client.app.state.holder.get().data
    release = threading.Event()

    def hang(ticker):
        release.wait(timeout=5)
        raise RuntimeError("never resolves in time")

    monkeypatch.setattr(data, "get_quote", hang)
    dashboard_route._quote_cache.clear()

    start = time.monotonic()
    r = client.get("/")
    elapsed = time.monotonic() - start

    assert r.status_code == 200
    # 5 strategies total * 0.3s BROKER_TIMEOUT_SECONDS would be 1.5s without
    # the shared budget; the budget bounds this to roughly one timeout.
    assert elapsed < 1.0
    assert "—" in r.text  # at least the skipped tickers render as a dash
    release.set()
