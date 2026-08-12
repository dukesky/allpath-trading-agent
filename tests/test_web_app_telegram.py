"""Tests for Task 5 of the Telegram plan: serve wiring in `web/app.py`.

Two things live here:

  1. `_start_telegram`/`_stop_telegram` -- the lifespan wiring that builds a
     `TelegramAPI` + poller and starts a daemon thread only when
     `settings.telegram_bot_token` is non-empty, and cleanly stops it on
     shutdown. `telegram_poller_cls` (mirrors `scheduler_cls`'s existing
     shape in this module) lets tests inject a spy instead of a real poller
     that would otherwise block on a live long-poll against
     api.telegram.org.
  2. `_mirror_to_telegram` -- the direction policy for spec §④'s web->
     Telegram mirroring. Tested directly (not through a live turn) against
     fake `TelegramAPI`/`AppState` objects, with `_MIRROR_EXECUTOR` swapped
     for a synchronous stand-in so assertions don't race a background
     thread.
"""

from __future__ import annotations

import threading
from typing import ClassVar

from fastapi.testclient import TestClient

import allpath_trade.web.app as app_module
from allpath_trade.config import Settings
from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, AppState
from allpath_trade.store.db import connect
from allpath_trade.web.app import _mirror_to_telegram, create_app
from tests.test_sentinel import FakeBroker


def _settings(tmp_path, **overrides):
    (tmp_path / "strategies").mkdir(exist_ok=True)
    return Settings(_env_file=None, db_path=tmp_path / "t.db",
                    strategies_dir=tmp_path / "strategies",
                    memory_dir=tmp_path / "memory", **overrides)


# --- lifespan wiring: no token / token present ------------------------------

class SpyPoller:
    """Stands in for `TelegramPoller`. `run_forever` blocks on the real
    `stop` Event (same shape the real one blocks on during backoff via
    `self.stop.wait(delay)`) so `_stop_telegram`'s `join(timeout=2)` has
    something real to prove: the thread actually exits once `stop` is set,
    not just that `stop.set()` was called."""

    instances: ClassVar[list[SpyPoller]] = []

    def __init__(self, api, chat_service, app_state, web_token, stop):
        self.api = api
        self.chat_service = chat_service
        self.app_state = app_state
        self.web_token = web_token
        self.stop = stop
        self.started = threading.Event()
        SpyPoller.instances.append(self)

    def run_forever(self):
        self.started.set()
        self.stop.wait()


def test_no_token_starts_no_poller_thread(tmp_path):
    SpyPoller.instances = []
    settings = _settings(tmp_path)  # telegram_bot_token="" by default
    app = create_app(settings, broker=FakeBroker(), telegram_poller_cls=SpyPoller)

    with TestClient(app):
        pass

    assert SpyPoller.instances == []
    assert not hasattr(app.state, "telegram_thread")


def test_no_token_leaves_no_mirror_registered(tmp_path):
    # Zero overhead means zero behavior change too -- ChatService.send with
    # no mirror registered is a documented no-op (test_chat_mirror.py).
    settings = _settings(tmp_path)
    app = create_app(settings, broker=FakeBroker(), telegram_poller_cls=SpyPoller)

    with TestClient(app):
        assert app.state.chat_service._mirror is None


def test_token_starts_a_daemon_thread_running_the_poller(tmp_path):
    SpyPoller.instances = []
    settings = _settings(tmp_path, telegram_bot_token="123:ABC")
    app = create_app(settings, broker=FakeBroker(), telegram_poller_cls=SpyPoller)

    with TestClient(app):
        assert len(SpyPoller.instances) == 1
        poller = SpyPoller.instances[0]
        assert poller.started.wait(timeout=2)
        assert poller.web_token == settings.web_token
        assert app.state.telegram_thread.daemon is True
        assert app.state.telegram_thread.is_alive()
        assert not poller.stop.is_set()


def test_shutdown_sets_the_stop_event_and_joins_the_thread(tmp_path):
    SpyPoller.instances = []
    settings = _settings(tmp_path, telegram_bot_token="123:ABC")
    app = create_app(settings, broker=FakeBroker(), telegram_poller_cls=SpyPoller)

    with TestClient(app):
        poller = SpyPoller.instances[0]
        assert poller.started.wait(timeout=2)

    # Lifespan shutdown already ran by the time the `with` block above exits.
    assert poller.stop.is_set()
    assert not app.state.telegram_thread.is_alive()


def test_token_registers_the_mirror_hook(tmp_path):
    SpyPoller.instances = []
    settings = _settings(tmp_path, telegram_bot_token="123:ABC")
    app = create_app(settings, broker=FakeBroker(), telegram_poller_cls=SpyPoller)

    with TestClient(app):
        assert app.state.chat_service._mirror is not None


