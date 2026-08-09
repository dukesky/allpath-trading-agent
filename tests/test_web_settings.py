from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings, SettingsStore
from allpath_trade.web import models_catalog
from allpath_trade.web.app import create_app
from allpath_trade.web.deps import ComponentHolder
from tests.helpers import assert_english_only
from tests.test_sentinel import FakeBroker

# The catalog covers every field's default value (see config.py's
# chat_model/review_model/memory_model defaults) so that, unless a test
# deliberately stores an off-list value, none of the three selects trigger
# the "stored value not in the list" prepend path by accident.
FAKE_CATALOG = [
    "anthropic/claude-haiku-4.5",
    "anthropic/claude-opus-5",
    "anthropic/claude-sonnet-5",
    "openai/gpt-5.2",
]


@pytest.fixture(autouse=True)
def _fake_model_catalog(monkeypatch):
    # The settings page fetches the model catalog on every GET. Tests must
    # never depend on, or wait on the timeout of, the real OpenRouter API --
    # models_catalog.py already has its own test suite covering the fetch,
    # cache, and fallback behavior in isolation.
    monkeypatch.setattr(models_catalog, "list_models", lambda provider: list(FAKE_CATALOG))


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


def test_a_short_secret_falls_through_to_the_fixed_width_mask(client, tmp_path):
    # A1: the old guard (`len(value) > 4`) unmasked the last 4 characters of
    # a 5-character secret -- 4 of 5 characters on screen, which is not
    # meaningfully different from showing it outright, and contradicted the
    # comment's own claim that the tail is only shown "when the value is
    # long enough that doing so doesn't reveal the whole thing." An 8-char
    # secret (exactly at the new threshold) must still fall through to the
    # plain dot mask -- none of it may leak.
    (tmp_path / ".env").write_text(
        'SMTP_PASSWORD="eightchr"\nWEB_TOKEN="secret"\n')
    client.app.state.holder.rebuild()
    body = client.get("/settings").text
    assert "eightchr" not in body
    assert "chr" not in body  # no trailing slice of it either
    assert "•" * 8 in body  # the fixed-width mask is still rendered


def test_settings_page_is_english_only(client, tmp_path):
    (tmp_path / ".env").write_text(
        'OPENROUTER_API_KEY="sk-or-v1-abcdefghijklmnop"\nWEB_TOKEN="secret"\n')
    client.app.state.holder.rebuild()
    assert_english_only(client.get("/settings").text)


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


def test_model_fields_render_as_selects_with_the_stored_value_selected(client):
    body = client.get("/settings").text
    assert '<select name="chat_model"' in body
    assert '<select name="review_model"' in body
    assert '<select name="memory_model"' in body
    # Settings() defaults (config.py) are all members of FAKE_CATALOG, so
    # each should come back pre-selected rather than defaulting to the
    # first catalog entry or nothing at all.
    assert '<option value="anthropic/claude-sonnet-5" selected>' in body  # chat_model
    assert '<option value="anthropic/claude-haiku-4.5" selected>' in body  # review_model
    assert '<option value="anthropic/claude-opus-5" selected>' in body  # memory_model


def test_a_stored_off_catalog_model_is_prepended_not_silently_swapped(client, tmp_path):
    (tmp_path / ".env").write_text(
        'CHAT_MODEL="custom-provider/exotic-model"\nWEB_TOKEN="secret"\n')
    client.app.state.holder.rebuild()
    body = client.get("/settings").text
    # The off-list value must still be on the page, selected, as itself --
    # not silently replaced by the first (or any) catalog entry.
    assert '<option value="custom-provider/exotic-model" selected>' in body
    assert 'custom-provider/exotic-model' in body


def test_model_select_offers_a_custom_option_for_arbitrary_slugs(client):
    body = client.get("/settings").text
    assert '__custom__' in body
    assert 'Custom' in body


def test_saving_an_off_catalog_custom_model_value_persists(client, tmp_path):
    # The <select>'s "Custom..." option reveals a plain text input via a
    # small inline script, but the posted field name is unchanged either
    # way -- the save handler (and its validation pipeline) must not need
    # to know or care whether the value came from the dropdown or the
    # custom text field.
    r = client.post("/settings", data={"chat_model": "totally/custom-slug"},
                     follow_redirects=False)
    assert r.status_code == 303
    assert client.app.state.holder.get().settings.chat_model == "totally/custom-slug"
    assert "totally/custom-slug" in (tmp_path / ".env").read_text()


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


