"""REFLECTION_REVISION_CARDS / SHADOW_REFLECTION_REVISION_CARDS: a Telegram
card with Approve / Reject buttons for each strategy revision a nightly
reflection proposed that is still pending after the auto-apply step
(reflect.py `_send_revision_cards`). Off by default; with the flag off a
reflection revision sends no notification of its own, as before. All sample
text here is made up."""

from __future__ import annotations

import html as html_lib

import pytest

from allpath_trade import compact_cards, reflect
from allpath_trade.agent.reflection_tools import apply_revision_factory
from allpath_trade.config import account_flag
from allpath_trade.llm.base import LLMResponse
from allpath_trade.notify import dispatch
from allpath_trade.reflect import Reflector
from allpath_trade.store.app_state import AppState
from allpath_trade.store.order_threads import OrderThreadStore, find_card
from allpath_trade.store.reviews import ReviewQueue
from tests.helpers import assert_english_only
from tests.test_agent_loop import ScriptedLLM, tool_response
from tests.test_card_brief import SpyNotifier, _settings
from tests.test_reflect import (
    NOW,
    REPORT_TEXT,
    STRAT,
    STRAT_GME,
    STRAT_V2,
    make_components,
    make_settings,
)
from tests.test_telegram_poller import (
    FakeChatService,
    FakeStrategies,
    _callback_update,
    pair,
    make_review_poller,
)
from tests.test_telegram_poller import FakeTelegramAPI as PollerAPI

FLAG = {"shadow_reflection_revision_cards": True, "telegram_bot_token": "t"}
RATIONALE = "The stop sits too far below recent lows. More detail follows here."


class CardAPI:
    """Records review cards; optionally fails chosen sends."""

    instances: list = []

    def __init__(self, token):
        self.sent = []          # (chat_id, html, markup)
        CardAPI.instances.append(self)

    def _send(self, chat_id, html, reply_markup):
        # One API object is built per push, so the send count is class-wide.
        CardAPI.calls += 1
        if CardAPI.calls in CardAPI.fail_calls:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, html_lib.unescape(html), reply_markup))

    def send_message(self, chat_id, html, reply_markup=None):
        self._send(chat_id, html, reply_markup)
        return True

    def send_message_id(self, chat_id, html, reply_markup=None):
        self._send(chat_id, html, reply_markup)
        return 9000 + CardAPI.calls

    calls = 0
    fail_calls: set = set()  # 1-based send numbers that raise


@pytest.fixture(autouse=True)
def _telegram(monkeypatch):
    CardAPI.instances, CardAPI.fail_calls, CardAPI.calls = [], set(), 0
    monkeypatch.setattr(dispatch, "TelegramAPI", CardAPI)


def _sent():
    return [card for api in CardAPI.instances for card in api.sent]


def _components(tmp_path, account="shadow", *, paired=True):
    components = make_components(tmp_path, account=account)
    components.queue = ReviewQueue(components.conn, None, account=account)
    components.queue.set_revision_applier(apply_revision_factory(components.strategies))
    (components.strategies.directory / "u.yaml").write_text(STRAT)
    if paired:
        AppState(components.conn).set("telegram_chat_id", "111")
    return components


def _propose(*strategy_ids, new_yaml=STRAT_V2):
    calls = [tool_response("propose_strategy_revision", {
        "strategy_id": sid, "new_yaml": new_yaml, "rationale": RATIONALE}, id_=f"c{n}")
        for n, sid in enumerate(strategy_ids)]
    return ScriptedLLM([*calls, LLMResponse(text=REPORT_TEXT)])


def _run(components, llm, notifier=None, **flags):
    reflector = Reflector(llm=llm, components=components, settings=make_settings(**flags),
                          notifier=notifier)
    return reflector.run_daily(NOW)


def _rows(components):
    return [dict(r) for r in components.conn.execute(
        "SELECT * FROM pending_reviews ORDER BY id")]


# ---------------------------------------------------------------------------
# flag
# ---------------------------------------------------------------------------

