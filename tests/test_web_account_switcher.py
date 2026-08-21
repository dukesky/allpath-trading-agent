"""Tests for shadow-dual-active Task 5: the web account switcher
(`web/account_ctx.py`, `base.html`) and the "every page shows only the
current account's data" isolation it exists to guarantee.

The interleaved fixture below seeds BOTH accounts with their own strategy,
trade, memory dossier, report, and pending review -- each carrying a unique,
grep-able marker token -- then asserts every account-aware page shows ONLY
the current cookie's account's tokens and never the other's, under both
cookie values. This is the adversarial "cross-account leakage" check the
plan's Task 5 test list calls for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from allpath_trade.broker.base import Order, OrderIntent, OrderSide, OrderStatus
from allpath_trade.config import Settings
from allpath_trade.risk.gate import RiskDecision
from allpath_trade.web.account_ctx import ACCOUNT_COOKIE
from allpath_trade.web.app import create_app
from allpath_trade.web.routes import dashboard as dashboard_route
from tests.helpers import assert_english_only
from tests.test_sentinel import FakeBroker
from tests.test_web_dashboard import FakeDataSource

PAPER_STRAT = """
name: "Paper Semis PAPRSTRATMARK"
status: active
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""
SHADOW_STRAT = """
name: "Shadow Financials SHDWSTRATMARK"
status: active
position: {ticker: JPM, target_weight: 10%}
rules:
  - {id: r1, type: hard, condition: "price < 50", action: "sell all"}
"""


@pytest.fixture(autouse=True)
def _clear_caches():
    dashboard_route._quote_cache.clear()
    dashboard_route._equity_history_cache.clear()
    yield
    dashboard_route._quote_cache.clear()
    dashboard_route._equity_history_cache.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    strategies_dir = tmp_path / "strategies"
    (strategies_dir / "paper").mkdir(parents=True)
    (strategies_dir / "shadow").mkdir(parents=True)
    (strategies_dir / "paper" / "paper-semis.yaml").write_text(PAPER_STRAT)
    (strategies_dir / "shadow" / "shadow-financials.yaml").write_text(SHADOW_STRAT)
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=strategies_dir,
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        monkeypatch.setattr(c.app.state.holder.get(), "data", FakeDataSource())
        c.post("/login", data={"token": "secret"})
        yield c


def _seed(client) -> None:
    """Writes one distinctly-markered trade, memory dossier entry, report,
    and pending review into EACH account's own bundle."""
    comp = client.app.state.holder.get()
    for account, marker in (("paper", "PAPR"), ("shadow", "SHDW")):
        b = comp.accounts[account]
        intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=Decimal(1),
                             reason=f"{marker}TRADEMARK")
        order = Order(id=f"{marker}-o1", ticker="AAPL", side=OrderSide.BUY,
                     qty=Decimal(1), notional=None, status=OrderStatus.FILLED,
                     filled_qty=Decimal(1), filled_avg_price=Decimal(200),
                     submitted_at=datetime.now(UTC), filled_at=datetime.now(UTC))
        b.journal.record(intent, RiskDecision(approved=True), order)

        b.memory.apply("stock", "AAPL", "add", text=f"{marker}STOCKMARK dossier note")

        b.reports.add(date="2026-08-10", body=f"{marker}REPORTBODYMARK",
                      summary=f"{marker}REPORTSUMMARYMARK", conversation_id=None,
                      model="opus", tokens_used=10)

        b.queue.add(strategy_id="s1", rule_id="r1", ticker="AAPL", rule_type="soft",
                   condition=f"{marker}REVIEWMARK < 100", action="sell all",
                   snapshot={"price": "99"}, intent=None, source="sentinel")


def _get(client, path: str, account: str) -> str:
    client.cookies.set(ACCOUNT_COOKIE, account)
    return client.get(path).text


# --- switcher mechanics ------------------------------------------------------

def test_default_account_with_no_cookie_is_paper(client):
    # No account cookie has been set anywhere in this fixture yet -- the
    # very first request is the "fresh browser" case.
    body = client.get("/").text
    assert 'class="acct-paper on"' in body
    assert 'class="acct-shadow on"' not in body


