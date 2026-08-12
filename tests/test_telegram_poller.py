"""Tests for allpath_trade/telegram.py's poller half (`TelegramPoller`).

Never touches api.telegram.org or Telegram's SDK -- `api` is a fake object
implementing only `get_updates`/`send_message`/`send_typing` (the same shape
tests/test_telegram_api.py already proves against the real transport), and
`chat_service` is a fake too: `ChatService.send(text, source=...)` lands in
Task 4 (see plan's cross-task note), so every test here drives the poller
against a stand-in that just records calls. `app_state` is the real
`AppState` on a tmp-path sqlite DB -- the offset/pairing persistence
guarantees are worth proving against the real store, not a dict."""

from __future__ import annotations

import threading

from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, TELEGRAM_OFFSET_KEY, AppState
from allpath_trade.store.db import connect
from allpath_trade.telegram import TelegramPoller

WEB_TOKEN = "correct-horse-battery-staple"


def make_app_state(tmp_path):
    return AppState(connect(tmp_path / "t.db"))


class FakeTelegramAPI:
    """Stands in for TelegramAPI. `batches` is a list of update-lists, one
    consumed per `get_updates` call; once exhausted (or if never given)
    returns `[]`, matching the real transport's "nothing to do" contract."""

    def __init__(self, batches=None, on_get_updates=None):
        self._batches = list(batches or [])
        self._on_get_updates = on_get_updates
        self.get_updates_offsets: list[int] = []
        self.sent_messages: list[tuple[str, str]] = []
        self.typing_calls: list[str] = []

    def get_updates(self, offset: int, timeout_s: int = 50):
        self.get_updates_offsets.append(offset)
        if self._on_get_updates is not None:
            self._on_get_updates()
        if self._batches:
            return self._batches.pop(0)
        return []

    def send_message(self, chat_id: str, html: str) -> bool:
        self.sent_messages.append((chat_id, html))
        return True

    def send_typing(self, chat_id: str) -> None:
        self.typing_calls.append(chat_id)


class FakeChatService:
    """Stands in for ChatService -- `send(text, source=...)` is Task 4's
    signature; this fake accepts it so Task 3 doesn't need to wait on that
    task landing (per the plan's cross-task note)."""

    def __init__(self, reply="agent reply", log=None):
        self.reply = reply
        self.calls: list[dict] = []
        self._log = log

    def send(self, text: str, source: str = "web") -> str:
        self.calls.append({"text": text, "source": source})
        if self._log is not None:
            self._log.append(("chat_service.send", text))
        return self.reply


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
    return TelegramPoller(api, chat_service, app_state, web_token, threading.Event())


def _update(update_id, chat_id, text):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


# ---------------------------------------------------------------------------
# Pairing matrix
# ---------------------------------------------------------------------------

def test_start_with_correct_token_pairs_and_replies(tmp_path):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[_update(1, 111, f"/start {WEB_TOKEN}")]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    assert app_state.get(TELEGRAM_CHAT_ID_KEY) == "111"
    assert api.sent_messages == [
        ("111", "Paired. This chat now talks to your AllPath agent.")]


