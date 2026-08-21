import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from allpath_trade.broker.base import Account, Position
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

    def __init__(self, price: str = "210.00", previous_close: str | None = None):
        self.price = Decimal(price)
        self.previous_close = Decimal(previous_close) if previous_close is not None else None
        self.calls: list[str] = []
        self.fail = False

    def get_quote(self, ticker: str) -> Quote:
        self.calls.append(ticker)
        if self.fail:
            raise RuntimeError("no price available")
        return Quote(ticker=ticker, price=self.price, previous_close=self.previous_close,
                     as_of=datetime.now(UTC))

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


@pytest.fixture(autouse=True)
def _clear_equity_history_cache():
    # Same rationale as `_clear_quote_cache` above -- the equity-history
    # cache is module-level so it survives across requests within a
    # process, which is the point, but that would make assertions depend on
    # test order unless cleared between tests.
    dashboard_route._equity_history_cache.clear()
    yield
    dashboard_route._equity_history_cache.clear()


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


def test_dashboard_trades_table_labels_submitted_column_and_shows_fill_pending(client):
    # Fill-honesty round: the "When" column used to show submission time
    # under an ambiguous header. It's the submission time, always -- label
    # it as such, and never claim a fill happened when the row is still
    # unfilled.
    from decimal import Decimal

    from allpath_trade.broker.base import Order, OrderIntent, OrderSide, OrderStatus
    from allpath_trade.risk.gate import RiskDecision

    components = client.app.state.holder.get()
    intent = OrderIntent(ticker="TSLA", side=OrderSide.BUY, qty=Decimal(1), reason="dip buy")
    order = Order(id="o1", ticker="TSLA", side=OrderSide.BUY, qty=Decimal(1),
                 notional=None, status=OrderStatus.SUBMITTED, filled_qty=Decimal(0),
                 filled_avg_price=None, submitted_at=datetime.now(UTC))
    components.journal.record(intent, RiskDecision(approved=True), order)

    body = client.get("/").text
    assert "Submitted" in body
    assert "fill pending" in body


def test_dashboard_trades_table_shows_fill_time_and_price_when_filled(client):
    from decimal import Decimal

    from allpath_trade.broker.base import Order, OrderIntent, OrderSide, OrderStatus
    from allpath_trade.risk.gate import RiskDecision

    components = client.app.state.holder.get()
    intent = OrderIntent(ticker="TSLA", side=OrderSide.BUY, qty=Decimal(1), reason="dip buy")
    order = Order(id="o1", ticker="TSLA", side=OrderSide.BUY, qty=Decimal(1),
                 notional=None, status=OrderStatus.FILLED, filled_qty=Decimal(1),
                 filled_avg_price=Decimal("332.01"),
                 submitted_at=datetime.now(UTC), filled_at=datetime.now(UTC))
    components.journal.record(intent, RiskDecision(approved=True), order)

    body = client.get("/").text
    assert "filled" in body
    assert "332.01" in body


def test_dashboard_trades_table_shows_partial_fill_and_keeps_polling(client):
    # I3: a partial fill must not render the bare word "filled" (which
    # would read as done, not still-open), and status='partially_filled'
    # rows must stay in the sweep's polling set rather than dropping out
    # once a partial fill lands.
    from decimal import Decimal

    from allpath_trade.broker.base import Order, OrderIntent, OrderSide, OrderStatus
    from allpath_trade.risk.gate import RiskDecision

    components = client.app.state.holder.get()
    intent = OrderIntent(ticker="TSLA", side=OrderSide.BUY, qty=Decimal(10), reason="dip buy")
    order = Order(id="o1", ticker="TSLA", side=OrderSide.BUY, qty=Decimal(10),
                 notional=None, status=OrderStatus.PARTIALLY_FILLED,
                 filled_qty=Decimal(3), filled_avg_price=Decimal("332.01"),
                 submitted_at=datetime.now(UTC))
    components.journal.record(intent, RiskDecision(approved=True), order)

    body = client.get("/").text
    assert "partially filled 3 @ 332.01 so far" in body
    assert "&middot; filled" not in body  # not the terminal "filled" label

    [row] = components.journal.unfilled_recent()
    assert row["status"] == "partially_filled"


