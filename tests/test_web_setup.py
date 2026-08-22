"""setup-wizard T3: the `/setup` wizard itself.

The gate (T2, tests/test_web_setup_gate.py) is what sends a fresh install
here; this suite is about what it finds when it arrives -- the four steps,
what each save writes to `.env` (and, just as load-bearing, what it does
NOT write), the two test endpoints that touch the network but never disk,
and the "skip for now"/"finish" flag both of which the gate then reads.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import ClassVar

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

import allpath_trade.web.routes.setup as setup_route
from allpath_trade.broker.base import Account
from allpath_trade.config import Settings, SettingsStore
from allpath_trade.web.app import create_app
from allpath_trade.web.setup_status import SETUP_DISMISSED_KEY
from tests.helpers import assert_english_only
from tests.test_sentinel import FakeBroker


def _make_client(tmp_path, **settings_kwargs) -> TestClient:
    (tmp_path / "strategies").mkdir(exist_ok=True)
    # WEB_TOKEN on disk, not merely in the in-memory Settings: every save
    # below ends in `holder.rebuild()`, which reloads Settings straight from
    # this file -- without the token there the first rebuild would log the
    # already-authenticated test client out mid-test (the same reasoning
    # tests/test_web_settings.py's fixture spells out).
    SettingsStore(tmp_path / ".env").set("WEB_TOKEN", "secret")
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret",
                        **settings_kwargs)
    return TestClient(create_app(settings, broker=FakeBroker()))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A brand-new install: logged in, no LLM key, no Alpaca keys."""
    monkeypatch.chdir(tmp_path)
    with _make_client(tmp_path) as c:
        c.post("/login", data={"token": "secret"})
        yield c


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with _make_client(tmp_path, openrouter_api_key="sk-or-stored-key",
                      alpaca_api_key="alpaca-key", alpaca_secret_key="alpaca-secret") as c:
        c.post("/login", data={"token": "secret"})
        yield c


def env_keys(tmp_path) -> set[str]:
    return set(dotenv_values(tmp_path / ".env"))


def stored(tmp_path, key: str) -> str | None:
    return SettingsStore(tmp_path / ".env").get(key)


# -- which step you land on --------------------------------------------------


def test_the_default_step_is_the_first_incomplete_one(client):
    body = client.get("/setup").text
    assert 'data-step="1"' in body
    assert 'class="step on"' in body