def test_flag_defaults_off_and_the_shadow_flag_only_turns_on_shadow(monkeypatch):
    s = _settings()
    assert account_flag(s, reflect.REVISION_CARDS_FLAG, "paper") is False
    assert account_flag(s, reflect.REVISION_CARDS_FLAG, "shadow") is False
    s = _settings(shadow_reflection_revision_cards=True)
    assert account_flag(s, reflect.REVISION_CARDS_FLAG, "shadow") is True
    assert account_flag(s, reflect.REVISION_CARDS_FLAG, "paper") is False
    monkeypatch.setenv("SHADOW_REFLECTION_REVISION_CARDS", "true")
    s = _settings()
    assert s.shadow_reflection_revision_cards is True
    assert s.reflection_revision_cards is False


@pytest.mark.parametrize(("account", "flags"), [
    ("paper", {"telegram_bot_token": "t"}),
    ("shadow", {"telegram_bot_token": "t"}),
    # The shadow flag never reaches paper.
    ("paper", {**FLAG, "shadow_compact_cards": True}),
])
def test_flag_off_a_reflection_revision_sends_nothing_of_its_own(tmp_path, monkeypatch,
                                                                 account, flags):
    def boom(*a, **k):
        raise AssertionError("revision-card code ran with the flag off")

    monkeypatch.setattr(Reflector, "_send_revision_cards", boom)
    monkeypatch.setattr(reflect, "push_telegram_review_queued", boom)
    components = _components(tmp_path, account)
    notifier = SpyNotifier()
    assert _run(components, _propose("t"), notifier, **flags).startswith("ok:")
    (row,) = _rows(components)
    assert row["status"] == "pending" and row["card_format"] is None
    assert _sent() == []
    # The nightly report is the only notification, as before.
    assert len(notifier.sent) == 1 and "Daily reflection" in notifier.sent[0][0]


def test_flag_off_wires_no_proposal_collector(tmp_path, monkeypatch):
    seen = {}
    real = reflect.register_reflection_tools

    def spy(registry, **kw):
        seen.update(kw)
        return real(registry, **kw)

    monkeypatch.setattr(reflect, "register_reflection_tools", spy)
    _run(_components(tmp_path, "paper"), _propose("t"), **FLAG)
    assert seen["on_proposed"] is None


# ---------------------------------------------------------------------------
# flag on
# ---------------------------------------------------------------------------

def test_one_card_per_still_pending_revision_in_id_order(tmp_path):
    components = _components(tmp_path)
    notifier = SpyNotifier()
    out = _run(components, _propose("u", "t"), notifier, **FLAG, shadow_compact_cards=True)
    assert out.startswith("ok:")
    first, second = _rows(components)
    assert (first["strategy_id"], second["strategy_id"]) == ("u", "t")
    cards = _sent()
    assert [chat for chat, _html, _markup in cards] == ["111", "111"]
    for row, (_chat, html, markup) in zip((first, second), cards, strict=True):
        assert html.split("\n")[:3] == [
            f"[Shadow] AAPL · {row['strategy_id']} · strategy change",
            "Agent proposes 1 change:",
            "• r1: trigger price < $100 → $105"]
        assert "Why: The stop sits too far below recent lows." in html
        assert "More detail follows" not in html
        nonce = row["approval_token_hash"][:16]
        assert [b["callback_data"] for b in markup["inline_keyboard"][0]] == [
            f"rv:approve:{row['id']}:{nonce}", f"rv:reject:{row['id']}:{nonce}"]
        assert row["status"] == "pending"
        assert_english_only(html)
    # Telegram only: email/ntfy got the nightly report and nothing else.
    assert len(notifier.sent) == 1 and "Daily reflection" in notifier.sent[0][0]


def test_classic_revision_text_when_compact_cards_is_off(tmp_path):
    components = _components(tmp_path)
    _run(components, _propose("t"), **FLAG)
    ((_chat, html, markup),) = _sent()
    assert html.startswith("[Shadow] Item #1 is waiting for you.\n"
                           "Proposed: revise strategy 't' to v2 on AAPL\n"
                           "Strategy: t\n")
    assert len(markup["inline_keyboard"][0]) == 2
    assert _rows(components)[0]["card_format"] is None