def test_dashboard_trades_table_shows_status_unconfirmed_for_stale_submitted_rows(client):
    # I2: a row stuck 'submitted' well past the fill-pending window must
    # stop affirmatively claiming "fill pending" -- that claim is no longer
    # honest once it's been unconfirmed this long.
    components = client.app.state.holder.get()
    old_ts = (datetime.now(UTC) - timedelta(hours=72)).isoformat()
    components.journal._conn.execute(
        "INSERT INTO trades (ts, ticker, side, qty, notional, status, reason,"
        " strategy_id, risk_reasons, broker_order_id, filled_qty, filled_avg_price)"
        " VALUES (?, 'TSLA', 'buy', '1', NULL, 'submitted', 'old', NULL, '[]',"
        " 'o-old', '0', NULL)",
        (old_ts,))
    components.journal._conn.commit()

    body = client.get("/").text
    assert "status unconfirmed" in body
    assert "fill pending" not in body


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


def test_dashboard_shows_open_pill_when_market_is_open(client, monkeypatch):
    # Patched, never the real clock -- dashboard_route.is_market_hours is
    # the same allpath_trade.scheduler.is_market_hours the sentinel gates
    # its pass on, imported directly into dashboard.py.
    monkeypatch.setattr(dashboard_route, "is_market_hours", lambda: True)
    body = client.get("/").text
    assert 'class="chip market-pill open"' in body
    assert "Market open" in body
    assert "Market closed" not in body


def test_dashboard_shows_closed_pill_when_market_is_closed(client, monkeypatch):
    monkeypatch.setattr(dashboard_route, "is_market_hours", lambda: False)
    body = client.get("/").text
    assert 'class="chip market-pill closed"' in body
    assert "Market closed" in body
    assert "Market open" not in body


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
    # _quote() leaves previous_close at its default of None -- there is
    # nothing to compare the current price against, so direction must
    # degrade to neutral rather than being invented, and no percentage is
    # rendered.
    result = summarize_strategy(_doc(), None, _quote("212.50"))
    assert result["price_class"] == ""
    assert result["day_change_pct"] is None


def test_summarize_strategy_price_class_up_with_positive_day_change():
    quote = Quote(ticker="AAPL", price=Decimal("212.50"), previous_close=Decimal("200.00"),
                  as_of=datetime.now(UTC))
    result = summarize_strategy(_doc(), None, quote)
    assert result["price_class"] == "up"
    assert result["day_change_pct"] == pytest.approx(6.25)


def test_summarize_strategy_price_class_down_with_negative_day_change():
    quote = Quote(ticker="AAPL", price=Decimal("190.00"), previous_close=Decimal("200.00"),
                  as_of=datetime.now(UTC))
    result = summarize_strategy(_doc(), None, quote)
    assert result["price_class"] == "down"
    assert result["day_change_pct"] == pytest.approx(-5.0)


def test_summarize_strategy_price_class_neutral_when_flat():
    quote = Quote(ticker="AAPL", price=Decimal("200.00"), previous_close=Decimal("200.00"),
                  as_of=datetime.now(UTC))
    result = summarize_strategy(_doc(), None, quote)
    assert result["price_class"] == ""
    assert result["day_change_pct"] == 0.0


def test_summarize_strategy_day_change_none_without_quote():
    result = summarize_strategy(_doc(), None, None)
    assert result["day_change_pct"] is None
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


def test_dashboard_strategy_card_shows_green_up_change(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "data",
                        FakeDataSource(price="210.00", previous_close="200.00"))
    dashboard_route._quote_cache.clear()
    body = client.get("/").text
    assert 'class="up">$210.00 +5.00%</span>' in body


def test_dashboard_strategy_card_shows_red_down_change(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "data",
                        FakeDataSource(price="190.00", previous_close="200.00"))
    dashboard_route._quote_cache.clear()
    body = client.get("/").text
    assert 'class="down">$190.00 -5.00%</span>' in body


def test_dashboard_strategy_card_no_change_without_previous_close(client):
    # The fixture's FakeDataSource defaults previous_close to None -- no
    # percentage must be rendered, and the class must stay neutral.
    body = client.get("/").text
    assert 'class="">$210.00</span>' in body


