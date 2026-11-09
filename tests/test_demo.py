"""`allpath-trade demo`: seeded sample data, a static broker, offline quotes,
and a working directory that is sealed off from a real install."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from allpath_trade import cli
from allpath_trade.broker.base import BrokerError, OrderIntent, OrderSide, OrderStatus
from allpath_trade.demo import (
    DEMO_CASH,
    DEMO_PRICES,
    DEMO_TOKEN,
    OUTBOUND_ENV,
    STRATEGY_FILES,
    DemoBroker,
    DemoData,
    demo_token,
    is_loopback,
    scrub_outbound_env,
    seed_demo,
)
from allpath_trade.store.db import connect
from allpath_trade.strategy.loader import load_strategy
from allpath_trade.web.setup_status import SETUP_DISMISSED_KEY
from tests.helpers import assert_english_only

# -- environment scrubbing and the token --


def test_scrub_removes_outbound_keys_case_insensitively_and_keeps_llm_keys():
    env = {"ALPACA_API_KEY": "a", "alpaca_secret_key": "s",
           "TELEGRAM_BOT_TOKEN": "t", "SMTP_PASSWORD": "p", "NTFY_URL": "n",
           "AGENT_WEBHOOK_URL": "w", "PUBLISH_TOKEN": "x", "WEB_TOKEN": "old",
           "OPENROUTER_API_KEY": "llm", "ANTHROPIC_API_KEY": "llm2", "PATH": "/bin"}
    removed = scrub_outbound_env(env)
    assert set(removed) == {"ALPACA_API_KEY", "alpaca_secret_key",
                            "TELEGRAM_BOT_TOKEN", "SMTP_PASSWORD", "NTFY_URL",
                            "AGENT_WEBHOOK_URL", "PUBLISH_TOKEN", "WEB_TOKEN"}
    assert env == {"OPENROUTER_API_KEY": "llm", "ANTHROPIC_API_KEY": "llm2",
                   "PATH": "/bin"}


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True),
    ("0.0.0.0", False), ("192.168.1.5", False), ("my-box", False)])
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected


def test_well_known_token_only_on_loopback():
    assert demo_token("127.0.0.1") == DEMO_TOKEN
    remote = demo_token("0.0.0.0")
    assert remote != DEMO_TOKEN and len(remote) >= 16


# -- DemoBroker / DemoData --


def test_demo_broker_account_adds_up():
    b = DemoBroker()
    positions = b.get_positions()
    assert {p.ticker for p in positions} == {"NVDA", "AAPL"}
    acct = b.get_account()
    assert acct.cash == DEMO_CASH
    assert acct.equity == DEMO_CASH + sum(p.market_value for p in positions)
    for p in positions:
        assert p.market_value == p.qty * DEMO_PRICES[p.ticker]
        assert p.unrealized_pl == p.market_value - p.qty * p.avg_entry_price


def test_demo_broker_simulates_fills_in_memory():
    b = DemoBroker()
    before = b.get_positions()
    order = b.submit_order(OrderIntent(ticker="NVDA", side=OrderSide.BUY,
                                       notional=Decimal(2000), reason="t"))
    assert order.status == OrderStatus.FILLED
    assert order.filled_avg_price == DEMO_PRICES["NVDA"]
    assert order.filled_qty == (Decimal(2000) / DEMO_PRICES["NVDA"]).quantize(
        Decimal("0.0001"))
    assert b.get_order(order.id) == order
    assert b.get_orders(open_only=True) == []
    assert b.get_orders(open_only=False) == [order]
    assert b.get_positions() == before  # the sample portfolio never moves


def test_demo_broker_refuses_unknown_tickers_and_orders():
    b = DemoBroker()
    with pytest.raises(BrokerError):
        b.submit_order(OrderIntent(ticker="TSLA", side=OrderSide.BUY,
                                   qty=Decimal(1), reason="t"))
    with pytest.raises(BrokerError):
        b.get_order("nope")


def test_demo_equity_history_ends_at_current_equity():
    b = DemoBroker()
    hist = b.get_equity_history(30)
    assert len(hist) == 31
    assert hist[-1][1] == b.get_account().equity
    assert [t for t, _ in hist] == sorted(t for t, _ in hist)
    assert len(b.get_equity_history(10_000)) == 201


def test_demo_data_is_offline_and_deterministic():
    d = DemoData()
    q = d.get_quote("nvda")
    assert q.ticker == "NVDA" and q.price == DEMO_PRICES["NVDA"]
    assert q.previous_close is not None
    with pytest.raises(LookupError):
        d.get_quote("TSLA")
    bars = d.get_bars("AAPL", days=20)
    assert len(bars) == 20
    assert [b.close for b in bars] == [b.close for b in d.get_bars("AAPL", days=20)]


# -- seed data --


@pytest.mark.parametrize("sid", sorted(STRATEGY_FILES))
def test_seed_strategies_are_valid(tmp_path, sid):
    path = tmp_path / f"{sid}.yaml"
    path.write_text(STRATEGY_FILES[sid])
    assert load_strategy(path).id == sid


def test_seed_demo_populates_every_page_source_and_is_idempotent(tmp_path):
    conn = connect(tmp_path / "demo.db")
    assert seed_demo(conn, tmp_path / "strategies", tmp_path / "memory") is True
    assert seed_demo(conn, tmp_path / "strategies", tmp_path / "memory") is False

    def count(sql):
        return conn.execute(sql).fetchone()[0]

    assert count("SELECT COUNT(*) FROM trades WHERE account='paper'") == 3
    assert count("SELECT COUNT(*) FROM pending_reviews WHERE status='pending'") == 2
    assert count("SELECT COUNT(*) FROM reports") == 1
    assert count("SELECT COUNT(*) FROM memory_log") == 4
    assert sorted(p.stem for p in (tmp_path / "strategies" / "paper").glob("*.yaml")) \
        == sorted(STRATEGY_FILES)
    assert (tmp_path / "memory" / "user_profile.md").exists()
    dismissed = conn.execute("SELECT value FROM app_state WHERE key = ?",
                             (SETUP_DISMISSED_KEY,)).fetchone()
    assert dismissed is not None


# -- the whole demo app --


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A demo started from a 'real install' directory whose .env and shell
    environment both carry live-looking credentials."""
    for key in OUTBOUND_ENV:
        monkeypatch.delenv(key.upper(), raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "real-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "real-bot")
    real = tmp_path / "real-install"
    real.mkdir()
    (real / ".env").write_text("ALPACA_API_KEY='real-key'\nWEB_TOKEN='real-token'\n")
    monkeypatch.chdir(real)  # restored at teardown, after prepare_demo's chdir
    workdir = tmp_path / "demo"
    app, token = cli.prepare_demo(workdir, "127.0.0.1")
    with TestClient(app) as client:
        client.post("/login", data={"token": token})
        yield client, real, workdir, token


def test_demo_never_touches_the_starting_directory(demo):
    _client, real, workdir, token = demo
    assert token == DEMO_TOKEN
    assert sorted(p.name for p in real.iterdir()) == [".env"]
    assert "real-token" in (real / ".env").read_text()
    assert (workdir / "allpath-trade.db").exists()
    assert (workdir / "strategies" / "paper" / "nvda-ai-infra.yaml").exists()


def test_demo_scrubs_outbound_credentials(demo):
    client, *_ = demo
    import os

    assert "ALPACA_API_KEY" not in os.environ
    assert "TELEGRAM_BOT_TOKEN" not in os.environ
    c = client.app.state.holder.get()
    assert c.settings.alpaca_api_key == ""
    assert c.settings.telegram_bot_token == ""
    assert c.broker.name == "demo"
    assert isinstance(c.data, DemoData)
    assert getattr(client.app.state, "telegram_thread", None) is None
    assert getattr(client.app.state, "scheduler", None) is None


@pytest.mark.parametrize("path,needle", [
    ("/", "NVDA"),
    ("/strategies", "AI infrastructure core position"),
    ("/reviews", "Agent recommends"),
    ("/reports", "one confirm order"),
    ("/memory", "long-term investor"),
])
def test_demo_pages_render_the_sample_data(demo, path, needle):
    client, *_ = demo
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 200, (path, r.status_code, r.headers.get("location"))
    assert needle in r.text
    assert_english_only(r.text)


def test_demo_rerun_on_same_dir_keeps_token_and_does_not_reseed(tmp_path, monkeypatch):
    for key in OUTBOUND_ENV:
        monkeypatch.delenv(key.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    workdir = tmp_path / "demo"
    _, first = cli.prepare_demo(workdir, "0.0.0.0")
    _, second = cli.prepare_demo(workdir, "0.0.0.0")
    assert first == second != DEMO_TOKEN
    conn = connect(workdir / "allpath-trade.db")
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3


def test_main_dispatches_demo_before_reading_the_local_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # An .env that every other command would reject with exit 2.
    (tmp_path / ".env").write_text("SENTINEL_INTERVAL_MINUTES=0\n")
    seen = {}

    def fake(workdir, host, port):
        seen.update(workdir=workdir, host=host, port=port)
        return 0

    monkeypatch.setattr(cli, "cmd_demo", fake)
    assert cli.main(["demo", "--port", "8799"]) == 0
    assert seen == {"workdir": None, "host": "127.0.0.1", "port": 8799}
    assert cli.main(["status"]) == 2  # sanity: the .env really is invalid


def test_approving_demo_reviews_fills_in_the_sandbox(demo):
    client, real, workdir, _ = demo
    c = client.app.state.holder.get()
    pending = {r["kind"]: r["id"] for r in c.queue.list()}
    assert set(pending) == {"order", "strategy_revision"}

    result = c.queue.approve(pending["order"])
    assert result.order is not None and result.order.status == OrderStatus.FILLED
    assert result.order.id.startswith("demo-")
    row = c.journal.recent(limit=1)[0]
    assert row["ticker"] == "NVDA" and row["status"] == "filled"

    c.queue.approve(pending["strategy_revision"])
    text = (workdir / "strategies" / "paper" / "aapl-services.yaml").read_text()
    assert "price < 245" in text
    assert sorted(p.name for p in real.iterdir()) == [".env"]


def test_demo_banner_replaces_the_setup_banner(demo):
    client, *_ = demo
    html = client.get("/strategies").text
    assert "Demo mode" in html
    assert "Setup incomplete" not in html
