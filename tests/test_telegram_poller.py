"""Tests for allpath_trade/telegram.py's poller half (`TelegramPoller`).

Never touches api.telegram.org or Telegram's SDK -- `api` is a fake object
implementing only `get_updates`/`send_message`/`send_typing`/`delete_message`
(the same shape tests/test_telegram_api.py already proves against the real
transport), and `chat_service` is a fake too: `ChatService.send(text,
source=...)` lands in Task 4 (see plan's cross-task note), so every test
here drives the poller against a stand-in that just records calls.
`app_state` is the real `AppState` on a tmp-path sqlite DB -- the
offset/pairing persistence guarantees are worth proving against the real
store, not a dict.

A handful of tests near the bottom wire the REAL `TelegramAPI` to a real
`TelegramPoller` against an always-failing/always-replaying fake `urlopen`
-- regression coverage for the Critical backoff fix, which is specifically
about the real transport's `None`-vs-`[]` contract, not something the
FakeTelegramAPI stand-in (which never modeled that distinction pre-fix)
could catch on its own.
"""

from __future__ import annotations

import threading
import time
import urllib.error
from types import SimpleNamespace

from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.store.app_state import (
    TELEGRAM_CHAT_ID_KEY,
    TELEGRAM_OFFSET_KEY,
    TELEGRAM_USER_ID_KEY,
    AppState,
)
from allpath_trade.store.db import connect
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.telegram import TelegramAPI, TelegramPoller
from tests.test_sentinel import SpyExecutor

WEB_TOKEN = "correct-horse-battery-staple"


def make_app_state(tmp_path):
    return AppState(connect(tmp_path / "t.db"))


class FakeHolder:
    """Minimal stand-in for `web.deps.ComponentHolder`: `.get()` returns a
    fresh snapshot exposing `.app_state`/`.settings.web_token`, mirroring
    the real `Components` shape `TelegramPoller` now reads through (see its
    class docstring's Finding 2/5 notes). `set_web_token` lets a test
    simulate a live `/settings/reset-token` save without reconstructing the
    poller -- this is the Finding 2 regression coverage: the SAME poller
    instance must reject the old token and accept the new one the moment
    the holder starts returning it, no restart."""

    def __init__(self, app_state: AppState, web_token: str, queue=None, strategies=None) -> None:
        self._app_state = app_state
        self._web_token = web_token
        self._queue = queue
        self._strategies = strategies

    def get(self) -> SimpleNamespace:
        return SimpleNamespace(app_state=self._app_state,
                               settings=SimpleNamespace(web_token=self._web_token),
                               queue=self._queue, strategies=self._strategies)

    def set_web_token(self, token: str) -> None:
        self._web_token = token


def pair(app_state: AppState, chat_id, user_id=None) -> None:
    """Test helper: writes the (chat_id, user_id) pairing state directly,
    bypassing the `/start` flow, for tests that only care about
    paired-chat message handling. `user_id` defaults to `chat_id` -- true
    for a real Telegram private chat, where the sender IS the chat in a
    1:1 DM."""
    chat_id = str(chat_id)
    app_state.set(TELEGRAM_CHAT_ID_KEY, chat_id)
    app_state.set(TELEGRAM_USER_ID_KEY, str(user_id) if user_id is not None else chat_id)


class FakeTelegramAPI:
    """Stands in for TelegramAPI. `batches` is a list of update-lists, one
    consumed per `get_updates` call; once exhausted (or if never given)
    returns `[]`, matching the real transport's "nothing new this poll"
    contract. A batch entry of `None` simulates `get_updates` itself
    failing (network/HTTP error, malformed response, ...) -- the contract
    Task-2's fix established for the real `TelegramAPI.get_updates`.

    Carries a fake `token`/`_scrub` pair (mirroring the real
    `TelegramAPI`'s) because `TelegramPoller._log_error` scrubs through
    `self.api._scrub` unconditionally -- any test that triggers a poller
    stderr line needs this to exist."""

    def __init__(self, batches=None, on_get_updates=None, token="fake-bot-token"):
        self._batches = list(batches or [])
        self._on_get_updates = on_get_updates
        self.token = token
        self.get_updates_offsets: list[int] = []
        self.sent_messages: list[tuple[str, str]] = []
        self.sent_markups: list[dict | None] = []
        self.typing_calls: list[str] = []
        self.deleted_messages: list[tuple[str, int]] = []
        self.answered_callbacks: list[tuple[str, str]] = []
        self.edited_markups: list[tuple[str, int, dict | None]] = []

    def get_updates(self, offset: int, timeout_s: int = 50):
        self.get_updates_offsets.append(offset)
        if self._on_get_updates is not None:
            self._on_get_updates()
        if self._batches:
            return self._batches.pop(0)
        return []

    def send_message(self, chat_id: str, html: str, reply_markup=None) -> bool:
        self.sent_messages.append((chat_id, html))
        self.sent_markups.append(reply_markup)
        return True

    def send_typing(self, chat_id: str) -> None:
        self.typing_calls.append(chat_id)

    def delete_message(self, chat_id: str, message_id: int) -> None:
        self.deleted_messages.append((chat_id, message_id))

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        self.answered_callbacks.append((callback_query_id, text))

    def edit_message_reply_markup(self, chat_id: str, message_id: int, reply_markup=None) -> None:
        self.edited_markups.append((chat_id, message_id, reply_markup))

    def _scrub(self, text: str) -> str:
        if not self.token:
            return text
        return text.replace(self.token, "***")


