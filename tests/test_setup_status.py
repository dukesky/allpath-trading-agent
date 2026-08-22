"""setup-wizard T2: the pure predicates behind the first-run gate --
what counts as "not set up yet", and when a request should be bounced to
the wizard. No FastAPI, no database: `allpath_trade/web/setup_status.py`
is deliberately free of both so the decision can be tested (and reasoned
about) on its own, with the wiring covered separately in
tests/test_web_setup_gate.py."""

from __future__ import annotations

import pytest

from allpath_trade.config import Settings
from allpath_trade.web.setup_status import (
    GATE_EXEMPT_PREFIXES,
    SETUP_DISMISSED_KEY,
    alpaca_keys_missing,
    llm_key_missing,
    setup_dismissed,
    setup_missing,
    should_redirect_to_setup,
)


class FakeAppState:
    """Duck-typed stand-in for store.app_state.AppState -- only `get` is
    read here, and a real one needs a database connection this module has
    no business opening."""

    def __init__(self, **values: str) -> None:
        self.values = values

    def get(self, key: str) -> str | None:
        return self.values.get(key)


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


# -- setup_missing / the two component predicates --


@pytest.mark.parametrize("keys, expected", [
    ({}, ["LLM key", "Alpaca keys"]),
    ({"openrouter_api_key": "k"}, ["Alpaca keys"]),
    ({"alpaca_api_key": "a", "alpaca_secret_key": "s"}, ["LLM key"]),
    ({"openrouter_api_key": "k", "alpaca_api_key": "a",
      "alpaca_secret_key": "s"}, []),
])
def test_setup_missing_covers_the_four_key_combinations(keys, expected):
    assert setup_missing(_settings(**keys)) == expected


def test_setup_missing_lists_the_llm_key_before_the_alpaca_keys():
    # Order is load-bearing: it is what the banner joins into its sentence,
    # and the wizard's own step order (LLM first, broker second) follows it.
    assert setup_missing(_settings()) == ["LLM key", "Alpaca keys"]


@pytest.mark.parametrize("provider, field", [
    ("openrouter", "openrouter_api_key"),
    ("anthropic", "anthropic_api_key"),
    ("openai", "openai_api_key"),
])
def test_llm_key_missing_reads_the_key_for_the_selected_provider(provider, field):
    assert llm_key_missing(_settings(llm_provider=provider)) is True
    assert llm_key_missing(_settings(llm_provider=provider, **{field: "k"})) is False


@pytest.mark.parametrize("provider, other", [
    ("openrouter", "anthropic_api_key"),
    ("anthropic", "openai_api_key"),
    ("openai", "openrouter_api_key"),
])
def test_another_providers_key_does_not_satisfy_the_selected_provider(provider, other):
    """A key for a provider you are not using cannot make a call -- the
    factory (llm/factory.py) only ever reads the selected provider's own
    field, so counting any-key-anywhere as configured would send the user
    off the wizard into a chat that fails on its first message."""
    assert llm_key_missing(_settings(llm_provider=provider, **{other: "k"})) is True


def test_llm_key_missing_ignores_whitespace_only_keys():
    assert llm_key_missing(_settings(openrouter_api_key="   ")) is True


def test_llm_key_missing_treats_an_unknown_provider_as_missing():
    """`llm_provider` is a free-text setting, so a typo ("opnerouter") or a
    provider added to .env by hand must not read as configured -- there is
    no field to satisfy it with."""
    assert llm_key_missing(_settings(llm_provider="nonesuch",
                                     openrouter_api_key="k")) is True


@pytest.mark.parametrize("keys, expected", [
    ({}, True),
    ({"alpaca_api_key": "a"}, True),
    ({"alpaca_secret_key": "s"}, True),
    ({"alpaca_api_key": "  ", "alpaca_secret_key": "s"}, True),
    ({"alpaca_api_key": "a", "alpaca_secret_key": "s"}, False),
])
def test_alpaca_keys_missing_needs_both_halves(keys, expected):
    # A half-filled pair cannot authenticate, and app.py::_build_broker
    # already treats it as unconfigured -- this predicate must agree with
    # it, or the dashboard would show a placeholder broker with no banner
    # explaining why.
    assert alpaca_keys_missing(_settings(**keys)) is expected


# -- the dismissal flag --


def test_setup_dismissed_is_false_when_unset_or_blank():
    assert setup_dismissed(FakeAppState()) is False
    assert setup_dismissed(FakeAppState(**{SETUP_DISMISSED_KEY: ""})) is False


def test_setup_dismissed_is_true_once_the_flag_is_written():
    assert setup_dismissed(FakeAppState(**{SETUP_DISMISSED_KEY: "1"})) is True


# -- should_redirect_to_setup: the full matrix --


CONFIGURED = {"openrouter_api_key": "k", "alpaca_api_key": "a",
              "alpaca_secret_key": "s"}


@pytest.mark.parametrize("missing", [True, False])
@pytest.mark.parametrize("dismissed", [True, False])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("path", ["/", "/setup"])
def test_should_redirect_to_setup_matrix(missing, dismissed, method, path):
    settings = _settings() if missing else _settings(**CONFIGURED)
    state = FakeAppState(**({SETUP_DISMISSED_KEY: "1"} if dismissed else {}))
    expected = missing and not dismissed and method == "GET" and path == "/"
    assert should_redirect_to_setup(method, path, settings, state) is expected


@pytest.mark.parametrize("path", [
    "/setup", "/setup/", "/login", "/logout", "/static/app.css",
    "/a/abc123", "/healthz", "/healthz/", "/account/switch",
])
def test_exempt_paths_are_never_redirected(path):
    assert should_redirect_to_setup("GET", path, _settings(), FakeAppState()) is False


@pytest.mark.parametrize("path", ["/", "/chat", "/reviews", "/settings",
                                  "/strategies", "/memory", "/reports"])
def test_every_ordinary_page_is_redirected_while_setup_is_unfinished(path):
    assert should_redirect_to_setup("GET", path, _settings(), FakeAppState()) is True


@pytest.mark.parametrize("path", ["/setups", "/setup-notes", "/logout-all",
                                  "/staticky", "/account/switcheroo"])
def test_a_path_that_merely_starts_with_an_exempt_prefix_is_not_exempt(path):
    """`startswith` on the bare prefixes would hand a free pass to any
    future route whose name happens to begin with one of them -- the match
    is on whole path segments instead."""
    assert should_redirect_to_setup("GET", path, _settings(), FakeAppState()) is True


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD"])
def test_only_get_is_gated(method):
    """The gate exists to put a browser NAVIGATION in front of the wizard.
    Redirecting a POST would silently swallow a form submission (a 302 on a
    POST is re-issued as a GET, so the body is dropped), and the one POST
    a half-configured install most needs -- the settings save that enters
    the very keys being asked for -- is exactly what would break. HEAD is
    left alone for the same reason it is pointless to gate: nothing is
    rendered for a human to read."""
    assert should_redirect_to_setup(method, "/", _settings(), FakeAppState()) is False


def test_exempt_prefixes_include_every_pre_setup_surface():
    assert GATE_EXEMPT_PREFIXES == ("/setup", "/login", "/logout", "/static",
                                    "/a/", "/healthz", "/account/switch")