def test_card_format_is_compact_once_whether_or_not_it_was_set_at_queue_time(tmp_path):
    # Queue-time recording off (the queue's own flag unset), card compact.
    (tmp_path / "a").mkdir()
    components = _components(tmp_path / "a")
    _run(components, _propose("t"), **FLAG, shadow_compact_cards=True)
    assert _rows(components)[0]["card_format"] == "compact"
    # Queue-time recording on, as app.py wires it: same value, no second write.
    (tmp_path / "b").mkdir()
    components = _components(tmp_path / "b")
    components.queue.compact_cards = True
    writes = []
    real = components.queue.record_card_format
    components.queue.record_card_format = lambda rid, fmt: (writes.append(rid),
                                                             real(rid, fmt))
    _run(components, _propose("t"), **FLAG, shadow_compact_cards=True)
    assert _rows(components)[0]["card_format"] == compact_cards.FORMAT
    assert writes == [1]  # once, at queue time


def test_order_thread_recording_and_reply_hint_work_for_these_cards(tmp_path):
    components = _components(tmp_path)
    components.queue.set_telegram_card_recorder(
        OrderThreadStore(components.conn, "shadow").add_card)
    _run(components, _propose("t"), **FLAG, shadow_compact_cards=True)
    ((_chat, html, _markup),) = _sent()
    assert html.rstrip().endswith("Reply to ask why. Only the buttons approve or reject.")
    card = find_card(components.conn, "111", 9001)
    assert card["review_id"] == 1 and card["account"] == "shadow"


def test_rows_from_earlier_runs_or_chat_get_no_card(tmp_path):
    components = _components(tmp_path)
    components.queue.add_strategy_revision(
        strategy_id="u", ticker="AAPL", old_yaml=STRAT, new_yaml=STRAT_V2, diff="",
        rationale="older", source="chat")
    _run(components, _propose("t"), **FLAG)
    ((_chat, html, _markup),) = _sent()
    assert "Item #2" in html


def test_a_failed_run_sends_no_cards(tmp_path):
    components = _components(tmp_path)
    llm = ScriptedLLM([
        tool_response("propose_strategy_revision", {
            "strategy_id": "t", "new_yaml": STRAT_V2, "rationale": RATIONALE}),
        LLMResponse(text="not a report"), LLMResponse(text="still not a report")])
    out = _run(components, llm, **FLAG)
    assert out.startswith("reflection failed")
    assert _sent() == [] and _rows(components)[0]["status"] == "pending"


# ---------------------------------------------------------------------------
# auto regime: applied rows get no card, held rows get one that says why
# ---------------------------------------------------------------------------

def test_auto_applied_revision_gets_no_card(tmp_path):
    components = _components(tmp_path)
    out = _run(components, _propose("t"), **FLAG,
               shadow_experiment_auto_apply_revisions=True)
    assert out.startswith("ok:")
    (row,) = _rows(components)
    assert row["status"] == "approved"
    assert _sent() == []


def test_held_revision_stays_pending_and_its_card_says_why(tmp_path):
    components = _components(tmp_path)
    _run(components, _propose("u", new_yaml=STRAT_GME), **FLAG, shadow_compact_cards=True,
         shadow_experiment_auto_apply_revisions=True)
    (row,) = _rows(components)
    assert row["status"] == "pending" and row["hold_reason"]
    ((_chat, html, markup),) = _sent()
    assert html.split("\n")[0] == f"[Shadow] ⚠ Held for human: {row['hold_reason']}"
    assert "ticker: AAPL → GME" in html
    assert len(markup["inline_keyboard"][0]) == 2