def test_switch_sets_cookie_and_redirects_to_referer(client):
    r = client.post("/account/switch", data={"account": "shadow"},
                    headers={"referer": "http://testserver/strategies"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/strategies"
    assert r.cookies.get(ACCOUNT_COOKIE) == "shadow"


def test_switch_cookie_is_httponly_and_samesite_strict(client):
    r = client.post("/account/switch", data={"account": "shadow"},
                    follow_redirects=False)
    set_cookie = r.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "samesite=strict" in set_cookie.lower()


def test_switch_with_no_referer_redirects_to_dashboard(client):
    r = client.post("/account/switch", data={"account": "paper"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"


def test_switch_with_cross_origin_referer_redirects_to_dashboard_not_the_foreign_url(client):
    r = client.post("/account/switch", data={"account": "paper"},
                    headers={"referer": "https://evil.example.com/steal"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"


def test_switch_with_invalid_account_value_falls_back_to_paper(client):
    r = client.post("/account/switch", data={"account": "admin"},
                    follow_redirects=False)
    assert r.cookies.get(ACCOUNT_COOKIE) == "paper"


def test_garbled_account_cookie_reads_as_paper(client):
    client.cookies.set(ACCOUNT_COOKIE, "not-a-real-account")
    body = client.get("/").text
    assert 'class="acct-paper on"' in body
    assert 'class="acct-shadow on"' not in body


def test_switcher_visible_on_every_page(client):
    for path in ("/", "/chat", "/reviews", "/strategies", "/memory", "/reports", "/settings"):
        body = client.get(path).text
        assert 'class="account-switcher"' in body, path


# --- per-page cross-account isolation ---------------------------------------

def test_dashboard_shows_only_current_account_strategy(client):
    body = _get(client, "/", "paper")
    assert "PAPRSTRATMARK" in body
    assert "SHDWSTRATMARK" not in body

    body = _get(client, "/", "shadow")
    assert "SHDWSTRATMARK" in body
    assert "PAPRSTRATMARK" not in body


def test_dashboard_shows_only_current_account_trades(client):
    _seed(client)
    body = _get(client, "/", "paper")
    assert "PAPRTRADEMARK" in body
    assert "SHDWTRADEMARK" not in body

    body = _get(client, "/", "shadow")
    assert "SHDWTRADEMARK" in body
    assert "PAPRTRADEMARK" not in body


def test_strategies_page_shows_only_current_account_strategy(client):
    body = _get(client, "/strategies", "paper")
    assert "PAPRSTRATMARK" in body
    assert "SHDWSTRATMARK" not in body

    body = _get(client, "/strategies", "shadow")
    assert "SHDWSTRATMARK" in body
    assert "PAPRSTRATMARK" not in body


def test_memory_page_shows_only_current_account_dossier(client):
    _seed(client)
    body = _get(client, "/memory?tab=stock", "paper")
    assert "PAPRSTOCKMARK" in body
    assert "SHDWSTOCKMARK" not in body

    body = _get(client, "/memory?tab=stock", "shadow")
    assert "SHDWSTOCKMARK" in body
    assert "PAPRSTOCKMARK" not in body


def test_reports_page_shows_only_current_account_report(client):
    _seed(client)
    body = _get(client, "/reports", "paper")
    assert "PAPRREPORTSUMMARYMARK" in body
    assert "SHDWREPORTSUMMARYMARK" not in body

    body = _get(client, "/reports", "shadow")
    assert "SHDWREPORTSUMMARYMARK" in body
    assert "PAPRREPORTSUMMARYMARK" not in body

    body = _get(client, "/reports/2026-08-10", "paper")
    assert "PAPRREPORTBODYMARK" in body
    assert "SHDWREPORTBODYMARK" not in body


def test_reviews_page_shows_only_current_account_pending_row(client):
    _seed(client)
    body = _get(client, "/reviews", "paper")
    assert "PAPRREVIEWMARK" in body
    assert "SHDWREVIEWMARK" not in body

    body = _get(client, "/reviews", "shadow")
    assert "SHDWREVIEWMARK" in body
    assert "PAPRREVIEWMARK" not in body


def test_other_account_pending_dot_shows_on_the_switcher(client):
    _seed(client)
    # Both accounts have exactly one pending row each -- viewed from paper,
    # the switcher's shadow button should carry the "other account has
    # pending" dot (and vice versa).
    body = _get(client, "/", "paper")
    assert '<span class="dot"></span></button>' in body.split("acct-shadow")[1][:80]

    body = _get(client, "/", "shadow")
    assert '<span class="dot"></span></button>' in body.split("acct-paper")[1][:80]


def test_pending_badge_counts_only_current_account(client):
    _seed(client)
    body = _get(client, "/", "paper")
    assert '<a href="/reviews" class="">Pending<span class="badge">1</span></a>' in body \
        or '<span class="badge">1</span>' in body


def test_account_chip_next_to_page_heading_reflects_current_account(client):
    body = _get(client, "/strategies", "shadow")
    assert 'class="chip account-chip chip-shadow"' in body
    assert ">Shadow<" in body


# --- English-only on the new/changed surfaces --------------------------------

def test_switcher_and_chips_are_english_only(client):
    _seed(client)
    for path in ("/", "/chat", "/reviews", "/strategies", "/memory", "/reports", "/settings"):
        assert_english_only(_get(client, path, "shadow"))
