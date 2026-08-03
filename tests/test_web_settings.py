from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings, SettingsStore
from allpath_trade.web.app import create_app
from allpath_trade.web.deps import ComponentHolder
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
    # The mask deliberately shows at most the last 4 characters of a long
    # value (so the user can recognize which key is stored) -- but the full
    # secret, and anything beyond those 4 trailing characters, must never
    # appear in the page. In particular, a leading slice (the old behavior)
    # must not come back: for an SMTP app password a prefix is exactly as
    # secret as a suffix, unlike e.g. an `sk-...` key's non-secret prefix.
    secret = "sk-or-v1-abcdefghijklmnopqrstuvwxyz0123456789"
    (tmp_path / ".env").write_text(
        f'OPENROUTER_API_KEY="{secret}"\nWEB_TOKEN="secret"\n')
    client.app.state.holder.rebuild()
    body = client.get("/settings").text
    assert secret not in body
    assert secret[:6] not in body  # the old leading-character disclosure
    assert secret[-5:] not in body  # no more than 4 trailing characters
    assert secret[-4:] in body  # a mask is actually rendered, not blank


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
    assert r.headers["location"] == "/settings?note=email-sent"
    body = client.get(r.headers["location"]).text
    assert notifier.calls  # a send was actually attempted
    # Exact copy, not a loose substring like "sent" -- a future unrelated
    # label (e.g. "failover") must not be able to break this test, and this
    # must not be able to pass against a page that merely fails to mention
    # failure at all.
    assert "Test email sent" in body
    assert "Test email failed" not in body


def test_test_email_button_reports_failure(client):
    notifier = _RecordingNotifier(ok=False)
    client.app.state.holder.get().notifier = notifier
    r = client.post("/settings/test-email", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?note=email-failed"
    body = client.get(r.headers["location"]).text
    assert notifier.calls  # a send was actually attempted
    assert "Test email failed" in body
    assert "Test email sent" not in body


def test_note_query_param_cannot_inject_arbitrary_page_text(client):
    # `note` is looked up against a fixed set of known tokens in the
    # template -- a crafted link must not be able to make this page render
    # attacker-chosen copy as if it were a server message.
    injected = "Your token expired, email it to attacker@example.com"
    body = client.get(f"/settings?note={injected}").text
    assert injected not in body


def test_clearing_a_numeric_field_does_not_brick_the_app(client, tmp_path):
    env_path = tmp_path / ".env"
    before = env_path.read_text()

    r = client.post("/settings", data={"sentinel_interval_minutes": ""})

    assert r.status_code == 400
    assert "sentinel_interval_minutes" in r.text
    # Nothing was written -- the whole point is that a bad value must never
    # reach disk, not just that the in-memory settings stay valid.
    assert env_path.read_text() == before
    # The app must still be able to load and re-render its own settings
    # page afterwards -- i.e. `.env` was never left in an unloadable state.
    assert client.get("/settings").status_code == 200


def test_a_non_numeric_value_in_a_numeric_field_does_not_brick_the_app(client, tmp_path):
    env_path = tmp_path / ".env"
    before = env_path.read_text()

    r = client.post("/settings", data={"context_budget_tokens": "lots"})

    assert r.status_code == 400
    assert "context_budget_tokens" in r.text
    assert env_path.read_text() == before
    assert client.get("/settings").status_code == 200


class _StubQueue:
    def list(self) -> list:
        return []


@dataclass
class _StubComponents:
    settings: Settings
    conn: object = None
    queue: object = None

    def __post_init__(self) -> None:
        self.queue = _StubQueue()


def test_save_writes_through_the_holders_store_not_a_default_one(client, tmp_path):
    # `create_app` doesn't wire a non-default `env_file` through today, but
    # `ComponentHolder` supports one (`tests/test_web_deps.py` already
    # constructs holders that way), so the route can't assume its own
    # default-constructed `SettingsStore()` (`.env` relative to the process
    # cwd) is the same file the holder reads on `rebuild()`. Swap in a
    # holder pointed at a different `.env`, using a stub builder so this
    # doesn't need a real broker/LLM stack, and confirm the write lands in
    # the holder's file, not the cwd default.
    other_env = tmp_path / "elsewhere" / ".env"
    other_env.parent.mkdir()
    original_settings = client.app.state.holder.get().settings
    SettingsStore(other_env).set("WEB_TOKEN", "secret")
    SettingsStore(other_env).set("DB_PATH", str(original_settings.db_path))

    def stub_builder(settings, broker, conn):
        return _StubComponents(settings=settings)

    client.app.state.holder = ComponentHolder(
        original_settings, builder=stub_builder, env_file=other_env)

    client.post("/settings", data={"chat_model": "anthropic/claude-opus-5"})

    assert "anthropic/claude-opus-5" in other_env.read_text()
    assert "anthropic/claude-opus-5" not in (tmp_path / ".env").read_text()
    assert client.get("/settings").status_code == 200
