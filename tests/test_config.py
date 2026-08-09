from pathlib import Path

import pytest
from pydantic import ValidationError

from allpath_trade.config import Settings, SettingsStore


def test_settings_defaults(tmp_path: Path):
    s = Settings(_env_file=tmp_path / "nope.env")
    assert s.alpaca_paper is True
    assert s.alpaca_api_key == ""
    assert s.context_budget_tokens == 60000


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
