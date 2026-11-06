"""Shadow check-in pilot: a Telegram reply to a check-in message goes to the
shadow chat agent, framed with the questions, whatever /account says."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from allpath_trade.store.app_state import TELEGRAM_ACCOUNT_KEY, AppState
from allpath_trade.store.db import connect
from allpath_trade.store.views import CheckinStore
from allpath_trade.telegram import TelegramPoller
from tests.test_telegram_poller import (
    WEB_TOKEN,
    FakeChatService,
    FakeTelegramAPI,
    pair,
)


class CheckinHolder:
    def __init__(self, conn, *, enabled: bool) -> None:
        self.conn = conn
        self.app_state = AppState(conn)
        self.enabled = enabled

    def get(self):
        return SimpleNamespace(
            app_state=self.app_state, conn=self.conn,
            settings=SimpleNamespace(web_token=WEB_TOKEN,
                                     shadow_daily_checkin=self.enabled),
            accounts={})


def _reply(update_id, chat_id, text, reply_to):
    return {"update_id": update_id, "message": {
        "message_id": 1000 + update_id, "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id}, "text": text,
        "reply_to_message": {"message_id": reply_to, "text": "[Shadow] Check-in"}}}


def _setup(tmp_path, *, enabled=True, account="paper"):
    conn = connect(tmp_path / "t.db")
    holder = CheckinHolder(conn, enabled=enabled)
    pair(holder.app_state, 111)
    holder.app_state.set(TELEGRAM_ACCOUNT_KEY, account)
    CheckinStore(conn, account="shadow").add(
        et_date="2026-11-06", questions=["NVDA dip-buy fired 10x: keep, cap or stop?"],
        telegram_message_id=555)
    paper, shadow = FakeChatService("paper reply"), FakeChatService("shadow reply")
    api = FakeTelegramAPI()
    poller = TelegramPoller(api, {"paper": paper, "shadow": shadow}, holder,
                            threading.Event())
    return poller, api, paper, shadow


def test_reply_to_checkin_goes_to_shadow_even_when_chat_is_on_paper(tmp_path):
    poller, api, paper, shadow = _setup(tmp_path, account="paper")
    poller._handle_update(_reply(1, 111, "Cap it, I still like NVDA long term", 555))
    assert paper.calls == []
    [call] = shadow.calls
    assert call["text"].startswith("[Check-in reply")
    assert "NVDA dip-buy fired 10x" in call["text"]
    assert call["text"].endswith("Cap it, I still like NVDA long term")
    assert api.sent_messages[-1][1].startswith("[Shadow]")


def test_reply_to_some_other_message_follows_normal_routing(tmp_path):
    poller, _, paper, shadow = _setup(tmp_path, account="paper")
    poller._handle_update(_reply(1, 111, "hello", 999))
    assert shadow.calls == [] and paper.calls[0]["text"] == "hello"


def test_flag_off_disables_checkin_routing(tmp_path):
    poller, _, paper, shadow = _setup(tmp_path, enabled=False, account="paper")
    poller._handle_update(_reply(1, 111, "Cap it", 555))
    assert shadow.calls == [] and paper.calls[0]["text"] == "Cap it"
