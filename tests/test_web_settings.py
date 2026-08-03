from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings, SettingsStore
from allpath_trade.web.app import create_app
from tests.test_sentinel import FakeBroker


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    # Persist WEB_TOKEN to the tmp .env, not just the in-memory Settings --
    # matching real bootstrap (`ensure_token` always writes it before
    # `create_app` runs). This suite is the first to routinely call
    # `holder.rebuild()`, which reloads Settings straight from disk; without
    # the token on disk too, the very first rebuild would silently drop it
    # and lock the already-logged-in test client out of every later request.
    SettingsStore(tmp_path / ".env").set("WEB_TOKEN", "secret")
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        c.post("/login", data={"token": "secret"})
        yield c


def test_secrets_are_never_echoed_back(client, tmp_path):
    # WEB_TOKEN must stay on disk too -- this write replaces the whole file,
    # and `rebuild()` below reloads Settings straight from it, so dropping
    # the token here would lock the already-logged-in client out before the
    # real assertions ever run.
    (tmp_path / ".env").write_text(
        'ALPACA_SECRET_KEY="supersecret"\nWEB_TOKEN="secret"\n')
    client.app.state.holder.rebuild()
    body = client.get("/settings").text
    assert "supersecret" not in body
    assert "Replace" in body


def test_long_secret_is_never_echoed_back_even_partially_unmasked(client, tmp_path):
    # The mask deliberately shows a few leading/trailing characters for long
    # values (so the user can recognize which key is stored) -- but the full
    # secret must never appear in the page.
    secret = "sk-or-v1-abcdefghijklmnopqrstuvwxyz0123456789"
    (tmp_path / ".env").write_text(
        f'OPENROUTER_API_KEY="{secret}"\nWEB_TOKEN="secret"\n')
    client.app.state.holder.rebuild()
    body = client.get("/settings").text
    assert secret not in body


def test_saving_writes_env_and_rebuilds(client, tmp_path):
    r = client.post("/settings", data={
        "chat_model": "anthropic/claude-opus-5",
        "sentinel_interval_minutes": "30",
        "smtp_from": "AllPath Trade <bot@example.com>",
    }, follow_redirects=False)
    assert r.status_code == 303
    text = (tmp_path / ".env").read_text()
    assert "anthropic/claude-opus-5" in text
    assert "AllPath Trade <bot@example.com>" in text
    assert client.app.state.holder.get().settings.chat_model == "anthropic/claude-opus-5"


def test_blank_secret_field_leaves_the_stored_value_alone(client, tmp_path):
    (tmp_path / ".env").write_text(
        'OPENROUTER_API_KEY="keep-me"\nWEB_TOKEN="secret"\n')
    r = client.post("/settings", data={"openrouter_api_key": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert "keep-me" in (tmp_path / ".env").read_text()


def test_paper_mode_cannot_be_switched_from_the_page(client):
    body = client.get("/settings").text
    assert 'name="alpaca_paper"' not in body
    client.post("/settings", data={"alpaca_paper": "false"})
    assert client.app.state.holder.get().settings.alpaca_paper is True


def test_unchecked_checkboxes_are_written_as_false(client, tmp_path):
    # Seed both booleans to true, then post a form that simply omits them
    # (exactly what a browser sends when the checkboxes are unchecked). If
    # `save` only wrote fields present in the form body, these could never
    # be turned off again.
    (tmp_path / ".env").write_text(
        'DAILY_CONSOLIDATION="true"\nCONSOLIDATE_AFTER_CHAT="true"\n'
        'WEB_TOKEN="secret"\n')
    client.app.state.holder.rebuild()
    assert client.app.state.holder.get().settings.daily_consolidation is True

    client.post("/settings", data={"chat_model": "anthropic/claude-opus-5"})

    settings = client.app.state.holder.get().settings
    assert settings.daily_consolidation is False
    assert settings.consolidate_after_chat is False


def test_checked_checkboxes_are_written_as_true(client):
    client.post("/settings", data={
        "daily_consolidation": "true",
        "consolidate_after_chat": "true",
    })
    settings = client.app.state.holder.get().settings
    assert settings.daily_consolidation is True
    assert settings.consolidate_after_chat is True


def test_saving_resets_the_chat_service_so_the_next_turn_picks_up_new_config(client):
    sentinel_service = object()
    client.app.state.chat = sentinel_service
    client.post("/settings", data={"chat_model": "anthropic/claude-opus-5"})
    assert client.app.state.chat is None


def test_reset_token_invalidates_the_session(client):
    client.post("/settings/reset-token", follow_redirects=False)
    assert client.get("/", follow_redirects=False).status_code == 303


class _RecordingNotifier:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str]] = []

    def send(self, subject: str, body: str) -> bool:
        self.calls.append((subject, body))
        return self.ok


def test_test_email_button_reports_success(client):
    notifier = _RecordingNotifier(ok=True)
    client.app.state.holder.get().notifier = notifier
    r = client.post("/settings/test-email", follow_redirects=False)
    assert r.status_code == 303
    body = client.get(r.headers["location"]).text
    assert notifier.calls  # a send was actually attempted
    assert "sent" in body.lower()
    assert "fail" not in body.lower()


def test_test_email_button_reports_failure(client):
    notifier = _RecordingNotifier(ok=False)
    client.app.state.holder.get().notifier = notifier
    r = client.post("/settings/test-email", follow_redirects=False)
    assert r.status_code == 303
    body = client.get(r.headers["location"]).text
    assert notifier.calls  # a send was actually attempted
    assert "fail" in body.lower()
