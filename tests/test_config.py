from pathlib import Path

from allpath_trade.config import Settings, SettingsStore


def test_settings_defaults(tmp_path: Path):
    s = Settings(_env_file=tmp_path / "nope.env")
    assert s.alpaca_paper is True
    assert s.alpaca_api_key == ""
    assert s.context_budget_tokens == 60000


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