class FakeChatService:
    """Stands in for ChatService -- `send(text, source=...)` is Task 4's
    signature; this fake accepts it so Task 3 doesn't need to wait on that
    task landing (per the plan's cross-task note)."""

    def __init__(self, reply="agent reply", log=None):
        self.reply = reply
        self.calls: list[dict] = []
        self.resolutions: list[str] = []
        self._log = log

    def send(self, text: str, source: str = "web") -> str:
        self.calls.append({"text": text, "source": source})
        if self._log is not None:
            self._log.append(("chat_service.send", text))
        return self.reply

    def note_resolution(self, line: str) -> None:
        self.resolutions.append(line)


class LoggingAppState:
    """Thin wrapper around a real `AppState` that appends to a shared
    `log` list whenever the offset key is written -- lets a test assert
    ordering (offset persisted before chat_service is invoked) without
    faking storage itself away."""

    def __init__(self, app_state: AppState, log: list) -> None:
        self._app_state = app_state
        self._log = log

    def get(self, key: str):
        return self._app_state.get(key)

    def set(self, key: str, value: str) -> None:
        if key == TELEGRAM_OFFSET_KEY:
            self._log.append(("app_state.set", key))
        self._app_state.set(key, value)


def make_poller(api, chat_service, app_state, web_token=WEB_TOKEN):
    return TelegramPoller(api, chat_service, FakeHolder(app_state, web_token), threading.Event())