def test_mixed_night_only_the_pending_one_is_carded(tmp_path):
    components = _components(tmp_path)
    llm = ScriptedLLM([
        tool_response("propose_strategy_revision", {
            "strategy_id": "t", "new_yaml": STRAT_V2, "rationale": RATIONALE}, id_="a"),
        tool_response("propose_strategy_revision", {
            "strategy_id": "u", "new_yaml": STRAT_GME, "rationale": RATIONALE}, id_="b"),
        LLMResponse(text=REPORT_TEXT)])
    _run(components, llm, **FLAG, shadow_experiment_auto_apply_revisions=True)
    applied, held = _rows(components)
    assert (applied["status"], held["status"]) == ("approved", "pending")
    ((_chat, html, _markup),) = _sent()
    assert f"Item #{held['id']}" in html


# ---------------------------------------------------------------------------
# failures are isolated
# ---------------------------------------------------------------------------

def test_one_failed_send_does_not_stop_the_others_or_the_run(tmp_path, capsys):
    CardAPI.fail_calls = {1}
    components = _components(tmp_path)
    out = _run(components, _propose("u", "t"), **FLAG)
    assert out.startswith("ok:")
    ((_chat, html, _markup),) = _sent()
    assert "Item #2" in html
    err = capsys.readouterr().err
    assert "revision card for #1 was not sent" in err and "#2" not in err
    assert [r["status"] for r in _rows(components)] == ["pending", "pending"]


def test_a_card_that_cannot_be_built_is_logged_and_skipped(tmp_path, monkeypatch, capsys):
    real = Reflector._revision_card

    def flaky(self, row):
        if row["id"] == 1:
            raise RuntimeError("bad snapshot")
        return real(self, row)

    monkeypatch.setattr(Reflector, "_revision_card", flaky)
    components = _components(tmp_path)
    assert _run(components, _propose("u", "t"), **FLAG).startswith("ok:")
    assert len(_sent()) == 1
    assert "revision card for #1 failed: bad snapshot" in capsys.readouterr().err


def test_unpaired_telegram_is_quiet_without_a_token_and_logged_with_one(tmp_path, capsys):
    (tmp_path / "a").mkdir()
    components = _components(tmp_path / "a", paired=False)
    assert _run(components, _propose("t"), **FLAG).startswith("ok:")
    assert "was not sent" in capsys.readouterr().err
    (tmp_path / "b").mkdir()
    components = _components(tmp_path / "b", paired=False)
    assert _run(components, _propose("t"),
                shadow_reflection_revision_cards=True).startswith("ok:")
    assert capsys.readouterr().err == ""
    assert _sent() == []


# ---------------------------------------------------------------------------
# the buttons on such a card resolve the review the usual way
# ---------------------------------------------------------------------------

def _card_callback(tmp_path, verb):
    components = _components(tmp_path)
    _run(components, _propose("t"), **FLAG, shadow_compact_cards=True)
    ((_chat, _html, markup),) = _sent()
    data = {"approve": markup["inline_keyboard"][0][0]["callback_data"],
            "reject": markup["inline_keyboard"][0][1]["callback_data"]}[verb]
    paper_queue = ReviewQueue(components.conn, None, account="paper")
    api = PollerAPI(batches=[[_callback_update(1, 111, 111, data, message_id=42)]])
    app_state = AppState(components.conn)
    pair(app_state, "111")
    poller = make_review_poller(api, FakeChatService(), app_state,
                                paper_queue, shadow_queue=components.queue,
                                shadow_strategies=FakeStrategies())
    poller.poll_once()
    return components, api


def test_approve_button_on_a_reflection_card_applies_the_revision(tmp_path):
    components, api = _card_callback(tmp_path, "approve")
    (row,) = _rows(components)
    assert row["status"] == "approved" and row["account"] == "shadow"
    assert "version: 2" in (components.strategies.directory / "t.yaml").read_text()
    assert api.edited_markups == [("111", 42, None)]
    assert api.answered_callbacks == [("cb1", "[Shadow] Approved")]


def test_reject_button_on_a_reflection_card_rejects_the_revision(tmp_path):
    components, api = _card_callback(tmp_path, "reject")
    (row,) = _rows(components)
    assert row["status"] == "rejected"
    assert row["resolution_note"] == "rejected via Telegram"
    assert "version: 1" in (components.strategies.directory / "t.yaml").read_text()
    assert api.edited_markups == [("111", 42, None)]
