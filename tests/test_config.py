from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from allpath_trade.config import Settings, SettingsStore


def test_settings_defaults(tmp_path: Path):
    s = Settings(_env_file=tmp_path / "nope.env")
    assert s.alpaca_paper is True
    assert s.alpaca_api_key == ""
    assert s.context_budget_tokens == 60000
    # Telegram channel is off by default -- empty token, no poller starts.
    assert s.telegram_bot_token == ""


def test_telegram_bot_token_loads_from_env(tmp_path: Path):
    store = SettingsStore(tmp_path / ".env")
    store.set("TELEGRAM_BOT_TOKEN", "123:abc")
    assert store.load().telegram_bot_token == "123:abc"


# -- Finding 4: range validation, not just type validation -- a negative
# sentinel_interval_minutes or a zero context_budget_tokens is the same
# class of brick Task 12 set out to prevent; the type check alone let both
# straight through (see allpath_trade/web/routes/settings.py's `save`,
# which validates by constructing a Settings before writing to .env).


def test_negative_sentinel_interval_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sentinel_interval_minutes=-5)


def test_zero_sentinel_interval_is_rejected():
    # 0 minutes means APScheduler's IntervalTrigger fires roughly every
    # second -- a hot loop against the broker, not a paused sentinel.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sentinel_interval_minutes=0)


def test_zero_context_budget_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, context_budget_tokens=0)


def test_out_of_range_smtp_port_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, smtp_port=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, smtp_port=70000)


# -- Ops-hardening: llm_timeout_seconds bounds a genuinely hung LLM call
# (see llm/factory.py) so it can never block the after-close chain forever.

def test_llm_timeout_defaults_to_180_seconds():
    assert Settings(_env_file=None).llm_timeout_seconds == 180


def test_llm_timeout_below_floor_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_timeout_seconds=5)


# -- Task 8 review Finding 1: a scheme-less ntfy_url reaches
# urllib.request.Request (ntfy.py's NtfyNotifier.send), which raises
# ValueError("unknown url type") for it -- and the settings-page hint says
# "paste the topic URL here", so pasting the bare topic (no "https://") is
# the expected mistake. The constraint has to live on the field itself so
# both the settings page and process startup are covered from one place.


def test_scheme_less_ntfy_url_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ntfy_url="ntfy.sh/my-topic")


def test_empty_ntfy_url_is_allowed():
    assert Settings(_env_file=None, ntfy_url="").ntfy_url == ""


def test_http_and_https_ntfy_url_are_allowed():
    assert (Settings(_env_file=None, ntfy_url="http://ntfy.sh/t").ntfy_url
            == "http://ntfy.sh/t")
    assert (Settings(_env_file=None, ntfy_url="https://ntfy.sh/t").ntfy_url
            == "https://ntfy.sh/t")


# -- Approve-by-link (Part A): web_base_url is opt-in and gates whether
# notifications ever carry an approval link -- same scheme-validation shape
# as ntfy_url above, since both feed a URL a notifier builds later.


def test_scheme_less_web_base_url_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, web_base_url="192.168.1.20:8791")


def test_empty_web_base_url_is_allowed_and_is_the_default():
    assert Settings(_env_file=None).web_base_url == ""
    assert Settings(_env_file=None, web_base_url="").web_base_url == ""


def test_http_and_https_web_base_url_are_allowed():
    assert (Settings(_env_file=None, web_base_url="http://192.168.1.20:8791").web_base_url
            == "http://192.168.1.20:8791")
    assert (Settings(_env_file=None, web_base_url="https://example.com").web_base_url
            == "https://example.com")


def test_web_base_url_trailing_slash_is_stripped():
    assert (Settings(_env_file=None, web_base_url="http://192.168.1.20:8791/").web_base_url
            == "http://192.168.1.20:8791")


# -- M4: the scheme check is case-insensitive, and "scheme, but no host" is
# rejected -- the old `.startswith(("http://", "https://"))` check let
# "http://" alone straight through, which would have built a dead link
# (f"{base}/a/{id}?k=..." -> "http:///a/1?k=...").


def test_web_base_url_scheme_check_is_case_insensitive():
    assert (Settings(_env_file=None, web_base_url="HTTPS://Example.com").web_base_url
            == "HTTPS://Example.com")
    assert (Settings(_env_file=None, web_base_url="Http://192.168.1.20:8791").web_base_url
            == "Http://192.168.1.20:8791")


def test_web_base_url_scheme_with_no_host_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, web_base_url="http://")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, web_base_url="https://")


def test_store_set_creates_and_updates_env_file(tmp_path: Path):
    env = tmp_path / ".env"
    store = SettingsStore(env)
    store.set("ALPACA_API_KEY", "k1")
    store.set("ALPACA_SECRET_KEY", "s1")
    store.set("ALPACA_API_KEY", "k2")  # update in place
    text = env.read_text()
    # Values are quoted for safety, so we check the retrieved value instead
    assert store.get("ALPACA_API_KEY") == "k2"
    assert store.get("ALPACA_SECRET_KEY") == "s1"
    assert text.count("ALPACA_API_KEY") == 1


def test_store_load_returns_settings(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    env = tmp_path / ".env"
    store = SettingsStore(env)
    store.set("ALPACA_API_KEY", "abc")
    store.set("ALPACA_PAPER", "true")
    s = store.load()
    assert s.alpaca_api_key == "abc"
    assert s.alpaca_paper is True


def test_set_preserves_values_with_spaces_hashes_and_equals(tmp_path: Path):
    store = SettingsStore(tmp_path / ".env")
    store.set("SMTP_FROM", "AllPath Trade <bot@example.com>")
    store.set("WEB_TOKEN", "abc#def=ghi jkl")
    reloaded = SettingsStore(tmp_path / ".env")
    assert reloaded.get("SMTP_FROM") == "AllPath Trade <bot@example.com>"
    assert reloaded.get("WEB_TOKEN") == "abc#def=ghi jkl"


def test_experiment_and_breaker_settings_defaults():
    s = Settings(_env_file=None)
    assert s.experiment_auto_apply_revisions is False
    assert s.drawdown_halt_pct == Decimal("0.15")
    assert s.broker_http_timeout_seconds == 30


def test_drawdown_halt_pct_range(monkeypatch):
    monkeypatch.setenv("DRAWDOWN_HALT_PCT", "1.5")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("DRAWDOWN_HALT_PCT", "0")
    assert Settings().drawdown_halt_pct == Decimal("0")


def test_options_trading_defaults_to_false():
    s = Settings(_env_file=None)
    assert s.options_trading is False


def test_options_trading_loads_from_env(tmp_path: Path):
    store = SettingsStore(tmp_path / ".env")
    store.set("OPTIONS_TRADING", "true")
    assert store.load().options_trading is True
