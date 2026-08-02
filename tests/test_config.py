from pathlib import Path

from allpath_trade.config import Settings, SettingsStore


def test_settings_defaults(tmp_path: Path):
    s = Settings(_env_file=tmp_path / "nope.env")
    assert s.alpaca_paper is True
    assert s.alpaca_api_key == ""


def test_store_set_creates_and_updates_env_file(tmp_path: Path):
    env = tmp_path / ".env"
    store = SettingsStore(env)
    store.set("ALPACA_API_KEY", "k1")
    store.set("ALPACA_SECRET_KEY", "s1")
    store.set("ALPACA_API_KEY", "k2")  # update in place
    text = env.read_text()
    assert "ALPACA_API_KEY=k2" in text
    assert "ALPACA_SECRET_KEY=s1" in text
    assert text.count("ALPACA_API_KEY") == 1
    assert store.get("ALPACA_API_KEY") == "k2"


def test_store_load_returns_settings(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    env = tmp_path / ".env"
    store = SettingsStore(env)
    store.set("ALPACA_API_KEY", "abc")
    store.set("ALPACA_PAPER", "true")
    s = store.load()
    assert s.alpaca_api_key == "abc"
    assert s.alpaca_paper is True