def test_dashboard_strategy_card_suppresses_percentage_when_it_rounds_to_zero(client, monkeypatch):
    # (200.008 - 200.00) / 200.00 * 100 = 0.004%, which rounds to "0.00" at
    # two decimals -- a coloured "+0.0%" (the old one-decimal behaviour
    # this replaces) reads as a real move that isn't there, so the
    # percentage must not render at all once it rounds to zero.
    monkeypatch.setattr(client.app.state.holder.get(), "data",
                        FakeDataSource(price="200.008", previous_close="200.00"))
    dashboard_route._quote_cache.clear()
    body = client.get("/").text
    # No trailing percentage at all -- not "+0.00%", not the old "+0.0%".
    assert 'class="up">$200.01</span>' in body


def test_dashboard_strategy_card_no_signed_zero_when_exactly_flat(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "data",
                        FakeDataSource(price="200.00", previous_close="200.00"))
    dashboard_route._quote_cache.clear()
    body = client.get("/").text
    assert 'class="">$200.00</span>' in body


class ShortPositionBroker(FakeBroker):
    """A negative market_value (short position) -- current_weight_pct then
    comes out negative too, which the weight-bar's ratio-to-target must not
    turn into a negative CSS width."""

    def get_positions(self):
        return [Position(ticker="AAPL", qty=Decimal(-10),
                         avg_entry_price=Decimal(180),
                         market_value=Decimal(-500),
                         unrealized_pl=Decimal(0))]


def test_dashboard_weight_bar_clamps_negative_ratio_to_zero(client, monkeypatch):
    # equity=10000, market_value=-500 -> current_weight_pct=-5%; target is
    # 15% (see STRAT) -> ratio is negative. An unclamped width:-33.3% is
    # invalid CSS (silently dropped by the browser) rather than the empty
    # bar a non-positive weight should render as.
    monkeypatch.setattr(client.app.state.holder.get(), "broker", ShortPositionBroker())
    body = client.get("/").text
    assert 'style="width:0.0%"' in body
    assert "width:-" not in body


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


def test_dashboard_strategy_card_shows_not_monitored_badge_for_draft(client):
    (client.app.state.holder.get().strategies.directory / "draftstrat.yaml").write_text("""
name: "Draft one"
status: draft
position: {ticker: MSFT, target_weight: 5%}
rules: []
""")
    dashboard_route._quote_cache.clear()
    body = client.get("/").text
    assert "not monitored" in body