def test_a_stored_llm_key_moves_the_default_step_to_alpaca(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with _make_client(tmp_path, openrouter_api_key="k") as c:
        c.post("/login", data={"token": "secret"})
        assert 'data-step="2"' in c.get("/setup").text


def test_a_fully_configured_install_defaults_to_the_shadow_step(configured):
    assert 'data-step="3"' in configured.get("/setup").text


def test_setup_never_redirects_even_when_nothing_is_missing(configured):
    """The Settings "Re-run setup" link has to land somewhere, forever."""
    r = configured.get("/setup", follow_redirects=False)
    assert r.status_code == 200


def test_an_explicit_step_wins_over_the_default(client):
    assert 'data-step="4"' in client.get("/setup?step=4").text


@pytest.mark.parametrize("raw", ["0", "9", "abc", "", "-1"])
def test_a_garbled_step_falls_back_instead_of_failing(client, raw):
    r = client.get(f"/setup?step={raw}")
    assert r.status_code == 200
    assert 'data-step="1"' in r.text


def test_the_wizard_carries_no_setup_banner(client):
    assert "Setup incomplete" not in client.get("/setup").text


# -- what each step says -----------------------------------------------------


@pytest.mark.parametrize("step", [1, 2, 3, 4])
def test_every_step_is_english_only(client, step):
    assert_english_only(client.get(f"/setup?step={step}").text)


def test_step_one_offers_both_providers_and_where_to_get_a_key(client):
    body = client.get("/setup?step=1").text
    assert "https://openrouter.ai/keys" in body
    assert "https://console.anthropic.com/settings/keys" in body
    assert 'value="openrouter"' in body
    assert 'value="anthropic"' in body
    # One sentence about the three tiers, no model pickers here.
    assert "chat" in body and "review" in body and "memory" in body
    assert "chat_model" not in body


def test_step_two_walks_through_the_alpaca_signup(client):
    body = client.get("/setup?step=2").text
    assert "https://alpaca.markets" in body
    assert "Paper Trading" in body
    assert "Generate New Keys" in body
    assert "the secret is shown once" in body


def test_step_three_explains_shadow_and_offers_the_three_actions(client):
    body = client.get("/setup?step=3").text
    assert "Shadow mirrors your real brokerage." in body
    assert "nothing is ever routed" in body
    assert 'action="/setup/open-chat"' in body
    assert 'hx-post="/settings/shadow/csv-preview"' in body
    assert 'id="shadow-csv-result"' in body
    assert "position" in body  # the ledger summary


def test_step_four_links_the_remaining_optional_setup(client):
    body = client.get("/setup?step=4").text
    assert 'href="/settings#telegram"' in body
    assert 'href="/chat"' in body
    assert 'action="/setup/finish"' in body


def test_every_step_can_be_skipped(client):
    for step in (1, 2, 3, 4):
        assert 'action="/setup/skip"' in client.get(f"/setup?step={step}").text


# -- secrets on the page -----------------------------------------------------


def test_the_llm_key_input_is_masked_and_never_prefilled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    secret = "sk-or-v1-abcdefghijklmnop"
    with _make_client(tmp_path, openrouter_api_key=secret) as c:
        c.post("/login", data={"token": "secret"})
        body = c.get("/setup?step=1").text
        assert secret not in body
        assert "•" * 8 + secret[-4:] in body
        assert 'name="llm_api_key" type="password"' in body
        assert 'name="llm_api_key" type="password" value=' not in body


def test_the_alpaca_inputs_are_masked_and_never_prefilled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with _make_client(tmp_path, alpaca_api_key="alpaca-key-1234",
                      alpaca_secret_key="alpaca-secret-9876") as c:
        c.post("/login", data={"token": "secret"})
        body = c.get("/setup?step=2").text
        assert "alpaca-key-1234" not in body
        assert "alpaca-secret-9876" not in body
        assert "•" * 8 + "1234" in body
        assert "•" * 8 + "9876" in body
        assert "value=" not in _input_line(body, "alpaca_api_key")
        assert "value=" not in _input_line(body, "alpaca_secret_key")


def _input_line(body: str, field: str) -> str:
    for line in body.splitlines():
        if f'name="{field}"' in line:
            return line
    raise AssertionError(f"no input named {field!r} on the page")


# -- step 1: saving ----------------------------------------------------------


def test_step_one_writes_only_the_provider_and_its_key(client, tmp_path):
    r = client.post("/setup/step/1",
                    data={"llm_provider": "openrouter", "llm_api_key": "sk-or-typed"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup?step=2"
    assert env_keys(tmp_path) == {"WEB_TOKEN", "LLM_PROVIDER", "OPENROUTER_API_KEY"}
    assert stored(tmp_path, "LLM_PROVIDER") == "openrouter"
    assert stored(tmp_path, "OPENROUTER_API_KEY") == "sk-or-typed"


def test_step_one_writes_the_anthropic_key_when_anthropic_is_chosen(client, tmp_path):
    client.post("/setup/step/1",
                data={"llm_provider": "anthropic", "llm_api_key": "sk-ant-typed"})
    assert env_keys(tmp_path) == {"WEB_TOKEN", "LLM_PROVIDER", "ANTHROPIC_API_KEY"}
    assert stored(tmp_path, "ANTHROPIC_API_KEY") == "sk-ant-typed"


def test_a_blank_key_keeps_the_stored_one(configured, tmp_path):
    SettingsStore(tmp_path / ".env").set("OPENROUTER_API_KEY", "sk-or-stored-key")
    configured.post("/setup/step/1", data={"llm_provider": "openrouter", "llm_api_key": ""})
    assert stored(tmp_path, "OPENROUTER_API_KEY") == "sk-or-stored-key"
    assert configured.app.state.holder.get().settings.openrouter_api_key == "sk-or-stored-key"


def test_the_saved_key_is_live_without_a_restart(client):
    client.post("/setup/step/1",
                data={"llm_provider": "anthropic", "llm_api_key": "sk-ant-typed"})
    s = client.app.state.holder.get().settings
    assert s.llm_provider == "anthropic"
    assert s.anthropic_api_key == "sk-ant-typed"


# -- step 1: an install on a provider the wizard doesn't offer ---------------
#
# Whole-branch review (Important 1). Settings offers three providers, the
# wizard's radios offer two. An `openai` install that followed "Re-run
# setup" landed on step 1 with the OpenRouter radio pre-checked (the old
# `_page` fallback) and, on a blank "Save & continue", had LLM_PROVIDER
# rewritten to `openrouter` -- a provider with no key -- which re-gated the
# whole app behind the setup wizard it had just walked out of.


@pytest.fixture
def openai_install(tmp_path, monkeypatch):
    """A working install on the one provider the wizard doesn't offer."""
    monkeypatch.chdir(tmp_path)
    store = SettingsStore(tmp_path / ".env")
    store.set("LLM_PROVIDER", "openai")
    store.set("OPENAI_API_KEY", "sk-openai-stored")
    with _make_client(tmp_path, llm_provider="openai",
                      openai_api_key="sk-openai-stored",
                      alpaca_api_key="alpaca-key",
                      alpaca_secret_key="alpaca-secret") as c:
        c.post("/login", data={"token": "secret"})
        yield c


def test_an_openai_install_lands_on_the_shadow_step(openai_install):
    assert 'data-step="3"' in openai_install.get("/setup").text


def test_step_one_names_the_provider_it_cannot_offer(openai_install):
    body = openai_install.get("/setup?step=1").text
    assert "Current provider: openai (keys managed in Settings)" in body
    # And pre-checks NEITHER radio: whichever one was checked is what a
    # blank submit would switch this install to.
    assert "checked" not in body
    assert_english_only(body)


def test_a_blank_save_on_an_openai_install_changes_nothing(openai_install, tmp_path):
    before = dict(dotenv_values(tmp_path / ".env"))
    r = openai_install.post("/setup/step/1",
                            data={"llm_provider": "openrouter", "llm_api_key": ""},
                            follow_redirects=False)
    assert r.status_code == 303
    assert dict(dotenv_values(tmp_path / ".env")) == before
    settings = openai_install.app.state.holder.get().settings
    assert settings.llm_provider == "openai"
    assert settings.openai_api_key == "sk-openai-stored"


def test_a_blank_save_does_not_re_gate_a_working_install(openai_install):
    openai_install.post("/setup/step/1",
                        data={"llm_provider": "openrouter", "llm_api_key": ""})
    # The gate is what a fresh GET anywhere else would hit.
    r = openai_install.get("/", follow_redirects=False)
    assert r.status_code == 200


def test_a_typed_key_still_switches_the_provider(openai_install, tmp_path):
    """The wizard is still how you MOVE an install to one of its two
    providers -- it just takes a key to do it, not a bare submit."""
    openai_install.post("/setup/step/1",
                        data={"llm_provider": "anthropic",
                              "llm_api_key": "sk-ant-typed"})
    assert stored(tmp_path, "LLM_PROVIDER") == "anthropic"
    assert stored(tmp_path, "ANTHROPIC_API_KEY") == "sk-ant-typed"
    assert stored(tmp_path, "OPENAI_API_KEY") == "sk-openai-stored"


def test_switching_to_a_provider_that_already_has_a_key_needs_no_retype(
        tmp_path, monkeypatch):
    """The other half of the rule: a blank submit DOES switch provider when
    the target already has a stored key, so rotating between two configured
    providers doesn't force a paste."""
    monkeypatch.chdir(tmp_path)
    store = SettingsStore(tmp_path / ".env")
    store.set("LLM_PROVIDER", "openrouter")
    store.set("OPENROUTER_API_KEY", "sk-or-stored")
    store.set("ANTHROPIC_API_KEY", "sk-ant-stored")
    with _make_client(tmp_path, llm_provider="openrouter",
                      openrouter_api_key="sk-or-stored",
                      anthropic_api_key="sk-ant-stored") as c:
        c.post("/login", data={"token": "secret"})
        c.post("/setup/step/1",
               data={"llm_provider": "anthropic", "llm_api_key": ""})
        assert stored(tmp_path, "LLM_PROVIDER") == "anthropic"
        assert c.app.state.holder.get().settings.llm_provider == "anthropic"


def test_a_blank_submit_never_switches_to_a_provider_with_no_key(client, tmp_path):
    """A fresh install, nothing stored anywhere: "Save & continue" with an
    empty box must not record a provider whose key is missing -- that is
    exactly the state the wizard exists to get out of."""
    r = client.post("/setup/step/1",
                    data={"llm_provider": "anthropic", "llm_api_key": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert env_keys(tmp_path) == {"WEB_TOKEN"}


def test_an_unknown_provider_is_refused_and_writes_nothing(client, tmp_path):
    r = client.post("/setup/step/1",
                    data={"llm_provider": "hotdog", "llm_api_key": "sk-typed"},
                    follow_redirects=False)
    assert r.status_code == 400
    assert env_keys(tmp_path) == {"WEB_TOKEN"}
    assert "sk-typed" not in r.text


# -- step 2: saving ----------------------------------------------------------


def test_step_two_writes_both_alpaca_keys(client, tmp_path):
    r = client.post("/setup/step/2",
                    data={"alpaca_api_key": "AK-typed", "alpaca_secret_key": "AS-typed"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup?step=3"
    assert env_keys(tmp_path) == {"WEB_TOKEN", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"}
    assert stored(tmp_path, "ALPACA_API_KEY") == "AK-typed"
    assert stored(tmp_path, "ALPACA_SECRET_KEY") == "AS-typed"


def test_blank_alpaca_fields_keep_what_is_stored(configured, tmp_path):
    SettingsStore(tmp_path / ".env").set("ALPACA_API_KEY", "AK-stored")
    SettingsStore(tmp_path / ".env").set("ALPACA_SECRET_KEY", "AS-stored")
    configured.post("/setup/step/2", data={"alpaca_api_key": "", "alpaca_secret_key": ""})
    assert stored(tmp_path, "ALPACA_API_KEY") == "AK-stored"
    assert stored(tmp_path, "ALPACA_SECRET_KEY") == "AS-stored"


def test_step_two_can_fill_only_the_half_that_is_missing(client, tmp_path):
    client.post("/setup/step/2", data={"alpaca_api_key": "AK-typed", "alpaca_secret_key": ""})
    assert env_keys(tmp_path) == {"WEB_TOKEN", "ALPACA_API_KEY"}


# -- step 3, skip, finish ----------------------------------------------------


def test_step_three_continues_to_the_last_step(client):
    r = client.post("/setup/step/3", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup?step=4"


def test_skip_sets_the_dismissed_flag_and_goes_home(client):
    r = client.post("/setup/skip", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert client.app.state.holder.get().app_state.get(SETUP_DISMISSED_KEY) == "1"


def test_finish_sets_the_same_flag(client):
    r = client.post("/setup/finish", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert client.app.state.holder.get().app_state.get(SETUP_DISMISSED_KEY) == "1"


def test_skipping_lifts_the_gate(client):
    client.post("/setup/skip")
    assert client.get("/", follow_redirects=False).status_code == 200


def test_open_chat_switches_to_shadow_and_lands_on_the_import_hint(client):
    r = client.post("/setup/open-chat", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/chat?hint=import"
    assert client.cookies.get("account") == "shadow"


# -- test endpoints ----------------------------------------------------------


class _FakeLLM:
    model = "anthropic/claude-haiku-4.5"

    def __init__(self, boom: Exception | None = None) -> None:
        self.boom = boom
        self.calls: list[list[dict]] = []

    def complete(self, messages, tools=None):
        self.calls.append(messages)
        if self.boom:
            raise self.boom
        return SimpleNamespace(text="OK", input_tokens=1, output_tokens=1)


def test_test_llm_reports_the_model_that_replied(client, tmp_path, monkeypatch):
    fake = _FakeLLM()
    seen: dict = {}

    def build(settings, tier="chat", usage_store=None):
        seen["tier"] = tier
        seen["key"] = settings.openrouter_api_key
        return fake

    monkeypatch.setattr(setup_route, "build_llm", build)
    before = (tmp_path / ".env").read_text()
    r = client.post("/setup/test-llm",
                    data={"llm_provider": "openrouter", "llm_api_key": "sk-typed-probe"})
    assert r.status_code == 200
    assert "OK · anthropic/claude-haiku-4.5 replied" in r.text
    assert "sk-typed-probe" not in r.text
    assert seen == {"tier": "chat", "key": "sk-typed-probe"}
    assert fake.calls == [[{"role": "user", "content": "Reply with OK."}]]
    assert (tmp_path / ".env").read_text() == before


def test_test_llm_falls_back_to_the_stored_key(configured, monkeypatch):
    seen: dict = {}

    def build(settings, tier="chat", usage_store=None):
        seen["key"] = settings.openrouter_api_key
        return _FakeLLM()

    monkeypatch.setattr(setup_route, "build_llm", build)
    configured.post("/setup/test-llm", data={"llm_provider": "openrouter", "llm_api_key": ""})
    assert seen["key"] == "sk-or-stored-key"


def test_test_llm_reports_a_failure_without_echoing_the_key(client, tmp_path, monkeypatch):
    typed = "sk-typed-probe"
    monkeypatch.setattr(
        setup_route, "build_llm",
        lambda settings, tier="chat", usage_store=None: _FakeLLM(
            RuntimeError(f"401 unauthorized for key {typed}")))
    before = (tmp_path / ".env").read_text()
    r = client.post("/setup/test-llm",
                    data={"llm_provider": "openrouter", "llm_api_key": typed})
    assert r.status_code == 200
    assert typed not in r.text
    assert "RuntimeError" in r.text
    assert "401 unauthorized" in r.text
    assert (tmp_path / ".env").read_text() == before


def test_test_llm_needs_a_key_from_somewhere(client, monkeypatch):
    monkeypatch.setattr(setup_route, "build_llm",
                        lambda *a, **k: pytest.fail("must not build a client"))
    r = client.post("/setup/test-llm", data={"llm_provider": "openrouter", "llm_api_key": ""})
    assert "Enter an API key first." in r.text


def test_a_long_error_is_truncated(client, monkeypatch):
    monkeypatch.setattr(
        setup_route, "build_llm",
        lambda settings, tier="chat", usage_store=None: _FakeLLM(RuntimeError("x" * 500)))
    r = client.post("/setup/test-llm",
                    data={"llm_provider": "openrouter", "llm_api_key": "k"})
    assert "x" * 120 in r.text
    assert "x" * 130 not in r.text


class _FakeAlpaca:
    last: ClassVar[dict] = {}

    def __init__(self, key, secret, paper=True):
        type(self).last = {"key": key, "secret": secret, "paper": paper}

    def get_account(self):
        return Account(equity=Decimal(100000), cash=Decimal(100000),
                       buying_power=Decimal(200000))


def test_test_broker_reports_the_equity_it_read(client, tmp_path, monkeypatch):
    monkeypatch.setattr(setup_route, "AlpacaBroker", _FakeAlpaca)
    before = (tmp_path / ".env").read_text()
    r = client.post("/setup/test-broker",
                    data={"alpaca_api_key": "AK-typed", "alpaca_secret_key": "AS-typed"})
    assert r.status_code == 200
    assert "Connected · equity $100,000.00" in r.text
    assert "AS-typed" not in r.text
    assert _FakeAlpaca.last == {"key": "AK-typed", "secret": "AS-typed", "paper": True}
    assert (tmp_path / ".env").read_text() == before


def test_test_broker_falls_back_to_the_stored_pair(configured, monkeypatch):
    monkeypatch.setattr(setup_route, "AlpacaBroker", _FakeAlpaca)
    configured.post("/setup/test-broker",
                    data={"alpaca_api_key": "", "alpaca_secret_key": ""})
    assert _FakeAlpaca.last == {"key": "alpaca-key", "secret": "alpaca-secret", "paper": True}


def test_test_broker_probes_the_environment_this_install_trades_in(
        tmp_path, monkeypatch):
    """Whole-branch review (M5): `ALPACA_PAPER=false` is a deliberate
    hand-edit of `.env`, and the wizard's Test button has to reach the same
    endpoint the app itself will -- a "Connected · equity ..." read off the
    paper API tells a live install nothing about the keys it actually uses.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_route, "AlpacaBroker", _FakeAlpaca)
    with _make_client(tmp_path, alpaca_paper=False) as c:
        c.post("/login", data={"token": "secret"})
        c.post("/setup/test-broker",
               data={"alpaca_api_key": "AK", "alpaca_secret_key": "AS"})
    assert _FakeAlpaca.last["paper"] is False


def test_test_broker_needs_both_halves(client, monkeypatch):
    monkeypatch.setattr(setup_route, "AlpacaBroker",
                        lambda *a, **k: pytest.fail("must not build a broker"))
    r = client.post("/setup/test-broker",
                    data={"alpaca_api_key": "AK", "alpaca_secret_key": ""})
    assert "Enter both the API key and the secret key first." in r.text


def test_test_broker_reports_a_failure_without_echoing_the_secret(client, monkeypatch):
    class Boom:
        def __init__(self, key, secret, paper=True):
            pass

        def get_account(self):
            raise RuntimeError("403 forbidden for AS-typed")

    monkeypatch.setattr(setup_route, "AlpacaBroker", Boom)
    r = client.post("/setup/test-broker",
                    data={"alpaca_api_key": "AK-typed", "alpaca_secret_key": "AS-typed"})
    assert "AS-typed" not in r.text
    assert "RuntimeError" in r.text
    assert "403 forbidden" in r.text


def test_a_broker_failure_in_the_constructor_is_reported_too(client, monkeypatch):
    class Boom:
        def __init__(self, key, secret, paper=True):
            raise ValueError("bad key format")

    monkeypatch.setattr(setup_route, "AlpacaBroker", Boom)
    r = client.post("/setup/test-broker",
                    data={"alpaca_api_key": "AK", "alpaca_secret_key": "AS"})
    assert "ValueError" in r.text
    assert "bad key format" in r.text


# -- the wizard's own exits must not be bounced back to it -------------------
#
# Review round 2 (Important): step 3 and step 4 are almost entirely links and
# buttons OUT of the wizard -- Open Chat, the CSV import's confirm (which
# lands on /settings), and step 4's Telegram/notifications/chat links. While
# `setup_dismissed` is unset the gate (web/auth.py) 302s every one of those
# GETs straight back to /setup, so the exits were dead and the CSV import's
# "queued as #N" notice was lost on the way. Controller ruling: reaching
# step 3 IS the user's answer on both required keys, so serving step 3 or 4
# sets the flag. Steps 1-2 have no exit but "Skip for now", which sets it
# itself -- a fresh user loading step 1 stays gated.


def flag(client) -> str | None:
    return client.app.state.holder.get().app_state.get(SETUP_DISMISSED_KEY)


def test_open_chat_actually_reaches_the_chat_page(client):
    client.post("/setup/open-chat", follow_redirects=False)
    assert flag(client) == "1"
    r = client.get("/chat?hint=import", follow_redirects=False)
    assert r.status_code == 200


@pytest.mark.parametrize("step", [3, 4])
def test_rendering_a_step_with_exits_lifts_the_gate(client, step):
    assert client.get(f"/setup?step={step}").status_code == 200
    assert flag(client) == "1"
    # ... and the links on those steps now land where they point.
    for path in ("/settings", "/chat", "/"):
        assert client.get(path, follow_redirects=False).status_code == 200


@pytest.mark.parametrize("path", ["/setup", "/setup?step=1", "/setup?step=2"])
def test_the_first_two_steps_leave_a_fresh_user_gated(client, path):
    """The flag is the wizard's own escape hatch, not a side effect of
    opening it: someone who loads step 1 and wanders off must still be
    redirected back here on their next page view."""
    assert client.get(path).status_code == 200
    assert flag(client) is None
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/setup"


def test_saving_step_two_lifts_the_gate(client):
    client.post("/setup/step/2",
                data={"alpaca_api_key": "AK", "alpaca_secret_key": "AS"},
                follow_redirects=False)
    assert flag(client) == "1"


def test_a_csv_import_started_in_the_wizard_reaches_its_notice(client):
    """The embedded CSV form previews inline on step 3 but confirms on
    /settings -- which is exactly the redirect the gate used to eat, taking
    the "queued as #N" notice with it."""
    client.get("/setup?step=3")
    r = client.post("/settings/shadow/csv-confirm",
                    data={"csv_text": "AAPL,10,150\nCASH,1000\n"})
    assert r.status_code == 200
    assert "Ledger import queued for your approval as #1." in r.text


# -- nothing typed, nothing rebuilt -----------------------------------------


def _count_rebuilds(client, monkeypatch) -> list[int]:
    calls: list[int] = []
    holder = client.app.state.holder
    monkeypatch.setattr(holder, "rebuild", lambda *a, **k: calls.append(1))
    return calls


def test_an_all_blank_step_two_save_rebuilds_nothing(client, monkeypatch):
    calls = _count_rebuilds(client, monkeypatch)
    r = client.post("/setup/step/2",
                    data={"alpaca_api_key": "", "alpaca_secret_key": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup?step=3"
    assert calls == []


def test_a_step_two_save_with_a_value_does_rebuild(client, monkeypatch):
    calls = _count_rebuilds(client, monkeypatch)
    client.post("/setup/step/2", data={"alpaca_api_key": "AK", "alpaca_secret_key": ""},
                follow_redirects=False)
    assert calls == [1]