def _update(update_id, chat_id, text, from_id=None, chat_type="private"):
    """Builds a minimal `message` update. `from_id` defaults to `chat_id`
    (true for a real Telegram private chat: the sender IS the chat in a
    1:1 DM). `message_id` is set to `update_id` -- Telegram's own
    message_id isn't the same sequence as update_id, but nothing here
    cares beyond "some int"."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": chat_id if from_id is None else from_id},
            "text": text,
        },
    }


# ---------------------------------------------------------------------------
# Pairing matrix
# ---------------------------------------------------------------------------

def test_start_with_correct_token_pairs_and_replies(tmp_path):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[_update(1, 111, f"/start {WEB_TOKEN}")]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    assert app_state.get(TELEGRAM_CHAT_ID_KEY) == "111"
    assert app_state.get(TELEGRAM_USER_ID_KEY) == "111"
    assert len(api.sent_messages) == 1
    chat_id, reply = api.sent_messages[0]
    assert chat_id == "111"
    assert reply.startswith("Paired. This chat now talks to your AllPath agent.")
    assert "delete your /start message" in reply
    # Best-effort cleanup of the token-bearing /start message.
    assert api.deleted_messages == [("111", 1)]


def test_start_with_wrong_token_does_not_pair_and_never_replies(tmp_path):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[_update(1, 111, "/start wrong-token")]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    assert app_state.get(TELEGRAM_CHAT_ID_KEY) is None
    assert app_state.get(TELEGRAM_USER_ID_KEY) is None
    assert api.sent_messages == []
    assert api.typing_calls == []
    assert api.deleted_messages == []


def test_start_with_missing_token_does_not_pair_and_never_replies(tmp_path):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[_update(1, 111, "/start")]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    assert app_state.get(TELEGRAM_CHAT_ID_KEY) is None
    assert api.sent_messages == []


def test_stranger_message_is_silently_dropped_no_reply(tmp_path, capsys):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[_update(1, 999, "hello bot")]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    assert api.sent_messages == []
    assert api.typing_calls == []
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "dropped" in err[0]


def test_stranger_messages_produce_at_most_one_stderr_line_per_batch(tmp_path, capsys):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[
        _update(1, 999, "hi"), _update(2, 998, "hi again"), _update(3, 997, "hi once more"),
    ]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1


def test_repairing_overwrites_old_chat_id(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    api = FakeTelegramAPI(batches=[[_update(1, 222, f"/start {WEB_TOKEN}")]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    assert app_state.get(TELEGRAM_CHAT_ID_KEY) == "222"
    assert app_state.get(TELEGRAM_USER_ID_KEY) == "222"


def test_after_repair_the_old_chat_id_becomes_a_stranger(tmp_path, capsys):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    api = FakeTelegramAPI(batches=[[_update(1, 222, f"/start {WEB_TOKEN}")]])
    poller = make_poller(api, FakeChatService(), app_state)
    poller.poll_once()
    assert app_state.get(TELEGRAM_CHAT_ID_KEY) == "222"

    api2 = FakeTelegramAPI(batches=[[_update(2, 111, "hello from the old chat")]])
    poller2 = make_poller(api2, FakeChatService(), app_state)
    poller2.poll_once()

    assert api2.sent_messages == []
    err = capsys.readouterr().err.strip().splitlines()
    assert any("dropped" in line for line in err)


# ---------------------------------------------------------------------------
# Group pairing / from.id belt-and-suspenders (Important 2)
# ---------------------------------------------------------------------------

def test_start_from_supergroup_with_valid_token_does_not_pair_no_reply(tmp_path):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[
        _update(1, -1001234567890, f"/start {WEB_TOKEN}", chat_type="supergroup"),
    ]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    assert app_state.get(TELEGRAM_CHAT_ID_KEY) is None
    assert app_state.get(TELEGRAM_USER_ID_KEY) is None
    assert api.sent_messages == []
    assert api.deleted_messages == []


def test_start_from_group_is_counted_in_the_batch_dropped_line(tmp_path, capsys):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[
        _update(1, -100999, f"/start {WEB_TOKEN}", chat_type="group"),
    ]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "dropped" in err[0]


def test_paired_chat_message_from_different_user_id_is_dropped(tmp_path, capsys):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111", user_id="999")  # the person who actually paired
    api = FakeTelegramAPI(batches=[[_update(1, 111, "hi", from_id=555)]])
    chat_service = FakeChatService()
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert chat_service.calls == []
    assert api.sent_messages == []
    err = capsys.readouterr().err.strip().splitlines()
    assert any("dropped" in line for line in err)


def test_paired_chat_message_with_matching_chat_and_user_id_is_handled(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    api = FakeTelegramAPI(batches=[[_update(1, 111, "hi")]])  # from_id defaults to 111
    chat_service = FakeChatService(reply="hi back")
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert chat_service.calls == [{"text": "hi", "source": "telegram"}]


# ---------------------------------------------------------------------------
# /start exact-match, not prefix (Important 3)
# ---------------------------------------------------------------------------

def test_starting_prefix_is_not_treated_as_start_command(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    api = FakeTelegramAPI(batches=[[_update(1, 111, "/starting a position in AAPL")]])
    chat_service = FakeChatService(reply="ok")
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert chat_service.calls == [{"text": "/starting a position in AAPL", "source": "telegram"}]
    assert app_state.get(TELEGRAM_CHAT_ID_KEY) == "111"  # unchanged, no re-pairing attempted


def test_exact_start_command_still_pairs(tmp_path):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[_update(1, 111, f"/start {WEB_TOKEN}")]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    assert app_state.get(TELEGRAM_CHAT_ID_KEY) == "111"


def test_start_with_bot_username_suffix_still_pairs(tmp_path):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[_update(1, 111, f"/start@allpath_bot {WEB_TOKEN}")]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    assert app_state.get(TELEGRAM_CHAT_ID_KEY) == "111"


# ---------------------------------------------------------------------------
# compare_digest on non-ASCII input (Important 1)
# ---------------------------------------------------------------------------

def test_non_ascii_stranger_token_is_silently_rejected_single_batch_line(tmp_path, capsys):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[_update(1, 111, "/start café-wrong-token")]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()  # must not raise (pre-fix: hmac.compare_digest(str, str) on
    # non-ASCII input raises TypeError, which would surface as a generic
    # "failed to process update" line instead of a clean "dropped" one)

    assert app_state.get(TELEGRAM_CHAT_ID_KEY) is None
    assert api.sent_messages == []
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "dropped" in err[0]


def test_non_ascii_web_token_with_correct_token_pairs(tmp_path):
    app_state = make_app_state(tmp_path)
    non_ascii_token = "café-token-中文"
    api = FakeTelegramAPI(batches=[[_update(1, 111, f"/start {non_ascii_token}")]])
    poller = make_poller(api, FakeChatService(), app_state, web_token=non_ascii_token)

    poller.poll_once()

    assert app_state.get(TELEGRAM_CHAT_ID_KEY) == "111"


# ---------------------------------------------------------------------------
# Pairing secret hygiene (Important 5)
# ---------------------------------------------------------------------------

def test_failed_pairing_attempt_and_stranger_message_share_one_stderr_line(tmp_path, capsys):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[
        _update(1, 111, "/start wrong-token"),
        _update(2, 222, "hi there"),
    ]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "dropped 2" in err[0]


# ---------------------------------------------------------------------------
# Offset persistence -- at-most-once
# ---------------------------------------------------------------------------

def test_offset_persisted_immediately_and_survives_restart(tmp_path):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[_update(5, 111, "hi")]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    assert app_state.get(TELEGRAM_OFFSET_KEY) == "6"

    # A fresh poller (simulating a restart) must resume after the offset,
    # not replay update_id=5.
    api2 = FakeTelegramAPI(batches=[[]])
    poller2 = make_poller(api2, FakeChatService(), app_state)
    poller2.poll_once()
    assert api2.get_updates_offsets == [6]


def test_offset_persisted_before_chat_service_is_invoked_for_paired_chat(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    log: list = []
    logging_app_state = LoggingAppState(app_state, log)
    api = FakeTelegramAPI(batches=[[_update(9, 111, "buy some AAPL")]])
    chat_service = FakeChatService(log=log)
    poller = make_poller(api, chat_service, logging_app_state)

    poller.poll_once()

    assert log == [("app_state.set", TELEGRAM_OFFSET_KEY), ("chat_service.send", "buy some AAPL")]


def test_offset_advances_even_when_the_update_crashes_processing(tmp_path):
    # At-most-once: a mid-turn crash must drop the message (offset already
    # advanced) rather than replay it on the next poll.
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")

    class RaisingChatService:
        def send(self, text, source="web"):
            raise RuntimeError("boom mid-turn")

    api = FakeTelegramAPI(batches=[[_update(7, 111, "hi")]])
    poller = make_poller(api, RaisingChatService(), app_state)

    status = poller.poll_once()  # must not raise

    assert app_state.get(TELEGRAM_OFFSET_KEY) == "8"
    # The offset DID advance, so the batch as a whole counts as "ok" for
    # backoff purposes even though this one update's handling crashed.
    assert status == "ok"


# ---------------------------------------------------------------------------
# Paired chat text -> typing, chat_service, formatted/split reply
# ---------------------------------------------------------------------------

def test_paired_text_sends_typing_then_calls_chat_service_with_telegram_source(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    api = FakeTelegramAPI(batches=[[_update(1, 111, "what's my position")]])
    chat_service = FakeChatService(reply="you own 10 AAPL")
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert api.typing_calls == ["111"]
    assert chat_service.calls == [{"text": "what's my position", "source": "telegram"}]
    assert api.sent_messages == [("111", "you own 10 AAPL")]


def test_reply_is_formatted_through_to_telegram_html(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    api = FakeTelegramAPI(batches=[[_update(1, 111, "hi")]])
    chat_service = FakeChatService(reply="**bold** reply")
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert api.sent_messages == [("111", "<b>bold</b> reply")]


def test_long_reply_is_split_into_multiple_send_message_calls(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    api = FakeTelegramAPI(batches=[[_update(1, 111, "hi")]])
    paragraph = "x" * 3000
    chat_service = FakeChatService(reply=f"{paragraph}\n\n{paragraph}\n\n{paragraph}")
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert len(api.sent_messages) > 1
    assert all(chat_id == "111" for chat_id, _ in api.sent_messages)
    assert all(len(html) <= 4096 for _, html in api.sent_messages)


def test_empty_agent_reply_sends_fallback_chunk(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    api = FakeTelegramAPI(batches=[[_update(1, 111, "hi")]])
    chat_service = FakeChatService(reply="")
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert api.sent_messages == [("111", "(empty reply)")]


# ---------------------------------------------------------------------------
# Non-text / malformed updates
# ---------------------------------------------------------------------------

def test_non_text_message_photo_is_ignored_gracefully(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    update = {"update_id": 1, "message": {"chat": {"id": 111}, "photo": [{"file_id": "abc"}]}}
    api = FakeTelegramAPI(batches=[[update]])
    chat_service = FakeChatService()
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert chat_service.calls == []
    assert api.sent_messages == []
    assert app_state.get(TELEGRAM_OFFSET_KEY) == "2"  # offset still advances


def test_edited_message_update_is_ignored(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    update = {"update_id": 1, "edited_message": {"chat": {"id": 111}, "text": "oops typo fixed"}}
    api = FakeTelegramAPI(batches=[[update]])
    chat_service = FakeChatService()
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert chat_service.calls == []
    assert api.sent_messages == []


def test_callback_query_update_is_ignored(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    update = {"update_id": 1, "callback_query": {"id": "cb1", "from": {"id": 111}, "data": "x"}}
    api = FakeTelegramAPI(batches=[[update]])
    chat_service = FakeChatService()
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert chat_service.calls == []
    assert api.sent_messages == []


# ---------------------------------------------------------------------------
# One bad update never kills the batch
# ---------------------------------------------------------------------------

def test_one_malformed_update_does_not_stop_the_rest_of_the_batch(tmp_path, capsys):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    api = FakeTelegramAPI(batches=[[
        "not-even-a-dict",
        _update(2, 111, "hello for real"),
    ]])
    chat_service = FakeChatService(reply="hi back")
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()  # must not raise

    assert chat_service.calls == [{"text": "hello for real", "source": "telegram"}]
    assert api.sent_messages == [("111", "hi back")]
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) >= 1


def test_update_missing_message_and_no_recognized_field_is_ignored(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    api = FakeTelegramAPI(batches=[[{"update_id": 1}]])
    chat_service = FakeChatService()
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert chat_service.calls == []
    assert app_state.get(TELEGRAM_OFFSET_KEY) == "2"


# ---------------------------------------------------------------------------
# Minor: stderr scrub + truncate
# ---------------------------------------------------------------------------

def test_stderr_lines_from_update_processing_are_scrubbed_and_truncated(tmp_path, capsys):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    api = FakeTelegramAPI(batches=[[_update(1, 111, "hi")]])

    class RaisingChatService:
        def send(self, text, source="web"):
            # Simulates a chat_service exception embedding arbitrary
            # user-controlled text (and, worst case, the bot token) --
            # must never leak unbounded/unscrubbed into stderr.
            raise RuntimeError(api.token + " " + ("x" * 500))

    poller = make_poller(api, RaisingChatService(), app_state)

    poller.poll_once()

    err = capsys.readouterr().err.strip()
    assert api.token not in err
    assert "***" in err
    assert len(err) <= len("[telegram] ") + 200


# ---------------------------------------------------------------------------
# poll_once status ("ok"/"failed") -- Critical fix
# ---------------------------------------------------------------------------

def test_poll_once_returns_ok_for_a_genuinely_empty_long_poll(tmp_path):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[]])
    poller = make_poller(api, FakeChatService(), app_state)

    assert poller.poll_once() == "ok"


def test_poll_once_returns_failed_when_get_updates_signals_transport_failure(tmp_path):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[None])  # simulates get_updates() -> None
    poller = make_poller(api, FakeChatService(), app_state)

    assert poller.poll_once() == "failed"


def test_poll_once_returns_ok_when_at_least_one_update_in_the_batch_advances_offset(tmp_path):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[
        {"update_id": "bad", "message": {"chat": {"id": 1, "type": "private"},
                                          "from": {"id": 1}, "text": "hi"}},
        _update(2, 111, "hi"),
    ]])
    poller = make_poller(api, FakeChatService(), app_state)

    assert poller.poll_once() == "ok"


def test_poll_once_returns_failed_when_batch_present_but_no_update_advances_offset(tmp_path):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[
        {"update_id": "bad", "message": {"chat": {"id": 1, "type": "private"},
                                          "from": {"id": 1}, "text": "hi"}},
    ]])
    poller = make_poller(api, FakeChatService(), app_state)

    assert poller.poll_once() == "failed"


# ---------------------------------------------------------------------------
# run_forever: backoff sequence
# ---------------------------------------------------------------------------

def test_run_forever_backs_off_on_repeated_failure_and_resets_on_success(tmp_path, monkeypatch):
    app_state = make_app_state(tmp_path)
    waits: list[float] = []
    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: waits.append(timeout))

    stop = threading.Event()
    call_count = {"n": 0}

    def flaky_get_updates():
        call_count["n"] += 1
        if call_count["n"] <= 3:
            raise RuntimeError("network is down")
        if call_count["n"] == 4:
            return  # succeeds this poll
        stop.set()  # stop after the 5th call so the loop terminates

    api = FakeTelegramAPI(on_get_updates=flaky_get_updates)
    poller = TelegramPoller(api, FakeChatService(), FakeHolder(app_state, WEB_TOKEN), stop)

    poller.run_forever()

    # 3 failures -> 5s, 10s, 20s backoff; 4th call succeeds (no wait after
    # success); 5th call sets stop and the loop exits without waiting again.
    assert waits == [5, 10, 20]


def test_run_forever_caps_backoff_at_60_seconds(tmp_path, monkeypatch):
    app_state = make_app_state(tmp_path)
    waits: list[float] = []
    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: waits.append(timeout))

    stop = threading.Event()
    call_count = {"n": 0}

    def fails_five_times_then_succeeds_and_stops():
        call_count["n"] += 1
        if call_count["n"] <= 5:
            raise RuntimeError("still down")
        stop.set()  # 6th call succeeds -- no wait for it, loop exits next check

    api = FakeTelegramAPI(on_get_updates=fails_five_times_then_succeeds_and_stops)
    poller = TelegramPoller(api, FakeChatService(), FakeHolder(app_state, WEB_TOKEN), stop)

    poller.run_forever()

    # 5, 10, 20, 40, then capped at 60 for the 5th failure.
    assert waits == [5, 10, 20, 40, 60]


def test_run_forever_does_not_poll_at_all_once_stop_is_already_set(tmp_path):
    app_state = make_app_state(tmp_path)
    stop = threading.Event()
    stop.set()
    api = FakeTelegramAPI()
    poller = TelegramPoller(api, FakeChatService(), FakeHolder(app_state, WEB_TOKEN), stop)

    poller.run_forever()

    assert api.get_updates_offsets == []


def test_stop_during_backoff_does_not_wait_out_the_full_delay(tmp_path):
    """Minor fix: the backoff sleep is `self.stop.wait(delay)`, not
    `time.sleep(delay)` -- a shutdown requested mid-backoff (up to 60s)
    must wake the loop immediately rather than blocking for the rest of
    the delay. Proven here with a REAL `threading.Event` (no monkeypatch)
    and wall-clock timing: the base backoff is 5s, so if this were still
    `time.sleep` the test would take ~5s; instead it should return within
    a fraction of a second of the timer firing."""
    app_state = make_app_state(tmp_path)
    stop = threading.Event()

    def always_fails():
        raise RuntimeError("network is down")

    api = FakeTelegramAPI(on_get_updates=always_fails)
    poller = TelegramPoller(api, FakeChatService(), FakeHolder(app_state, WEB_TOKEN), stop)

    timer = threading.Timer(0.05, stop.set)
    timer.start()
    start = time.monotonic()
    poller.run_forever()
    elapsed = time.monotonic() - start
    timer.cancel()

    assert elapsed < 2.0


# ---------------------------------------------------------------------------
# Critical fix regression: real TelegramAPI against a hostile/offline
# transport must back off, never hot-loop.
# ---------------------------------------------------------------------------

def test_real_telegram_api_offline_transport_backs_off_instead_of_hot_looping(tmp_path):
    """End-to-end regression for the Critical fix: wire a REAL
    `TelegramAPI` (not the FakeTelegramAPI stand-in above) to a REAL
    `TelegramPoller` and run `run_forever` against a transport that always
    fails -- exactly the "network is down" scenario the bug report
    measured at 121k polls/sec with zero sleeps. Runs against a real
    `threading.Event` (no monkeypatch) for a short, real wall-clock window
    and counts how many times `urlopen` was actually hit: a backing-off
    loop makes at most a couple of calls in that window; the pre-fix
    hot-loop would make thousands."""
    call_count = {"n": 0}

    def always_offline_urlopen(req, timeout=None):
        call_count["n"] += 1
        raise urllib.error.URLError("network is down")

    api = TelegramAPI("fake-bot-token", urlopen=always_offline_urlopen)
    app_state = make_app_state(tmp_path)
    stop = threading.Event()
    poller = TelegramPoller(api, FakeChatService(), FakeHolder(app_state, WEB_TOKEN), stop)

    def stop_soon():
        time.sleep(0.2)
        stop.set()

    stopper = threading.Thread(target=stop_soon)
    stopper.start()
    poller.run_forever()
    stopper.join()

    assert 1 <= call_count["n"] < 5


def test_batch_with_no_offset_advance_backs_off_instead_of_replaying_hot(tmp_path):
    """Regression for the other half of the Critical fix: `get_updates`
    itself succeeds (returns a batch), but every update in it fails to
    advance the offset -- the disk-full/string-update_id replay scenario.
    Without the `advanced_any` check in `poll_once`, this exact batch would
    be re-fetched and re-processed identically forever with zero backoff,
    since the offset never moves."""

    class ReplayingAPI:
        """Always returns the identical non-advancing batch -- the offset
        never moves, so a REAL TelegramAPI/get_updates(offset) would keep
        re-delivering the exact same update forever."""

        def __init__(self):
            self.calls = 0
            self.token = "fake-bot-token"

        def get_updates(self, offset, timeout_s=50):
            self.calls += 1
            return [{
                "update_id": "not-an-int",
                "message": {
                    "chat": {"id": 111, "type": "private"},
                    "from": {"id": 111},
                    "text": "hi",
                },
            }]

        def send_message(self, chat_id, html):
            return True

        def send_typing(self, chat_id):
            pass

        def delete_message(self, chat_id, message_id):
            pass

        def _scrub(self, text):
            return text.replace(self.token, "***") if self.token else text

    app_state = make_app_state(tmp_path)
    stop = threading.Event()
    api = ReplayingAPI()
    poller = TelegramPoller(api, FakeChatService(), FakeHolder(app_state, WEB_TOKEN), stop)

    def stop_soon():
        time.sleep(0.2)
        stop.set()

    stopper = threading.Thread(target=stop_soon)
    stopper.start()
    poller.run_forever()
    stopper.join()

    assert 1 <= api.calls < 5


# ---------------------------------------------------------------------------
# Whole-branch review Finding 2 (security) / Finding 5: the poller must read
# web_token/app_state live through `holder.get()`, never from a snapshot
# taken once at construction time.
# ---------------------------------------------------------------------------

def test_web_token_reset_revokes_the_old_token_and_accepts_the_new_one(tmp_path):
    # Same poller instance throughout -- no reconstruction -- proving the
    # token is read at compare time, not captured once in __init__.
    app_state = make_app_state(tmp_path)
    holder = FakeHolder(app_state, WEB_TOKEN)
    api = FakeTelegramAPI(batches=[[_update(1, 111, f"/start {WEB_TOKEN}")]])
    poller = TelegramPoller(api, FakeChatService(), holder, threading.Event())

    poller.poll_once()
    assert app_state.get(TELEGRAM_CHAT_ID_KEY) == "111"

    # Simulate /settings/reset-token: the holder now serves a new token.
    new_token = "brand-new-token-after-reset"
    holder.set_web_token(new_token)

    # A second chat tries to pair with the OLD (now-revoked) token.
    api2 = FakeTelegramAPI(batches=[[_update(2, 222, f"/start {WEB_TOKEN}")]])
    poller2 = TelegramPoller(api2, FakeChatService(), holder, threading.Event())
    poller2.poll_once()
    assert app_state.get(TELEGRAM_CHAT_ID_KEY) == "111"  # unchanged: old token rejected
    assert api2.sent_messages == []

    # The NEW token pairs immediately, no restart, same holder.
    api3 = FakeTelegramAPI(batches=[[_update(3, 333, f"/start {new_token}")]])
    poller3 = TelegramPoller(api3, FakeChatService(), holder, threading.Event())
    poller3.poll_once()
    assert app_state.get(TELEGRAM_CHAT_ID_KEY) == "333"


def test_app_state_is_read_live_through_the_holder_not_captured_once(tmp_path):
    # Finding 5: a db_path change (ComponentHolder.rebuild()) swaps the
    # AppState object the holder returns -- the poller must pick up the new
    # one, not keep writing to a stale/closed connection forever.
    first_app_state = make_app_state(tmp_path)
    holder = FakeHolder(first_app_state, WEB_TOKEN)
    poller = TelegramPoller(FakeTelegramAPI(), FakeChatService(), holder, threading.Event())
    assert poller.app_state is first_app_state

    second_app_state = make_app_state(tmp_path)
    holder._app_state = second_app_state
    assert poller.app_state is second_app_state


# ---------------------------------------------------------------------------
# Whole-branch review Finding 6 (minor): the typing indicator is re-sent
# while a long chat_service.send() call is still running, not just once at
# the start of the turn.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Approve/Reject inline-button callbacks
# ---------------------------------------------------------------------------

class FakeStrategies:
    """Stands in for `Components.strategies` -- `_resolve_review_callback`
    only ever calls `.rearm_warning(strategy_id)` on a successful revision
    approval."""

    def rearm_warning(self, strategy_id: str) -> str:
        return ""


def make_order_queue(tmp_path, *, fail=False, raise_exc=None, reject_reasons=None):
    conn = connect(tmp_path / "reviews.db")
    executor = SpyExecutor(fail=fail, raise_exc=raise_exc, reject_reasons=reject_reasons)
    return ReviewQueue(conn, executor)


def queue_order_review(queue: ReviewQueue, *, source="chat") -> int:
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty="10", reason="test")
    handle = queue.add(strategy_id="", rule_id="", ticker="AAPL", rule_type="chat",
                       condition="proposed in conversation", action="buy 10 AAPL",
                       snapshot={}, intent=intent, source=source)
    return int(handle)


def nonce_for(queue: ReviewQueue, review_id: int) -> str:
    return queue.get(review_id)["approval_token_hash"][:16]


def _callback_update(update_id, chat_id, from_id, data, message_id=1):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": from_id},
            "message": {"chat": {"id": chat_id}, "message_id": message_id},
            "data": data,
        },
    }


def make_review_poller(api, chat_service, app_state, queue, strategies=None,
                       web_token=WEB_TOKEN):
    holder = FakeHolder(app_state, web_token, queue=queue,
                        strategies=strategies or FakeStrategies())
    return TelegramPoller(api, chat_service, holder, threading.Event())


def test_paired_approve_callback_approves_order_and_removes_buttons(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    queue = make_order_queue(tmp_path)
    review_id = queue_order_review(queue)
    nonce = nonce_for(queue, review_id)
    api = FakeTelegramAPI(batches=[[
        _callback_update(1, 111, 111, f"rv:approve:{review_id}:{nonce}", message_id=42)]])
    chat_service = FakeChatService()
    poller = make_review_poller(api, chat_service, app_state, queue)

    poller.poll_once()

    assert queue.get(review_id)["status"] == "approved"
    assert api.edited_markups == [("111", 42, None)]
    assert api.answered_callbacks == [("cb1", "Approved")]
    assert any("order submitted" in html for _cid, html in api.sent_messages)
    assert chat_service.resolutions == [
        f"You resolved #{review_id}. Result: order submitted"]


def test_paired_reject_callback_rejects_order(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    queue = make_order_queue(tmp_path)
    review_id = queue_order_review(queue)
    nonce = nonce_for(queue, review_id)
    api = FakeTelegramAPI(batches=[[
        _callback_update(1, 111, 111, f"rv:reject:{review_id}:{nonce}")]])
    chat_service = FakeChatService()
    poller = make_review_poller(api, chat_service, app_state, queue)

    poller.poll_once()

    row = queue.get(review_id)
    assert row["status"] == "rejected"
    assert row["resolution_note"] == "rejected via Telegram"
    assert any("Rejected" in html for _cid, html in api.sent_messages)
    assert chat_service.resolutions == [
        f"You resolved #{review_id}. Result: rejected (rejected via Telegram)"]


def test_stranger_callback_is_silently_dropped(tmp_path, capsys):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111", user_id="999")
    queue = make_order_queue(tmp_path)
    review_id = queue_order_review(queue)
    nonce = nonce_for(queue, review_id)
    api = FakeTelegramAPI(batches=[[
        _callback_update(1, 111, 555, f"rv:approve:{review_id}:{nonce}")]])
    chat_service = FakeChatService()
    poller = make_review_poller(api, chat_service, app_state, queue)

    poller.poll_once()

    assert queue.get(review_id)["status"] == "pending"
    assert api.sent_messages == []
    assert api.answered_callbacks == []
    assert api.edited_markups == []
    err = capsys.readouterr().err.strip().splitlines()
    assert any("dropped" in line for line in err)


def test_unpaired_chat_callback_is_silently_dropped(tmp_path):
    app_state = make_app_state(tmp_path)  # nobody paired
    queue = make_order_queue(tmp_path)
    review_id = queue_order_review(queue)
    nonce = nonce_for(queue, review_id)
    api = FakeTelegramAPI(batches=[[
        _callback_update(1, 111, 111, f"rv:approve:{review_id}:{nonce}")]])
    poller = make_review_poller(api, FakeChatService(), app_state, queue)

    poller.poll_once()

    assert queue.get(review_id)["status"] == "pending"
    assert api.sent_messages == []


def test_wrong_nonce_is_ignored_review_stays_pending(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    queue = make_order_queue(tmp_path)
    review_id = queue_order_review(queue)
    api = FakeTelegramAPI(batches=[[
        _callback_update(1, 111, 111, f"rv:approve:{review_id}:0000000000000000")]])
    chat_service = FakeChatService()
    poller = make_review_poller(api, chat_service, app_state, queue)

    poller.poll_once()

    assert queue.get(review_id)["status"] == "pending"
    assert api.answered_callbacks == [("cb1", "Already resolved")]
    # No buttons removed, no chat receipt -- nothing was actually resolved.
    assert api.edited_markups == []
    assert chat_service.resolutions == []


def test_non_pending_review_callback_is_ignored(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    queue = make_order_queue(tmp_path)
    review_id = queue_order_review(queue)
    nonce = nonce_for(queue, review_id)
    queue.reject(review_id, note="already handled elsewhere")
    api = FakeTelegramAPI(batches=[[
        _callback_update(1, 111, 111, f"rv:approve:{review_id}:{nonce}")]])
    chat_service = FakeChatService()
    poller = make_review_poller(api, chat_service, app_state, queue)

    poller.poll_once()

    assert queue.get(review_id)["status"] == "rejected"  # unchanged
    assert api.answered_callbacks == [("cb1", "Already resolved")]
    assert chat_service.resolutions == []


def test_execution_error_gives_honest_message(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    queue = make_order_queue(tmp_path, fail=True)
    review_id = queue_order_review(queue)
    nonce = nonce_for(queue, review_id)
    api = FakeTelegramAPI(batches=[[
        _callback_update(1, 111, 111, f"rv:approve:{review_id}:{nonce}")]])
    chat_service = FakeChatService()
    poller = make_review_poller(api, chat_service, app_state, queue)

    poller.poll_once()

    assert queue.get(review_id)["status"] == "approved"  # claimed before the failure
    assert api.answered_callbacks == [("cb1", "Execution failed")]
    assert any("execution failed" in html for _cid, html in api.sent_messages)
    assert chat_service.resolutions == [
        f"You resolved #{review_id}. Result: execution failed: boom"]


def test_risk_gate_rejected_gives_honest_message(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    queue = make_order_queue(tmp_path, reject_reasons=["exceeds max position size"])
    review_id = queue_order_review(queue)
    nonce = nonce_for(queue, review_id)
    api = FakeTelegramAPI(batches=[[
        _callback_update(1, 111, 111, f"rv:approve:{review_id}:{nonce}")]])
    chat_service = FakeChatService()
    poller = make_review_poller(api, chat_service, app_state, queue)

    poller.poll_once()

    assert queue.get(review_id)["status"] == "approved"
    assert any("risk gate" in html for _cid, html in api.sent_messages)
    expected = (f"You resolved #{review_id}. Result: blocked by the risk "
               "gate (exceeds max position size)")
    assert chat_service.resolutions == [expected]


def test_sentinel_sourced_review_does_not_echo_into_chat(tmp_path):
    # Only chat-sourced reviews have a live conversation to report back into
    # -- mirrors web/routes/reviews.py's own `_echo_resolution` gate.
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    queue = make_order_queue(tmp_path)
    review_id = queue_order_review(queue, source="sentinel")
    nonce = nonce_for(queue, review_id)
    api = FakeTelegramAPI(batches=[[
        _callback_update(1, 111, 111, f"rv:approve:{review_id}:{nonce}")]])
    chat_service = FakeChatService()
    poller = make_review_poller(api, chat_service, app_state, queue)

    poller.poll_once()

    assert queue.get(review_id)["status"] == "approved"
    assert chat_service.resolutions == []


def test_callback_offset_advances(tmp_path):
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")
    queue = make_order_queue(tmp_path)
    review_id = queue_order_review(queue)
    nonce = nonce_for(queue, review_id)
    api = FakeTelegramAPI(batches=[[
        _callback_update(7, 111, 111, f"rv:approve:{review_id}:{nonce}")]])
    poller = make_review_poller(api, FakeChatService(), app_state, queue)

    poller.poll_once()

    assert app_state.get(TELEGRAM_OFFSET_KEY) == "8"


def test_typing_indicator_is_resent_during_a_slow_turn(tmp_path, monkeypatch):
    from allpath_trade import telegram as telegram_module

    monkeypatch.setattr(telegram_module, "_TYPING_RESEND_SECONDS", 0.05)
    app_state = make_app_state(tmp_path)
    pair(app_state, "111")

    class SlowChatService:
        def send(self, text, source="web"):
            time.sleep(0.2)
            return "done"

    api = FakeTelegramAPI(batches=[[_update(1, 111, "hi")]])
    poller = make_poller(api, SlowChatService(), app_state)

    poller.poll_once()

    # One immediate call plus at least one resend during the ~0.2s turn.
    assert len(api.typing_calls) >= 2
