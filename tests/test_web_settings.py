from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

import allpath_trade.web.routes.settings as settings_route
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


def test_ntfy_url_field_renders_and_round_trips(client, tmp_path):
    body = client.get("/settings").text
    assert 'name="ntfy_url"' in body
    assert "Install the ntfy app and subscribe to your topic" in body

    r = client.post("/settings", data={"ntfy_url": "https://ntfy.sh/my-topic"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "https://ntfy.sh/my-topic" in (tmp_path / ".env").read_text()
    assert client.app.state.holder.get().settings.ntfy_url == "https://ntfy.sh/my-topic"
    assert "https://ntfy.sh/my-topic" in client.get("/settings").text


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


def test_gmail_app_password_grouping_spaces_are_stripped_on_save(client, tmp_path):
    # Gmail shows app passwords as "abcd efgh ijkl mnop"; users paste that
    # verbatim and SMTP auth then fails with an opaque 535. The save path
    # strips the grouping when the value has exactly that shape.
    client.post("/settings", data={"smtp_password": "khei moik oppb dssr"})
    assert "'kheimoikoppbdssr'" in (tmp_path / ".env").read_text()


def test_passwords_that_merely_contain_spaces_are_stored_verbatim(client, tmp_path):
    # Only the exact 4x4-lowercase Gmail shape is rewritten -- a real
    # password containing spaces must reach .env byte-for-byte.
    client.post("/settings", data={"smtp_password": "correct horse battery staple9"})
    assert "'correct horse battery staple9'" in (tmp_path / ".env").read_text()


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


def test_save_ignores_a_stray_action_field_and_never_sends(client, tmp_path):
    # The old combined "Save and send test email" button posted
    # action=save_and_test. That branch is gone -- a stray/legacy
    # `action` field in the POST body must be silently ignored and the save
    # must behave exactly like a plain save (single redirect target, no
    # note query param).
    r = client.post("/settings", data={
        "action": "save_and_test",
        "smtp_host": "smtp.example.com",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=1"
    assert "smtp.example.com" in (tmp_path / ".env").read_text()


def test_invalid_field_does_not_discard_other_typed_fields_on_redisplay(client, tmp_path):
    # Finding 3 (final review, phase5.5-ui-polish): a validation failure on
    # one field must not throw away everything else the user typed in the
    # same submit -- the 400 branch used to re-render from the last-saved
    # Settings, discarding any unrelated, perfectly valid edit sitting in
    # the same form (e.g. a corrected smtp_host typed alongside a mistyped
    # interval).
    env_path = tmp_path / ".env"
    before = env_path.read_text()

    r = client.post("/settings", data={
        "sentinel_interval_minutes": "not-a-number",
        "smtp_host": "smtp.newhost.example.com",
    })

    assert r.status_code == 400
    assert "smtp.newhost.example.com" in r.text
    # Still refused, still nothing written -- only the redisplay changed.
    assert env_path.read_text() == before


def test_invalid_field_redisplay_does_not_leak_the_typed_invalid_value_as_a_secret(
        client, tmp_path):
    # Secrets must keep going through `masks`, never straight text, even on
    # the redisplay path -- a blank/omitted secret field in the failing POST
    # must not somehow end up rendered in the clear.
    (tmp_path / ".env").write_text(
        'OPENROUTER_API_KEY="keep-me-secret"\nWEB_TOKEN="secret"\n')
    client.app.state.holder.rebuild()

    r = client.post("/settings", data={"sentinel_interval_minutes": "not-a-number"})

    assert r.status_code == 400
    assert "keep-me-secret" not in r.text


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
    # Finding 1 of the Task 8 review: urllib.request.Request raises
    # ValueError("unknown url type") for a scheme-less URL, and the
    # settings-page hint literally says "paste the topic URL here" -- a
    # pasted-without-scheme value like "ntfy.sh/my-topic" is the expected
    # mistake, not an edge case. Must be refused here, before it ever
    # reaches .env, not discovered later as a 500 on save-and-test or a
    # silently swallowed sentinel_error.
    pytest.param("ntfy_url", "ntfy.sh/my-topic",
                 "must be empty or start with http:// or https://",
                 id="ntfy-url-missing-scheme"),
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


# -- Phase 5.5.3: per-section test buttons, replacing "Save and send test
# email". Each endpoint tests *typed* values without saving anything. --


@pytest.fixture
def anon_client(tmp_path, monkeypatch):
    # Deliberately never logs in -- unlike `client` above -- so the auth
    # middleware's own POST-guard behaviour (303 to /login, matching every
    # other authenticated POST route) is exercised for real.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    SettingsStore(tmp_path / ".env").set("WEB_TOKEN", "secret")
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        yield c


class _SpyEmailNotifier:
    instances: ClassVar[list[_SpyEmailNotifier]] = []
    ok: ClassVar[bool] = True

    def __init__(self, host, port, user, password, sender, to):
        self.host, self.port, self.user = host, port, user
        self.password, self.sender, self.to = password, sender, to
        self.sent: tuple[str, str] | None = None
        _SpyEmailNotifier.instances.append(self)

    def send(self, subject: str, body: str) -> bool:
        self.sent = (subject, body)
        return _SpyEmailNotifier.ok


class _SpyNtfyNotifier:
    instances: ClassVar[list[_SpyNtfyNotifier]] = []
    ok: ClassVar[bool] = True

    def __init__(self, url):
        self.url = url
        self.sent: tuple[str, str] | None = None
        _SpyNtfyNotifier.instances.append(self)

    def send(self, subject: str, body: str) -> bool:
        self.sent = (subject, body)
        return _SpyNtfyNotifier.ok


@pytest.fixture(autouse=True)
def _reset_notifier_spies():
    yield
    _SpyEmailNotifier.instances.clear()
    _SpyEmailNotifier.ok = True
    _SpyNtfyNotifier.instances.clear()
    _SpyNtfyNotifier.ok = True


def test_test_email_sends_typed_fields_and_falls_back_to_stored_password_when_blank(
        client, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text('SMTP_PASSWORD="stored-pw"\nWEB_TOKEN="secret"\n')
    client.app.state.holder.rebuild()
    monkeypatch.setattr(settings_route, "EmailNotifier", _SpyEmailNotifier)
    env_before = (tmp_path / ".env").read_text()

    r = client.post("/settings/test-email", data={
        "smtp_host": "smtp.typed.example.com",
        "smtp_port": "2525",
        "smtp_user": "typed-user@example.com",
        "smtp_from": "AllPath Trade <bot@example.com>",
        "notify_to": "me@example.com",
        "smtp_password": "",
    })

    assert r.status_code == 200
    [spy] = _SpyEmailNotifier.instances
    assert spy.host == "smtp.typed.example.com"
    assert spy.port == 2525
    assert spy.user == "typed-user@example.com"
    assert spy.password == "stored-pw"  # blank field falls back to the stored one
    assert spy.sent is not None  # a send was actually attempted
    assert "Test email sent" in r.text
    assert "Click Save settings below" in r.text
    # Nothing persisted -- this is a test, not a save.
    assert (tmp_path / ".env").read_text() == env_before


def test_test_email_reports_a_failure_line(client, monkeypatch):
    _SpyEmailNotifier.ok = False
    monkeypatch.setattr(settings_route, "EmailNotifier", _SpyEmailNotifier)
    r = client.post("/settings/test-email",
                     data={"smtp_host": "smtp.example.com", "notify_to": "me@example.com"})
    assert r.status_code == 200
    assert _SpyEmailNotifier.instances  # a send was actually attempted
    assert "Test email sent" not in r.text
    assert "failed" in r.text.lower()


def test_test_email_missing_host_is_an_inline_error_not_a_500(client, monkeypatch):
    monkeypatch.setattr(settings_route, "EmailNotifier", _SpyEmailNotifier)
    r = client.post("/settings/test-email", data={"smtp_host": "", "notify_to": "me@example.com"})
    assert r.status_code == 200
    assert not _SpyEmailNotifier.instances  # never attempted
    assert "SMTP host" in r.text


def test_test_email_missing_notify_to_is_an_inline_error_no_send_no_500(client, monkeypatch):
    # build_notifier() (notify/email.py) requires both smtp_host AND
    # notify_to before ever constructing an EmailNotifier -- the test path
    # used to only require smtp_host, so a blank "Send notifications to"
    # sent RCPT TO:<> and produced a generic, misleadingly-pointed SMTP
    # failure instead of naming the actual missing field.
    monkeypatch.setattr(settings_route, "EmailNotifier", _SpyEmailNotifier)
    r = client.post("/settings/test-email",
                     data={"smtp_host": "smtp.example.com", "notify_to": ""})
    assert r.status_code == 200
    assert not _SpyEmailNotifier.instances  # never attempted
    assert "Send notifications to" in r.text


def test_test_email_does_not_touch_env_or_stored_settings_even_on_success(
        client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings_route, "EmailNotifier", _SpyEmailNotifier)
    env_before = (tmp_path / ".env").read_text()
    client.post("/settings/test-email",
                 data={"smtp_host": "smtp.example.com", "notify_to": "me@example.com"})
    assert (tmp_path / ".env").read_text() == env_before
    assert client.app.state.holder.get().settings.smtp_host == ""


def test_test_email_ignores_fields_outside_its_own_section_even_if_posted(
        client, monkeypatch):
    # hx-params scopes what the *browser* sends (see the template test
    # below), but the endpoint itself is the real backstop: it must behave
    # identically -- and never read, use, or echo anything -- from fields
    # outside its own section, even if a full form post arrives anyway (a
    # non-JS client, or a future hx-params regression). Posts every other
    # settings field, including every secret, alongside the email fields.
    monkeypatch.setattr(settings_route, "EmailNotifier", _SpyEmailNotifier)
    r = client.post("/settings/test-email", data={
        "smtp_host": "smtp.example.com",
        "smtp_port": "587",
        "smtp_user": "user@example.com",
        "smtp_from": "bot@example.com",
        "notify_to": "me@example.com",
        "smtp_password": "typed-pw",
        "openrouter_api_key": "sk-or-untouched-secret",
        "openai_api_key": "sk-openai-untouched-secret",
        "anthropic_api_key": "sk-anthropic-untouched-secret",
        "alpaca_api_key": "alpaca-key-untouched-secret",
        "alpaca_secret_key": "alpaca-secret-untouched-secret",
        "ntfy_url": "https://ntfy.sh/unrelated-topic",
        "llm_provider": "anthropic",
    })
    assert r.status_code == 200
    [spy] = _SpyEmailNotifier.instances
    assert spy.host == "smtp.example.com"
    assert spy.password == "typed-pw"  # its own section's field, used normally
    for secret in ("sk-or-untouched-secret", "sk-openai-untouched-secret",
                   "sk-anthropic-untouched-secret", "alpaca-key-untouched-secret",
                   "alpaca-secret-untouched-secret", "unrelated-topic"):
        assert secret not in r.text


def test_test_push_sends_the_typed_url(client, monkeypatch):
    monkeypatch.setattr(settings_route, "NtfyNotifier", _SpyNtfyNotifier)
    r = client.post("/settings/test-push", data={"ntfy_url": "https://ntfy.sh/my-typed-topic"})
    assert r.status_code == 200
    [spy] = _SpyNtfyNotifier.instances
    assert spy.url == "https://ntfy.sh/my-typed-topic"
    assert "Test push sent" in r.text
    assert "Click Save settings below" in r.text


def test_test_push_reports_a_failure_line(client, monkeypatch):
    _SpyNtfyNotifier.ok = False
    monkeypatch.setattr(settings_route, "NtfyNotifier", _SpyNtfyNotifier)
    r = client.post("/settings/test-push", data={"ntfy_url": "https://ntfy.sh/my-topic"})
    assert r.status_code == 200
    assert _SpyNtfyNotifier.instances  # a send was actually attempted
    assert "Test push sent" not in r.text
    assert "failed" in r.text.lower()


def test_test_push_scheme_less_url_is_an_inline_error_no_send_no_500(client, monkeypatch):
    # Same http(s)-scheme rule as Settings.ntfy_url's own field validator --
    # reused, not duplicated (see Settings._ntfy_url_needs_a_scheme).
    monkeypatch.setattr(settings_route, "NtfyNotifier", _SpyNtfyNotifier)
    r = client.post("/settings/test-push", data={"ntfy_url": "ntfy.sh/my-topic"})
    assert r.status_code == 200
    assert not _SpyNtfyNotifier.instances  # never attempted
    assert "must be empty or start with http:// or https://" in r.text


def test_test_push_blank_url_is_an_inline_error_no_send(client, monkeypatch):
    monkeypatch.setattr(settings_route, "NtfyNotifier", _SpyNtfyNotifier)
    r = client.post("/settings/test-push", data={"ntfy_url": ""})
    assert r.status_code == 200
    assert not _SpyNtfyNotifier.instances


def test_test_push_does_not_touch_env_or_stored_settings(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings_route, "NtfyNotifier", _SpyNtfyNotifier)
    env_before = (tmp_path / ".env").read_text()
    client.post("/settings/test-push", data={"ntfy_url": "https://ntfy.sh/my-topic"})
    assert (tmp_path / ".env").read_text() == env_before
    assert client.app.state.holder.get().settings.ntfy_url == ""


def test_test_email_requires_auth(anon_client):
    r = anon_client.post("/settings/test-email", data={"smtp_host": "x"},
                         follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_test_push_requires_auth(anon_client):
    r = anon_client.post("/settings/test-push", data={"ntfy_url": "https://ntfy.sh/x"},
                         follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_test_email_unauthenticated_htmx_request_gets_hx_redirect_not_a_swapped_login_form(
        anon_client):
    # auth.py's guard middleware special-cases `HX-Request: true` so htmx
    # doesn't splice login.html's form into the Test button's own result
    # <div> -- covers the path the Test buttons actually take (an htmx
    # POST), not just a plain-browser POST's ordinary 303.
    r = anon_client.post("/settings/test-email", data={"smtp_host": "x"},
                         headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["HX-Redirect"] == "/login"


def test_help_toggles_present_with_key_setup_phrases(client):
    body = client.get("/settings").text
    assert "<details" in body
    # F5: each `?` disclosure toggle needs an accessible name -- a bare "?"
    # glyph means nothing to a screen reader.
    assert body.count('<summary aria-label="Setup help">?</summary>') == 2
    # Email help: Gmail app-password setup, the most-common-failure hint,
    # and the grouping-space strip save() already implements.
    assert "myaccount.google.com/apppasswords" in body
    assert "account avatar" in body
    assert "grouping spaces" in body
    assert "revokes all app passwords" in body
    assert "STARTTLS" in body
    # Push help: install/subscribe flow and the "topic is a password" warning.
    assert "ntfy app" in body


def test_ntfy_topic_privacy_warning_appears_only_once_in_the_help_toggle(client):
    # F6: the always-on field hint used to repeat the same "topic is
    # effectively a password" warning that the help toggle already states --
    # trim the hint to the short essential and keep the privacy detail only
    # in the toggle, so it isn't said twice on a page that's never scrolled.
    body = client.get("/settings").text
    assert body.count("topic name is effectively a password") == 1
    assert body.count("keep it unguessable") == 1
    assert ("Install the ntfy app and subscribe to your topic, then paste "
            "the topic URL here.") in body


def test_email_test_button_scopes_via_hx_params_allowlist_not_hx_include(client):
    # F1: for a non-GET request htmx always includes the closest enclosing
    # `<form>` -- here, the entire settings form, typed API keys included --
    # so `hx-include` adds nothing and does not scope anything. `hx-params`
    # is the real allowlist (verified directly against the vendored
    # allpath_trade/web/static/htmx.min.js: its filterValues step runs last
    # and, for a plain comma list, rebuilds the request's FormData from only
    # the named keys). Assert the button carries the real mechanism and the
    # misleading one is gone.
    body = client.get("/settings").text
    assert ('hx-params="smtp_host,smtp_port,smtp_user,smtp_password,'
            'smtp_from,notify_to"') in body
    assert "hx-include" not in body


def test_push_test_button_scopes_via_hx_params_allowlist(client):
    body = client.get("/settings").text
    assert 'hx-params="ntfy_url"' in body
    assert "topic name is effectively a password" in body
    assert "Self-hosted ntfy servers work too" in body
