"""B2: the state a brand-new user is actually in the first time they run
`allpath-trade serve` -- no LLM key, no broker credentials, no strategies,
no memory directory. Every other web test fixture configures at least a
FakeBroker that succeeds and (for the chat suite) an LLM key; this is the
state before any of that setup happens, and it's the first thing a new
user sees. Every page must still render, not 500."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from allpath_trade.broker.base import Broker
from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from tests.helpers import assert_english_only


class _NoCredsBroker(Broker):
    """Stands in for what AlpacaBroker actually does when ALPACA_API_KEY /
    ALPACA_SECRET_KEY are blank: every call fails, the same outcome
    test_web_dashboard.py's broker-outage test already covers in isolation
    -- here it's one piece of the combined brand-new-install state."""

    name = "alpaca"
    is_paper = True

    def get_account(self):
        raise RuntimeError("invalid Alpaca credentials")

    def get_positions(self):
        raise RuntimeError("invalid Alpaca credentials")

    def get_order(self, order_id):
        raise NotImplementedError

    def get_orders(self, open_only=True):
        raise NotImplementedError

    def submit_order(self, intent):
        raise NotImplementedError

    def cancel_order(self, order_id):
        raise NotImplementedError


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # strategies_dir exists but is empty -- build_components itself
    # mkdir(parents=True, exist_ok=True)s it, matching a real first run
    # where the directory was never populated, not one that was never
    # created at all.
    (tmp_path / "strategies").mkdir()
    # memory_dir is deliberately NOT created -- MemoryStore lazily creates
    # it on the agent's first write, and /memory must render before that
    # ever happens (see test_web_memory.py's own fresh-install test).
    settings = Settings(
        _env_file=None, db_path=tmp_path / "t.db",
        strategies_dir=tmp_path / "strategies", memory_dir=tmp_path / "memory",
        web_token="secret")
        # openrouter/openai/anthropic_api_key, alpaca_api_key/secret_key all
        # default to "" -- no LLM key, no broker credentials.
    with TestClient(create_app(settings, broker=_NoCredsBroker())) as c:
        c.post("/login", data={"token": "secret"})
        yield c


@pytest.mark.parametrize("path", ["/", "/chat", "/reviews", "/strategies", "/memory", "/settings", "/reports"])
def test_every_page_renders_for_a_brand_new_install(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert_english_only(r.text)


def test_dashboard_shows_the_broker_unavailable_banner(client):
    body = client.get("/").text
    assert "unavailable" in body.lower()


def test_chat_shows_the_no_llm_key_banner(client):
    # F5: `base.html` puts a "Settings" nav link on every page, so asserting
    # only "Settings" in body passed whether or not the banner itself
    # rendered -- assert on the banner's own copy instead (see
    # _chat_messages.html's llm_error branch).
    body = client.get("/chat").text
    assert "Chat needs an LLM key" in body


def test_strategies_and_memory_show_their_own_empty_states(client):
    assert "No strategies yet" in client.get("/strategies").text
    assert "Empty." in client.get("/memory").text
