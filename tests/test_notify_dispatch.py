"""Tests for allpath_trade/notify/dispatch.py -- the shared choke point for
a queued-review/receipt notification reaching both email/ntfy and the
paired Telegram chat. `TelegramAPI` itself is never touched: `dispatch.py`
constructs one internally from `telegram_bot_token`, so every test here
monkeypatches `allpath_trade.notify.dispatch.TelegramAPI` to a fake that
records calls, mirroring how tests/test_telegram_poller.py fakes the same
class at its own call site."""

from __future__ import annotations

from typing import ClassVar

from allpath_trade.notify import dispatch
from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, AppState
from allpath_trade.store.db import connect
from allpath_trade.store.reviews import ReviewQueue

TOKEN = "fake-bot-token"


class FakeTelegramAPI:
    instances: ClassVar[list[FakeTelegramAPI]] = []

    def __init__(self, token: str) -> None:
        self.token = token
        self.sent: list[tuple[str, str, dict | None]] = []
        FakeTelegramAPI.instances.append(self)

    def send_message(self, chat_id: str, html: str, reply_markup=None) -> bool:
        self.sent.append((chat_id, html, reply_markup))
        return True


class SpyNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))
        return True


class RaisingNotifier:
    def send(self, subject, body):
        raise RuntimeError("smtp down")


def make_queue(tmp_path):
    conn = connect(tmp_path / "t.db")
    return ReviewQueue(conn, None), AppState(conn)


def pair(app_state: AppState, chat_id: str = "111") -> None:
    app_state.set(TELEGRAM_CHAT_ID_KEY, chat_id)


def setup_fake_api(monkeypatch):
    FakeTelegramAPI.instances = []
    monkeypatch.setattr(dispatch, "TelegramAPI", FakeTelegramAPI)


# ---------------------------------------------------------------------------
# push_telegram_review_queued
# ---------------------------------------------------------------------------

def test_push_telegram_review_queued_sends_buttons_with_review_id_and_nonce(tmp_path, monkeypatch):
    setup_fake_api(monkeypatch)
    queue, app_state = make_queue(tmp_path)
    pair(app_state)
    handle = queue.add(strategy_id="s", rule_id="r", ticker="AAPL", rule_type="soft",
                       condition="price < 100", action="sell", snapshot={}, intent=None)

    dispatch.push_telegram_review_queued(
        queue=queue, app_state=app_state, telegram_bot_token=TOKEN,
        review_id=int(handle), body="Item waiting for approval", account="paper")

    [api] = FakeTelegramAPI.instances
    assert len(api.sent) == 1
    chat_id, html, markup = api.sent[0]
    assert chat_id == "111"
    assert "Item waiting for approval" in html
    row = queue.get(int(handle))
    nonce = row["approval_token_hash"][:16]
    [[approve, reject]] = markup["inline_keyboard"]
    assert approve == {"text": "✅ Approve", "callback_data": f"rv:approve:{int(handle)}:{nonce}"}
    assert reject == {"text": "❌ Reject", "callback_data": f"rv:reject:{int(handle)}:{nonce}"}


def test_push_telegram_review_queued_noop_when_no_token(tmp_path, monkeypatch):
    setup_fake_api(monkeypatch)
    queue, app_state = make_queue(tmp_path)
    pair(app_state)
    handle = queue.add(strategy_id="s", rule_id="r", ticker="AAPL", rule_type="soft",
                       condition="c", action="a", snapshot={}, intent=None)

    dispatch.push_telegram_review_queued(
        queue=queue, app_state=app_state, telegram_bot_token="",
        review_id=int(handle), body="x", account="paper")

    assert FakeTelegramAPI.instances == []


def test_push_telegram_review_queued_noop_when_not_paired(tmp_path, monkeypatch):
    setup_fake_api(monkeypatch)
    queue, app_state = make_queue(tmp_path)
    handle = queue.add(strategy_id="s", rule_id="r", ticker="AAPL", rule_type="soft",
                       condition="c", action="a", snapshot={}, intent=None)

    dispatch.push_telegram_review_queued(
        queue=queue, app_state=app_state, telegram_bot_token=TOKEN,
        review_id=int(handle), body="x", account="paper")

    assert FakeTelegramAPI.instances == []


def test_push_telegram_review_queued_noop_on_missing_review(tmp_path, monkeypatch):
    setup_fake_api(monkeypatch)
    queue, app_state = make_queue(tmp_path)
    pair(app_state)

    dispatch.push_telegram_review_queued(
        queue=queue, app_state=app_state, telegram_bot_token=TOKEN,
        review_id=999, body="x", account="paper")

    [api] = FakeTelegramAPI.instances
    assert api.sent == []