def test_dashboard_strategy_card_omits_not_monitored_badge_for_active(client):
    body = client.get("/").text
    assert "not monitored" not in body


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
    c.app_state.set(f"{SENTINEL_HEARTBEAT_KEY}:paper",
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
    c.app_state.set(f"{SENTINEL_HEARTBEAT_KEY}:paper",
                    (frozen_now - timedelta(minutes=200)).isoformat())

    body = client.get("/").text

    assert "Sentinel: last check 200m ago" in body
    assert '<p class="warn">Sentinel: last check 200m ago' in body


def test_dashboard_sentinel_heartbeat_recent_does_not_carry_the_warn_class(client, monkeypatch):
    frozen_now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(dashboard_route, "_utcnow", lambda: frozen_now)
    c = client.app.state.holder.get()
    c.app_state.set(f"{SENTINEL_HEARTBEAT_KEY}:paper",
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
    c.app_state.set(f"{SENTINEL_HEARTBEAT_KEY}:paper",
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


# --- Equity history chart (Item 1) ------------------------------------------

class EquityHistoryBroker(FakeBroker):
    """Canned get_equity_history so route tests don't depend on a real
    broker call -- 5 ascending points, comfortably enough to render a line
    regardless of which range's day-count the route computes."""

    def get_equity_history(self, days):
        base = datetime(2026, 8, 1, tzinfo=UTC)
        return [(base + timedelta(days=i), Decimal(str(10000 + i * 100)))
                for i in range(5)]


class FailingEquityHistoryBroker(FakeBroker):
    def get_equity_history(self, days):
        raise RuntimeError("history unavailable")


def test_dashboard_equity_chart_renders_line_for_each_range(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "broker", EquityHistoryBroker())
    # The client fixture's own login POST already rendered "/" once (with
    # the default range and the fixture's original FakeBroker) before this
    # test's monkeypatch ran, caching an empty history under the default
    # range's key -- clear it so every range below actually hits the
    # broker just swapped in above.
    dashboard_route._equity_history_cache.clear()
    for key in ("1w", "1m", "ytd", "1y"):
        body = client.get(f"/?range={key}").text
        assert "<polyline" in body
        assert "No history yet" not in body


def test_dashboard_equity_chart_range_tabs_mark_active(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "broker", EquityHistoryBroker())
    dashboard_route._equity_history_cache.clear()
    body = client.get("/?range=ytd").text
    assert '<a href="/?range=ytd" class="tab-link on">YTD</a>' in body


def test_dashboard_equity_chart_default_range_is_month(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "broker", EquityHistoryBroker())
    dashboard_route._equity_history_cache.clear()
    body = client.get("/").text
    assert '<a href="/?range=1m" class="tab-link on">Month</a>' in body


def test_dashboard_equity_chart_invalid_range_falls_back_to_default(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "broker", EquityHistoryBroker())
    dashboard_route._equity_history_cache.clear()
    body = client.get("/?range=garbage").text
    assert '<a href="/?range=1m" class="tab-link on">Month</a>' in body


def test_dashboard_equity_chart_broker_failure_shows_placeholder(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "broker", FailingEquityHistoryBroker())
    r = client.get("/")
    assert r.status_code == 200
    assert "No history yet" in r.text


def test_dashboard_equity_chart_empty_history_shows_placeholder(client):
    # Default FakeBroker.get_equity_history inherits the base class's `[]`.
    body = client.get("/").text
    assert "No history yet" in body


def test_dashboard_equity_chart_shows_current_equity_and_change(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "broker", EquityHistoryBroker())
    body = client.get("/?range=1w").text
    # FakeBroker.get_account().equity is a fixed 10000; history's first
    # point in a 5-point ascending series is also 10000 -- change is $0.
    assert "$10,000.00" in body


def test_dashboard_equity_chart_is_english_only(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "broker", EquityHistoryBroker())
    assert_english_only(client.get("/?range=ytd").text)


# --- Important 3: headline direction and chart-line colour must agree ------

class DivergentEquityBroker(EquityHistoryBroker):
    """Live equity below the history's first point, while the history
    itself still trends upward -- reproduces Important 3: the headline
    (live equity vs first history point) and the raw line colour (history's
    last vs first point) used to be two independent comparisons that could
    disagree. Chart colour must follow the headline's sign."""

    def get_account(self):
        return Account(equity=Decimal(9000), cash=Decimal(5000), buying_power=Decimal(9000))


def test_dashboard_equity_chart_colour_follows_the_headline_not_the_raw_line(
        client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "broker", DivergentEquityBroker())
    dashboard_route._equity_history_cache.clear()
    body = client.get("/?range=1w").text
    # Headline: live equity (9000) - first history point (10000) = -1000 -> down.
    assert 'class="down"> -$1,000.00' in body
    # The line itself trends up (last 10400 >= first 10000) -- naive
    # last-vs-first would paint it green, contradicting the red headline
    # right above it. It must be red instead.
    assert 'class="equity-svg down"' in body
    assert 'class="equity-svg up"' not in body


# --- Minor 1: a failed/empty equity-history lookup gets a short cache TTL --

def test_equity_history_cache_retries_a_failure_well_before_the_success_ttl(
        client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "broker", FailingEquityHistoryBroker())
    dashboard_route._equity_history_cache.clear()
    client.get("/")
    cache_key = ("fake", dashboard_route._DEFAULT_RANGE)
    ts, history = dashboard_route._equity_history_cache[cache_key]
    assert history == []

    # 31s back is past the 30s failure TTL but nowhere near the 5-minute
    # success TTL -- a genuine failure must not get to hide behind the long
    # TTL meant for a working chart.
    dashboard_route._equity_history_cache[cache_key] = (ts - 31, history)
    monkeypatch.setattr(client.app.state.holder.get(), "broker", EquityHistoryBroker())
    body = client.get("/").text
    assert "<polyline" in body


# --- shadow-dual-active T4 review Important 4: keyed by (broker.name, range),
# not range alone -- two brokers sharing one process (paper + shadow) must
# never serve each other's cached equity curve. ------------------------------

class _NamedEquityHistoryBroker(EquityHistoryBroker):
    """Same canned history shape as EquityHistoryBroker, but with a
    distinct `.name` and a distinct series -- so a cache-key collision
    would be caught by the SERIES ending up wrong, not just by dict size."""

    def __init__(self, name: str, offset: int) -> None:
        super().__init__()
        self.name = name
        self._offset = offset

    def get_equity_history(self, days):
        base = datetime(2026, 8, 1, tzinfo=UTC)
        return [(base + timedelta(days=i), Decimal(str(self._offset + i * 100)))
                for i in range(5)]


def test_equity_history_cache_keyed_by_broker_name_not_range_alone(client, monkeypatch):
    dashboard_route._equity_history_cache.clear()
    holder = client.app.state.holder

    monkeypatch.setattr(holder.get(), "broker", _NamedEquityHistoryBroker("paper", 10000))
    client.get("/")
    monkeypatch.setattr(holder.get(), "broker", _NamedEquityHistoryBroker("shadow", 50000))
    client.get("/")

    assert len(dashboard_route._equity_history_cache) == 2
    paper_key = ("paper", dashboard_route._DEFAULT_RANGE)
    shadow_key = ("shadow", dashboard_route._DEFAULT_RANGE)
    assert paper_key in dashboard_route._equity_history_cache
    assert shadow_key in dashboard_route._equity_history_cache
    _, paper_history = dashboard_route._equity_history_cache[paper_key]
    _, shadow_history = dashboard_route._equity_history_cache[shadow_key]
    # Distinct series prove shadow's render didn't reuse paper's cached
    # entry (or vice versa) -- a same-key collision would leave one
    # account's dashboard silently showing the other's equity curve.
    assert paper_history[0][1] == Decimal(10000)
    assert shadow_history[0][1] == Decimal(50000)


# --- Minor 2: a one-point history hides the period-change headline ---------

class OnePointEquityHistoryBroker(FakeBroker):
    def get_equity_history(self, days):
        return [(datetime(2026, 8, 1, tzinfo=UTC), Decimal(10000))]


def test_dashboard_equity_chart_one_point_history_hides_the_change_headline(
        client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "broker", OnePointEquityHistoryBroker())
    dashboard_route._equity_history_cache.clear()
    body = client.get("/").text
    # Not enough points for equity_svg to draw a line -- placeholder chart.
    assert "No history yet" in body
    # A single point has nothing to compare against; the "current equity +
    # period change" headline above the chart must not show a number over
    # what is, visually, an empty chart.
    assert 'font-size:23px; font-weight:500">$10,000.00</span>' not in body


# --- Minor 4: "since <date>" caption under the chart ------------------------

def test_dashboard_equity_chart_shows_since_caption_when_history_exists(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "broker", EquityHistoryBroker())
    dashboard_route._equity_history_cache.clear()
    body = client.get("/").text
    first_et = datetime(2026, 8, 1, tzinfo=UTC).astimezone(ZoneInfo("America/New_York"))
    assert f"since {first_et.strftime('%b %d, %Y')}" in body


def test_dashboard_equity_chart_no_since_caption_when_history_is_empty(client):
    body = client.get("/").text
    assert "since " not in body


# --- Minor 11: YTD anchor uses the ET calendar date, not UTC's -------------

def test_range_days_ytd_anchors_to_et_calendar_date_not_utc():
    # 2026-01-01 00:30 UTC is still 2025-12-31 evening in ET (UTC-5 in
    # January) -- anchoring YTD's "start of year" to the UTC calendar date
    # treats this instant as day 1 of 2026 (a 1-day YTD window); the ET
    # calendar -- the same one the reports page's Today/This week/This
    # month chips already anchor to -- still considers it the last day of
    # 2025, giving a YTD window of almost the whole year instead.
    now = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    assert dashboard_route._range_days("ytd", now) == 364


# --- Minor 12: the `?range=` query param no longer shadows the `range`
# builtin inside the route function (renamed to `range_key`) -- the URL
# contract itself (`?range=...`) is unchanged, verified by every other
# equity-chart test above still passing with plain `?range=...` URLs. -------

def test_dashboard_range_query_param_name_is_unchanged(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "broker", EquityHistoryBroker())
    dashboard_route._equity_history_cache.clear()
    body = client.get("/?range=1y").text
    assert '<a href="/?range=1y" class="tab-link on">Year</a>' in body