def test_poller_shares_the_same_app_state_the_chat_service_uses(tmp_path):
    # Both must read/write the same pairing/offset rows -- a second AppState
    # object pointed at a different connection would silently split state.
    SpyPoller.instances = []
    settings = _settings(tmp_path, telegram_bot_token="123:ABC")
    app = create_app(settings, broker=FakeBroker(), telegram_poller_cls=SpyPoller)

    with TestClient(app):
        poller = SpyPoller.instances[0]
        assert poller.app_state is app.state.holder.get().app_state


# --- mirror direction policy -------------------------------------------------

class ImmediateExecutor:
    """Stands in for `_MIRROR_EXECUTOR`: runs the submitted call inline
    instead of on a background thread, so a test can assert on its effect
    without racing a real ThreadPoolExecutor worker."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class FakeMirrorAPI:
    """Stands in for `TelegramAPI`. `send_message` records every call
    unless `fail` is set, in which case it raises -- proving
    `_send_mirror_text`'s try/except actually swallows a failure rather
    than one that (like the real `TelegramAPI.send_message`) merely returns
    `False`."""

    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, str]] = []
        self.fail = fail
        self.token = "secret-token-value"

    def send_message(self, chat_id: str, text: str) -> bool:
        if self.fail:
            raise RuntimeError(f"boom, token={self.token}")
        self.sent.append((chat_id, text))
        return True

    def _scrub(self, text: str) -> str:
        return text.replace(self.token, "***")


def _paired_app_state(tmp_path, chat_id: str = "555") -> AppState:
    app_state = AppState(connect(tmp_path / "t.db"))
    app_state.set(TELEGRAM_CHAT_ID_KEY, chat_id)
    return app_state


def test_web_source_with_a_reply_sends_two_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "_MIRROR_EXECUTOR", ImmediateExecutor())
    api = FakeMirrorAPI()
    app_state = _paired_app_state(tmp_path)

    _mirror_to_telegram("web", "hello", "hi there", api=api, app_state=app_state)

    assert api.sent == [("555", "You (web): hello"), ("555", "hi there")]


def test_web_source_with_an_empty_reply_sends_only_the_note_line(tmp_path, monkeypatch):
    # note_resolution's shape: `text` is already the full record line, and
    # `reply` is "" -- must NOT get the "You (web): " prefix, and must not
    # send a second, empty message.
    monkeypatch.setattr(app_module, "_MIRROR_EXECUTOR", ImmediateExecutor())
    api = FakeMirrorAPI()
    app_state = _paired_app_state(tmp_path)

    _mirror_to_telegram("web", "You resolved #1. Result: order submitted", "",
                        api=api, app_state=app_state)

    assert api.sent == [("555", "You resolved #1. Result: order submitted")]


def test_telegram_source_is_a_no_op(tmp_path, monkeypatch):
    # The poller already replied in-channel -- mirroring it back would be
    # an echo loop.
    monkeypatch.setattr(app_module, "_MIRROR_EXECUTOR", ImmediateExecutor())
    api = FakeMirrorAPI()
    app_state = _paired_app_state(tmp_path)

    _mirror_to_telegram("telegram", "hello", "reply", api=api, app_state=app_state)

    assert api.sent == []


def test_unpaired_chat_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "_MIRROR_EXECUTOR", ImmediateExecutor())
    api = FakeMirrorAPI()
    app_state = AppState(connect(tmp_path / "t.db"))  # never paired

    _mirror_to_telegram("web", "hello", "reply", api=api, app_state=app_state)

    assert api.sent == []


def test_send_failure_is_swallowed_and_logged_scrubbed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(app_module, "_MIRROR_EXECUTOR", ImmediateExecutor())
    api = FakeMirrorAPI(fail=True)
    app_state = _paired_app_state(tmp_path)

    _mirror_to_telegram("web", "hello", "reply", api=api, app_state=app_state)  # must not raise

    err = capsys.readouterr().err
    assert "[telegram] mirror send failed" in err
    assert api.token not in err  # scrubbed, same discipline as the poller/transport


def test_paired_chat_id_is_re_read_on_every_call_not_captured_once(tmp_path, monkeypatch):
    # Pairing can change mid-process (re-pair, Unpair on the settings page)
    # -- a captured chat id from registration time would leak to a stale
    # chat or silently drop a message to a freshly (re-)paired one.
    monkeypatch.setattr(app_module, "_MIRROR_EXECUTOR", ImmediateExecutor())
    api = FakeMirrorAPI()
    app_state = AppState(connect(tmp_path / "t.db"))
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")

    _mirror_to_telegram("web", "first", "", api=api, app_state=app_state)

    app_state.set(TELEGRAM_CHAT_ID_KEY, "222")
    _mirror_to_telegram("web", "second", "", api=api, app_state=app_state)

    assert [chat_id for chat_id, _ in api.sent] == ["111", "222"]