def test_push_telegram_review_queued_swallows_api_exception(tmp_path, monkeypatch):
    class BoomAPI:
        def __init__(self, token):
            pass

        def send_message(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(dispatch, "TelegramAPI", BoomAPI)
    queue, app_state = make_queue(tmp_path)
    pair(app_state)
    handle = queue.add(strategy_id="s", rule_id="r", ticker="AAPL", rule_type="soft",
                       condition="c", action="a", snapshot={}, intent=None)

    dispatch.push_telegram_review_queued(  # must not raise
        queue=queue, app_state=app_state, telegram_bot_token=TOKEN,
        review_id=int(handle), body="x", account="paper")


# ---------------------------------------------------------------------------
# push_telegram_receipt
# ---------------------------------------------------------------------------

def test_push_telegram_receipt_sends_plain_message_no_buttons(tmp_path, monkeypatch):
    setup_fake_api(monkeypatch)
    _queue, app_state = make_queue(tmp_path)
    pair(app_state)

    dispatch.push_telegram_receipt(
        app_state=app_state, telegram_bot_token=TOKEN, body="order submitted",
        account="paper")

    [api] = FakeTelegramAPI.instances
    [(chat_id, html, markup)] = api.sent
    assert chat_id == "111"
    assert "order submitted" in html
    assert markup is None


def test_push_telegram_receipt_noop_when_unpaired(tmp_path, monkeypatch):
    setup_fake_api(monkeypatch)
    _queue, app_state = make_queue(tmp_path)

    dispatch.push_telegram_receipt(app_state=app_state, telegram_bot_token=TOKEN, body="x",
                                   account="paper")

    assert FakeTelegramAPI.instances == []


# ---------------------------------------------------------------------------
# notify_review_queued -- both legs together
# ---------------------------------------------------------------------------

def test_notify_review_queued_sends_both_email_and_telegram(tmp_path, monkeypatch):
    setup_fake_api(monkeypatch)
    queue, app_state = make_queue(tmp_path)
    pair(app_state)
    notifier = SpyNotifier()
    handle = queue.add(strategy_id="s", rule_id="r", ticker="AAPL", rule_type="soft",
                       condition="c", action="a", snapshot={}, intent=None)

    dispatch.notify_review_queued(
        queue=queue, notifier=notifier, app_state=app_state,
        telegram_bot_token=TOKEN, review_id=int(handle),
        subject="subj", body="body text", account="paper")

    assert notifier.sent == [("subj", "body text")]
    [api] = FakeTelegramAPI.instances
    assert len(api.sent) == 1


def test_notify_review_queued_notify_email_false_skips_email_not_telegram(tmp_path, monkeypatch):
    setup_fake_api(monkeypatch)
    queue, app_state = make_queue(tmp_path)
    pair(app_state)
    notifier = SpyNotifier()
    handle = queue.add(strategy_id="s", rule_id="r", ticker="AAPL", rule_type="soft",
                       condition="c", action="a", snapshot={}, intent=None)

    dispatch.notify_review_queued(
        queue=queue, notifier=notifier, app_state=app_state,
        telegram_bot_token=TOKEN, review_id=int(handle),
        subject="subj", body="body text", account="paper", notify_email=False)

    assert notifier.sent == []
    [api] = FakeTelegramAPI.instances
    assert len(api.sent) == 1


def test_notify_review_queued_no_notifier_no_telegram_is_a_noop(tmp_path, monkeypatch):
    setup_fake_api(monkeypatch)
    queue, app_state = make_queue(tmp_path)
    handle = queue.add(strategy_id="s", rule_id="r", ticker="AAPL", rule_type="soft",
                       condition="c", action="a", snapshot={}, intent=None)

    dispatch.notify_review_queued(  # must not raise
        queue=queue, notifier=None, app_state=app_state,
        telegram_bot_token="", review_id=int(handle), subject="s", body="b",
        account="paper")

    assert FakeTelegramAPI.instances == []


# ---------------------------------------------------------------------------
# shadow-dual-active T7: Telegram never renders `subject` -- only `body` --
# so the account has to be visible in the pushed body itself, same
# `[Paper]`/`[Shadow]` shape as every other prefix in this codebase.
# ---------------------------------------------------------------------------

def test_push_telegram_receipt_prefixes_body_for_shadow(tmp_path, monkeypatch):
    setup_fake_api(monkeypatch)
    _queue, app_state = make_queue(tmp_path)
    pair(app_state)

    dispatch.push_telegram_receipt(
        app_state=app_state, telegram_bot_token=TOKEN, body="order recorded",
        account="shadow")

    [api] = FakeTelegramAPI.instances
    [(_chat_id, html, _markup)] = api.sent
    assert html.startswith("[Shadow] ")


def test_push_telegram_receipt_does_not_double_prefix_an_already_prefixed_body(
        tmp_path, monkeypatch):
    setup_fake_api(monkeypatch)
    _queue, app_state = make_queue(tmp_path)
    pair(app_state)

    dispatch.push_telegram_receipt(
        app_state=app_state, telegram_bot_token=TOKEN,
        body="[Shadow] order recorded", account="shadow")

    [api] = FakeTelegramAPI.instances
    [(_chat_id, html, _markup)] = api.sent
    assert html.count("[Shadow]") == 1


def test_push_telegram_review_queued_prefixes_body_for_shadow(tmp_path, monkeypatch):
    setup_fake_api(monkeypatch)
    queue, app_state = make_queue(tmp_path)
    pair(app_state)
    handle = queue.add(strategy_id="s", rule_id="r", ticker="AAPL", rule_type="soft",
                       condition="c", action="a", snapshot={}, intent=None)

    dispatch.push_telegram_review_queued(
        queue=queue, app_state=app_state, telegram_bot_token=TOKEN,
        review_id=int(handle), body="waiting for approval", account="shadow")

    [api] = FakeTelegramAPI.instances
    _chat_id, html, _markup = api.sent[0]
    assert html.startswith("[Shadow] ")