def test_start_with_wrong_token_does_not_pair_and_never_replies(tmp_path):
    app_state = make_app_state(tmp_path)
    api = FakeTelegramAPI(batches=[[_update(1, 111, "/start wrong-token")]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    assert app_state.get(TELEGRAM_CHAT_ID_KEY) is None
    assert api.sent_messages == []
    assert api.typing_calls == []


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
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
    api = FakeTelegramAPI(batches=[[_update(1, 222, f"/start {WEB_TOKEN}")]])
    poller = make_poller(api, FakeChatService(), app_state)

    poller.poll_once()

    assert app_state.get(TELEGRAM_CHAT_ID_KEY) == "222"


def test_after_repair_the_old_chat_id_becomes_a_stranger(tmp_path, capsys):
    app_state = make_app_state(tmp_path)
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
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
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
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
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")

    class RaisingChatService:
        def send(self, text, source="web"):
            raise RuntimeError("boom mid-turn")

    api = FakeTelegramAPI(batches=[[_update(7, 111, "hi")]])
    poller = make_poller(api, RaisingChatService(), app_state)

    poller.poll_once()  # must not raise

    assert app_state.get(TELEGRAM_OFFSET_KEY) == "8"


# ---------------------------------------------------------------------------
# Paired chat text -> typing, chat_service, formatted/split reply
# ---------------------------------------------------------------------------

def test_paired_text_sends_typing_then_calls_chat_service_with_telegram_source(tmp_path):
    app_state = make_app_state(tmp_path)
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
    api = FakeTelegramAPI(batches=[[_update(1, 111, "what's my position")]])
    chat_service = FakeChatService(reply="you own 10 AAPL")
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert api.typing_calls == ["111"]
    assert chat_service.calls == [{"text": "what's my position", "source": "telegram"}]
    assert api.sent_messages == [("111", "you own 10 AAPL")]


def test_reply_is_formatted_through_to_telegram_html(tmp_path):
    app_state = make_app_state(tmp_path)
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
    api = FakeTelegramAPI(batches=[[_update(1, 111, "hi")]])
    chat_service = FakeChatService(reply="**bold** reply")
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert api.sent_messages == [("111", "<b>bold</b> reply")]


def test_long_reply_is_split_into_multiple_send_message_calls(tmp_path):
    app_state = make_app_state(tmp_path)
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
    api = FakeTelegramAPI(batches=[[_update(1, 111, "hi")]])
    paragraph = "x" * 3000
    chat_service = FakeChatService(reply=f"{paragraph}\n\n{paragraph}\n\n{paragraph}")
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert len(api.sent_messages) > 1
    assert all(chat_id == "111" for chat_id, _ in api.sent_messages)
    assert all(len(html) <= 4096 for _, html in api.sent_messages)


# ---------------------------------------------------------------------------
# Non-text / malformed updates
# ---------------------------------------------------------------------------

def test_non_text_message_photo_is_ignored_gracefully(tmp_path):
    app_state = make_app_state(tmp_path)
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
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
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
    update = {"update_id": 1, "edited_message": {"chat": {"id": 111}, "text": "oops typo fixed"}}
    api = FakeTelegramAPI(batches=[[update]])
    chat_service = FakeChatService()
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert chat_service.calls == []
    assert api.sent_messages == []


def test_callback_query_update_is_ignored(tmp_path):
    app_state = make_app_state(tmp_path)
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
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
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
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
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
    api = FakeTelegramAPI(batches=[[{"update_id": 1}]])
    chat_service = FakeChatService()
    poller = make_poller(api, chat_service, app_state)

    poller.poll_once()

    assert chat_service.calls == []
    assert app_state.get(TELEGRAM_OFFSET_KEY) == "2"


# ---------------------------------------------------------------------------
# run_forever: backoff sequence
# ---------------------------------------------------------------------------

def test_run_forever_backs_off_on_repeated_failure_and_resets_on_success(tmp_path, monkeypatch):
    app_state = make_app_state(tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr("allpath_trade.telegram.time.sleep", lambda s: sleeps.append(s))

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
    poller = TelegramPoller(api, FakeChatService(), app_state, WEB_TOKEN, stop)

    poller.run_forever()

    # 3 failures -> 5s, 10s, 20s backoff; 4th call succeeds (no sleep after
    # success); 5th call sets stop and the loop exits without sleeping again.
    assert sleeps == [5, 10, 20]


def test_run_forever_caps_backoff_at_60_seconds(tmp_path, monkeypatch):
    app_state = make_app_state(tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr("allpath_trade.telegram.time.sleep", lambda s: sleeps.append(s))

    stop = threading.Event()
    call_count = {"n": 0}

    def fails_five_times_then_succeeds_and_stops():
        call_count["n"] += 1
        if call_count["n"] <= 5:
            raise RuntimeError("still down")
        stop.set()  # 6th call succeeds -- no sleep for it, loop exits next check

    api = FakeTelegramAPI(on_get_updates=fails_five_times_then_succeeds_and_stops)
    poller = TelegramPoller(api, FakeChatService(), app_state, WEB_TOKEN, stop)

    poller.run_forever()

    # 5, 10, 20, 40, then capped at 60 for the 5th failure.
    assert sleeps == [5, 10, 20, 40, 60]


def test_run_forever_does_not_poll_at_all_once_stop_is_already_set(tmp_path):
    app_state = make_app_state(tmp_path)
    stop = threading.Event()
    stop.set()
    api = FakeTelegramAPI()
    poller = TelegramPoller(api, FakeChatService(), app_state, WEB_TOKEN, stop)

    poller.run_forever()

    assert api.get_updates_offsets == []