class _RecordingScheduler:
    def __init__(self):
        self.rescheduled = None

    def reschedule_job(self, job_id, trigger=None, **kwargs):
        self.rescheduled = (job_id, trigger, kwargs)

    def shutdown(self, wait=True):
        # create_app's lifespan (allpath_trade/web/app.py) calls this on
        # teardown for whatever's on app.state.scheduler -- a fixture-set
        # fake needs one too, or the TestClient context manager's own
        # shutdown blows up on exit for tests that install this fake.
        pass


def test_changing_the_sentinel_interval_reschedules_the_running_job(client):
    # Finding 5: without this wire-up, `sentinel_interval_minutes` reads back
    # correctly everywhere but the actual cadence never moves until restart,
    # while `context_budget_tokens` (right next to it on the same card) takes
    # effect immediately -- an inconsistency the user has no way to notice
    # from the UI alone.
    scheduler = _RecordingScheduler()
    client.app.state.scheduler = scheduler
    r = client.post("/settings", data={"sentinel_interval_minutes": "15"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert scheduler.rescheduled == ("sentinel_pass", "interval", {"minutes": 15})


def test_saving_without_changing_the_interval_does_not_reschedule(client):
    scheduler = _RecordingScheduler()
    client.app.state.scheduler = scheduler
    current = client.app.state.holder.get().settings.sentinel_interval_minutes
    client.post("/settings", data={"sentinel_interval_minutes": str(current)})
    assert scheduler.rescheduled is None


def test_saving_with_no_scheduler_running_does_not_crash(client):
    # No `serve` process behind this test client -- app.state.scheduler is
    # simply absent. A settings save must still succeed.
    assert getattr(client.app.state, "scheduler", None) is None
    r = client.post("/settings", data={"sentinel_interval_minutes": "20"},
                    follow_redirects=False)
    assert r.status_code == 303


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


def _install_notifier_spy(monkeypatch, ok: bool) -> _RecordingNotifier:
    # A successful `action=save_and_test` always calls `holder.rebuild()`
    # before sending, and `rebuild()` throws away the old `Components` --
    # including whatever notifier a test had assigned directly onto it --
    # and builds a brand new one via `build_components` (allpath_trade/app.py),
    # which always calls `build_notifier(settings)` for the real thing. To
    # inject a spy that survives that rebuild, patch `build_notifier` itself
    # rather than an instance attribute that rebuild would discard.
    import allpath_trade.app as app_module

    spy = _RecordingNotifier(ok)
    monkeypatch.setattr(app_module, "build_notifier", lambda settings: spy)
    return spy


def test_save_and_test_persists_changes_and_sends_exactly_once(client, tmp_path, monkeypatch):
    # The old design posted the test button to a separate form, which
    # reloaded the page from stored settings and discarded whatever the user
    # had just typed into the main form. `action=save_and_test` must run the
    # *same* save path as a normal submit -- persisting first -- and then
    # send exactly one test notification against the just-saved config.
    notifier = _install_notifier_spy(monkeypatch, ok=True)
    r = client.post("/settings", data={
        "action": "save_and_test",
        "smtp_host": "smtp.example.com",
        "smtp_from": "AllPath Trade <bot@example.com>",
    }, follow_redirects=False)
    assert r.status_code == 303
    text = (tmp_path / ".env").read_text()
    assert "smtp.example.com" in text
    assert client.app.state.holder.get().settings.smtp_host == "smtp.example.com"
    assert len(notifier.calls) == 1


def test_save_and_test_reports_success(client, monkeypatch):
    notifier = _install_notifier_spy(monkeypatch, ok=True)
    r = client.post("/settings", data={"action": "save_and_test"}, follow_redirects=False)
    assert r.status_code == 303
    body = client.get(r.headers["location"]).text
    assert notifier.calls  # a send was actually attempted
    # Exact copy, not a loose substring like "sent" -- a future unrelated
    # label (e.g. "failover") must not be able to break this test, and this
    # must not be able to pass against a page that merely fails to mention
    # failure at all.
    assert "Test email sent" in body
    assert "Test email failed" not in body


def test_save_and_test_reports_failure(client, monkeypatch):
    notifier = _install_notifier_spy(monkeypatch, ok=False)
    r = client.post("/settings", data={"action": "save_and_test"}, follow_redirects=False)
    assert r.status_code == 303
    body = client.get(r.headers["location"]).text
    assert notifier.calls  # a send was actually attempted
    assert "Test email failed" in body
    assert "Test email sent" not in body


def test_save_and_test_with_invalid_field_sends_nothing_and_returns_400(
        client, tmp_path, monkeypatch):
    # A validation failure with action=save_and_test must behave exactly
    # like a failed normal save: nothing written, nothing sent, 400 back.
    # No rebuild happens on this path, so a plain instance assignment (rather
    # than the rebuild-surviving `_install_notifier_spy`) is enough here.
    notifier = _RecordingNotifier(ok=True)
    client.app.state.holder.get().notifier = notifier
    env_path = tmp_path / ".env"
    before = env_path.read_text()

    r = client.post("/settings", data={
        "action": "save_and_test",
        "sentinel_interval_minutes": "not-a-number",
    })

    assert r.status_code == 400
    assert env_path.read_text() == before
    assert notifier.calls == []


def test_test_email_route_is_gone(client):
    assert client.post("/settings/test-email", follow_redirects=False).status_code == 404


def test_note_query_param_cannot_inject_arbitrary_page_text(client):
    # `note` is looked up against a fixed set of known tokens in the
    # template -- a crafted link must not be able to make this page render
    # attacker-chosen copy as if it were a server message.
    injected = "Your token expired, email it to attacker@example.com"
    body = client.get(f"/settings?note={injected}").text
    assert injected not in body


@pytest.mark.parametrize("field,bad_value,expected_msg", [
    # Blank / non-numeric text in a plain HTML input -- type-invalid, not
    # merely out of range. `field in r.text` alone would also pass against
    # a 400 that named the field but said nothing about *why* it failed
    # (or, before Finding 4, a version that let these straight through) --
    # asserting the pydantic error copy itself is the only way this test
    # can actually distinguish "refused, and here's why" from "accepted".
    pytest.param("sentinel_interval_minutes", "",
                 "unable to parse string as an integer",
                 id="clearing-a-numeric-field"),
    pytest.param("context_budget_tokens", "lots",
                 "unable to parse string as an integer",
                 id="non-numeric-text"),
    # Finding 4 regressions: type-valid but semantically absurd. A negative
    # sentinel interval schedules a perpetually-overdue APScheduler job that
    # hammers the broker in a hot loop; a zero context budget fires a
    # memory-tier summarization call on every single turn.
    pytest.param("sentinel_interval_minutes", "-5",
                 "Input should be greater than or equal to 1",
                 id="negative-sentinel-interval"),
    pytest.param("context_budget_tokens", "0",
                 "Input should be greater than or equal to 2000",
                 id="zero-context-budget"),
    # B1: the class of bug, not just the two fields Finding 4 happened to
    # fix -- smtp_port is the third numeric field on this same form and had
    # no web-route-level coverage at all (only a Settings()-level check in
    # test_config.py, which never proves the *route* refuses it before
    # writing .env).
    pytest.param("smtp_port", "0",
                 "Input should be greater than or equal to 1",
                 id="smtp-port-below-range"),
    pytest.param("smtp_port", "70000",
                 "Input should be less than or equal to 65535",
                 id="smtp-port-above-range"),
])
def test_a_type_valid_but_absurd_numeric_value_is_refused_and_env_unchanged(
        client, tmp_path, field, bad_value, expected_msg):
    env_path = tmp_path / ".env"
    before = env_path.read_text()

    r = client.post("/settings", data={field: bad_value})

    assert r.status_code == 400
    assert expected_msg in r.text
    # Nothing was written -- the whole point is that a bad value must never
    # reach disk, not just that the in-memory settings stay valid.
    assert env_path.read_text() == before
    # The app must still be able to load and re-render its own settings
    # page afterwards -- i.e. `.env` was never left in an unloadable state.
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
