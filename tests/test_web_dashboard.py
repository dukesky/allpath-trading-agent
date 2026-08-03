import threading
import time

import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from allpath_trade.web.routes import dashboard as dashboard_route
from tests.helpers import assert_english_only
from tests.test_sentinel import FakeBroker

STRAT = """
name: "Semis core"
status: active
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "semis.yaml").write_text(STRAT)
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
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
