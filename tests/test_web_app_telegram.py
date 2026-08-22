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
     fake `TelegramAPI`/`AppState` objects, with an `ImmediateExecutor`
     passed as `mirror_queue=` so assertions don't race a background
     thread.
  3. `_MirrorQueue` itself -- Finding 3's bounded, drop-oldest,
     cleanly-shut-down replacement for the old module-level
     `ThreadPoolExecutor` singleton.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from functools import partial
from typing import ClassVar

from fastapi.testclient import TestClient

import allpath_trade.web.app as app_module
from allpath_trade.config import Settings
from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, AppState
from allpath_trade.store.db import connect
from allpath_trade.web.app import _mirror_to_telegram, create_app
from tests.test_agent_loop import ScriptedLLM
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
    not just that `stop.set()` was called.

    Constructor shape matches the real `TelegramPoller`'s post-Finding-2/5
    signature: `holder` (not a separate `app_state`/`web_token` snapshot),
    read at the point of use via `holder.get()` -- see the real class's
    docstring."""

    instances: ClassVar[list[SpyPoller]] = []

    def __init__(self, api, chat_service, holder, stop):
        self.api = api
        self.chat_service = chat_service
        self.holder = holder
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
        assert poller.holder.get().settings.web_token == settings.web_token
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


def test_token_registers_the_mirror_hook_on_every_account_chat_service(tmp_path):
    # shadow-dual-active T5: a shadow web-chat turn must mirror into
    # Telegram too (labelled [Shadow]) -- not just paper's, which is all
    # the pre-T5 single-ChatService design ever wired up.
    SpyPoller.instances = []
    settings = _settings(tmp_path, telegram_bot_token="123:ABC")
    app = create_app(settings, broker=FakeBroker(), telegram_poller_cls=SpyPoller)

    with TestClient(app):
        assert set(app.state.chat_services) == {"paper", "shadow"}
        for service in app.state.chat_services.values():
            assert service._mirror is not None


def test_poller_shares_the_same_app_state_the_chat_service_uses(tmp_path):
    # Both must read/write the same pairing/offset rows -- a second AppState
    # object pointed at a different connection would silently split state.
    SpyPoller.instances = []
    settings = _settings(tmp_path, telegram_bot_token="123:ABC")
    app = create_app(settings, broker=FakeBroker(), telegram_poller_cls=SpyPoller)

    with TestClient(app):
        poller = SpyPoller.instances[0]
        assert poller.holder.get().app_state is app.state.holder.get().app_state


def test_poller_is_handed_the_real_holder_not_a_snapshot(tmp_path):
    # Finding 2/5: the poller must be handed the SAME ComponentHolder the
    # rest of the app uses, not a one-time snapshot of its current
    # settings/app_state -- otherwise a token reset or a db_path-changing
    # rebuild() would never reach it.
    SpyPoller.instances = []
    settings = _settings(tmp_path, telegram_bot_token="123:ABC")
    app = create_app(settings, broker=FakeBroker(), telegram_poller_cls=SpyPoller)

    with TestClient(app):
        poller = SpyPoller.instances[0]
        assert poller.holder is app.state.holder


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


def test_web_source_with_a_reply_sends_two_messages(tmp_path):
    # shadow-dual-active T5: every mirrored line now carries an account
    # prefix -- `account` defaults to "paper" here (the caller passed
    # none), matching `_start_telegram`'s partial-binding for the paper
    # ChatService (see test_account_prefix_reflects_which_chat_service_
    # mirrored below for the shadow case).
    api = FakeMirrorAPI()
    app_state = _paired_app_state(tmp_path)

    _mirror_to_telegram("web", "hello", "hi there", api=api, app_state=app_state, mirror_queue=ImmediateExecutor())

    assert api.sent == [("555", "[Paper] You (web): hello"), ("555", "[Paper] hi there")]


def test_account_prefix_reflects_which_chat_service_mirrored(tmp_path):
    # The shadow-account ChatService's mirror partial is bound with
    # account="shadow" (see _start_telegram) -- proven directly here against
    # the same _mirror_to_telegram function, without needing a full app.
    api = FakeMirrorAPI()
    app_state = _paired_app_state(tmp_path)

    _mirror_to_telegram("web", "buy some AAPL", "noted", api=api, app_state=app_state,
                        mirror_queue=ImmediateExecutor(), account="shadow")

    assert api.sent == [("555", "[Shadow] You (web): buy some AAPL"),
                        ("555", "[Shadow] noted")]


def test_web_source_with_an_empty_reply_sends_only_the_note_line(tmp_path):
    # note_resolution's shape: `text` is already the full record line, and
    # `reply` is "" -- must NOT get the "You (web): " prefix, and must not
    # send a second, empty message.
    api = FakeMirrorAPI()
    app_state = _paired_app_state(tmp_path)

    _mirror_to_telegram("web", "You resolved #1. Result: order submitted", "",
                        api=api, app_state=app_state, mirror_queue=ImmediateExecutor())

    assert api.sent == [("555", "[Paper] You resolved #1. Result: order submitted")]


def test_telegram_source_is_a_no_op(tmp_path):
    # The poller already replied in-channel -- mirroring it back would be
    # an echo loop.
    api = FakeMirrorAPI()
    app_state = _paired_app_state(tmp_path)

    _mirror_to_telegram("telegram", "hello", "reply", api=api, app_state=app_state, mirror_queue=ImmediateExecutor())

    assert api.sent == []


def test_unpaired_chat_sends_nothing(tmp_path):
    api = FakeMirrorAPI()
    app_state = AppState(connect(tmp_path / "t.db"))  # never paired

    _mirror_to_telegram("web", "hello", "reply", api=api, app_state=app_state, mirror_queue=ImmediateExecutor())

    assert api.sent == []


def test_send_failure_is_swallowed_and_logged_scrubbed(tmp_path, capsys):
    api = FakeMirrorAPI(fail=True)
    app_state = _paired_app_state(tmp_path)

    _mirror_to_telegram("web", "hello", "reply", api=api, app_state=app_state, mirror_queue=ImmediateExecutor())  # must not raise

    err = capsys.readouterr().err
    assert "[telegram] mirror send failed" in err
    assert api.token not in err  # scrubbed, same discipline as the poller/transport


def test_paired_chat_id_is_re_read_on_every_call_not_captured_once(tmp_path):
    # Pairing can change mid-process (re-pair, Unpair on the settings page)
    # -- a captured chat id from registration time would leak to a stale
    # chat or silently drop a message to a freshly (re-)paired one.
    api = FakeMirrorAPI()
    app_state = AppState(connect(tmp_path / "t.db"))
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")

    _mirror_to_telegram("web", "first", "", api=api, app_state=app_state, mirror_queue=ImmediateExecutor())

    app_state.set(TELEGRAM_CHAT_ID_KEY, "222")
    _mirror_to_telegram("web", "second", "", api=api, app_state=app_state, mirror_queue=ImmediateExecutor())

    assert [chat_id for chat_id, _ in api.sent] == ["111", "222"]


# ---------------------------------------------------------------------------
# Important 1: a Telegram button-resolve must not produce a SECOND, mirrored
# message back into the same chat. `ChatService.note_resolution` now takes a
# `source` kwarg (default "web", unchanged for the web reviews flow) that it
# threads straight into the mirror hook -- these two tests exercise a REAL
# ChatService (not the FakeChatService used elsewhere in this suite) wired
# to the real `_mirror_to_telegram` direction policy, proving the fix at the
# only layer where the bug actually lived: `note_resolution` hard-coding
# source="web" regardless of who called it.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _real_chat_service(tmp_path, monkeypatch):
    """Builds a real `ChatService` (via `create_app`, same as the actual
    server wiring) with a `ScriptedLLM` swapped in for `build_llm` so
    `session()`/`_build()` succeed without a real provider key. Yields
    `(chat_service, app_state)` paired to chat id "555" -- `note_resolution`
    never turns the LLM, so an empty response script is fine."""
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=(tmp_path / "strategies"),
                        memory_dir=tmp_path / "memory", web_token="secret",
                        openrouter_api_key="k")
    (tmp_path / "strategies").mkdir(exist_ok=True)
    llm = ScriptedLLM([])
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: llm)
    app = create_app(settings, broker=FakeBroker(), telegram_poller_cls=SpyPoller)
    with TestClient(app):
        chat_service = app.state.chat_service
        app_state = app.state.holder.get().app_state
        app_state.set(TELEGRAM_CHAT_ID_KEY, "555")
        yield chat_service, app_state


def test_telegram_sourced_resolution_lands_in_conversation_but_mirror_is_not_called(
        tmp_path, monkeypatch):
    with _real_chat_service(tmp_path, monkeypatch) as (chat_service, app_state):
        fake_api = FakeMirrorAPI()
        chat_service.set_mirror(
            partial(_mirror_to_telegram, api=fake_api, app_state=app_state,
                   mirror_queue=ImmediateExecutor()))

        chat_service.note_resolution(
            "You resolved #1. Result: order submitted", source="telegram")

        # The conversation still gets the receipt row (the shared record) --
        # only the redundant Telegram push is skipped.
        assert chat_service.messages()[-1]["display"] == (
            "You resolved #1. Result: order submitted")
        assert fake_api.sent == []


def test_web_sourced_resolution_still_mirrors_to_telegram_unchanged(
        tmp_path, monkeypatch):
    with _real_chat_service(tmp_path, monkeypatch) as (chat_service, app_state):
        fake_api = FakeMirrorAPI()
        chat_service.set_mirror(
            partial(_mirror_to_telegram, api=fake_api, app_state=app_state,
                   mirror_queue=ImmediateExecutor()))

        chat_service.note_resolution("You resolved #2. Result: rejected")  # default source="web"

        assert chat_service.messages()[-1]["display"] == "You resolved #2. Result: rejected"
        assert fake_api.sent == [("555", "[Paper] You resolved #2. Result: rejected")]


# ---------------------------------------------------------------------------
# Whole-branch review Finding 3: the mirror queue must be bounded
# (drop-oldest under backlog, never block the caller) and must actually be
# shut down -- including the mirror hook itself -- on `_stop_telegram`,
# instead of the old module-level `ThreadPoolExecutor` singleton that
# `_stop_telegram` never touched at all.
# ---------------------------------------------------------------------------

def test_mirror_queue_full_drops_oldest_rather_than_blocking():
    # Worker thread never started -- nothing drains the queue -- so this
    # proves `submit` itself never blocks and drops the OLDEST entry to
    # make room, not the newest.
    q = app_module._MirrorQueue(maxsize=2)
    sink: list[str] = []
    q.submit(sink.append, "first")
    q.submit(sink.append, "second")
    q.submit(sink.append, "third")  # queue was full -- "first" must be dropped

    remaining = []
    while True:
        try:
            _fn, args = q._queue.get_nowait()
        except queue.Empty:
            break
        remaining.append(args[0])
    assert remaining == ["second", "third"]


def test_mirror_queue_shutdown_cancels_pending_sends():
    q = app_module._MirrorQueue(maxsize=10)
    sink: list[str] = []
    q.submit(sink.append, "queued-but-never-run")

    q.shutdown(wait=False, cancel_futures=True)

    assert q._queue.empty()
    assert sink == []


def test_mirror_queue_processes_submitted_work_in_order():
    q = app_module._MirrorQueue(maxsize=10)
    q.start()
    sink: list[str] = []
    q.submit(sink.append, "one")
    q.submit(sink.append, "two")

    for _ in range(50):
        if sink == ["one", "two"]:
            break
        time.sleep(0.05)
    assert sink == ["one", "two"]

    q.shutdown(wait=True, cancel_futures=True)


def test_stop_telegram_shuts_down_the_mirror_queue_and_clears_the_hook(tmp_path):
    SpyPoller.instances = []
    settings = _settings(tmp_path, telegram_bot_token="123:ABC")
    app = create_app(settings, broker=FakeBroker(), telegram_poller_cls=SpyPoller)

    with TestClient(app):
        mirror_queue = app.state.mirror_queue
        assert app.state.chat_service._mirror is not None

    # Lifespan shutdown already ran by the time the `with` block exits.
    assert mirror_queue._stop.is_set()
    assert app.state.chat_service._mirror is None
    # shadow-dual-active T5: shadow's mirror hook must be cleared too, not
    # just paper's -- both were registered on startup.
    assert app.state.chat_services["shadow"]._mirror is None
