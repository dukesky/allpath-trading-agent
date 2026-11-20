"""COMPACT_CARDS / SHADOW_COMPACT_CARDS (allpath_trade/compact_cards.py):
verdict-first order cards, plain notify-only messages, and strategy
revisions as a list of changes. Off by default; with the flag off every
message and page is what it was (paper runs a live experiment). All sample
text here is made up."""

from __future__ import annotations

import csv
import json
from decimal import Decimal

import pytest

from allpath_trade import card_brief, compact_cards
from allpath_trade.agent.review import ReviewAgent, ReviewAnalysis
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.config import account_flag
from allpath_trade.notify import dispatch, events
from allpath_trade.order_threads import ORDER_THREAD_CARD_HINT
from allpath_trade.research import regime_metrics as rm
from allpath_trade.store.app_state import AppState
from allpath_trade.store.db import connect
from allpath_trade.store.order_threads import OrderThreadStore, find_card
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.strategy.loader import parse_strategy_text
from allpath_trade.web.order_sink import QueueingOrderSink
from tests.helpers import assert_english_only
from tests.test_agent_loop import ScriptedLLM
from tests.test_card_brief import (
    FOOTER,
    ORDER_BODY_BEFORE,
    ORDER_KW,
    REVIEW,
    REVISION_BODY_BEFORE,
    REVISION_KW,
    SpyNotifier,
    _answer,
    _call,
    _card,
    _chat_tools,
    _client,
    _prompt_for,
    _queue_order,
    _settings,
)
from tests.test_order_threads import IdAPI
from tests.test_sentinel import FakeTelegramAPI, make, strategy_yaml

BRIEF = {"why": "Revenue guidance was cut last week, so the dip is not just noise.",
         "risk": "Adding now deepens a position already down 22%.",
         "plan": "The ladder allows adds only while the thesis holds.",
         "origin": "review_agent"}
CRDO_KW = {"account": "shadow", "review_id": 276, "ticker": "CRDO", "action": "buy $1500",
           "strategy_id": "crdo-ladder", "trigger_price": "$135.35", "est_shares": "11.08",
           "recommendation": "skip — Guidance was cut last week. The thesis needs a re-check.",
           "kind": "order"}
CRDO_COMPACT = {"side": "buy", "notional": Decimal(1500), "qty": None,
                "price": Decimal("135.35")}

# One-line flow-style rules and a long thesis, the way production files look.
OLD_TSLA = """\
name: TSLA swing
status: active
version: 4
authorization: confirm
thesis: >
  Trade the range. Add on dips toward support while deliveries hold up,
  trim into strength, and never let the position grow past the cap.
position: {ticker: TSLA, target_weight: '0.10', max_weight: '0.15'}
rules:
- {id: trim-strength, type: hard, condition: 'position_weight > 0.14', action: sell 10%, state: armed}
- {id: dip-buy-1, type: soft, condition: 'price < 310 and position_weight < 0.10', action: buy $1500, state: armed, rearm: 24h, max_fires_per_day: 1}
- {id: stop, type: hard, condition: 'position_weight > 0 and price < 250', action: sell all, state: armed}
review: {cadence: daily, invalidation: Two quarters of falling deliveries}
"""
NEW_TSLA = (OLD_TSLA.replace("version: 4", "version: 5")
            .replace("position_weight > 0.14", "position_weight > 0.12")
            .replace("action: buy $1500, state: armed", "action: buy $1500, state: disabled"))


# ---------------------------------------------------------------------------
# flag
# ---------------------------------------------------------------------------

def test_flag_defaults_off_and_the_shadow_flag_only_turns_on_shadow(monkeypatch):
    s = _settings()
    assert account_flag(s, compact_cards.FLAG, "paper") is False
    assert account_flag(s, compact_cards.FLAG, "shadow") is False
    s = _settings(shadow_compact_cards=True)
    assert account_flag(s, compact_cards.FLAG, "shadow") is True
    assert account_flag(s, compact_cards.FLAG, "paper") is False
    monkeypatch.setenv("SHADOW_COMPACT_CARDS", "true")
    s = _settings()
    assert s.shadow_compact_cards is True and s.compact_cards is False


def test_a_new_queue_is_off_and_enabled_only_reads_a_real_true():
    conn = connect(":memory:")
    queue = ReviewQueue(conn, None, account="paper")
    assert queue.compact_cards is False and queue.revision_effect is None
    assert compact_cards.enabled(queue) is False
    assert compact_cards.enabled(object()) is False
    queue.compact_cards = True
    assert compact_cards.enabled(queue) is True


# ---------------------------------------------------------------------------
# flag off: every message is what it was
# ---------------------------------------------------------------------------

RULE_KW = {"account": "paper", "strategy_id": "fig-watch", "rule_id": "alert-20",
           "ticker": "FIG", "condition": "price <= 20", "disposition": "notified"}
RULE_BODY_BEFORE = ("Strategy fig-watch, rule alert-20 triggered on FIG.\n"
                    "Condition: price <= 20\n"
                    "Disposition: notified." + FOOTER)


def test_classic_builders_are_byte_identical_without_the_compact_argument():
    subject, body = events.review_queued(**ORDER_KW)
    assert body == ORDER_BODY_BEFORE and type(body) is str
    assert subject == "[Shadow] [AllPath] NVDA: waiting for your approval"
    assert events.review_queued(**ORDER_KW, compact=None)[1] == ORDER_BODY_BEFORE
    assert events.review_queued(**REVISION_KW, compact=None)[1] == REVISION_BODY_BEFORE
    subject, body = events.rule_triggered(**RULE_KW)
    assert subject == "[Paper] [AllPath] FIG: rule alert-20 triggered"
    assert body == RULE_BODY_BEFORE
    assert events.rule_triggered(**RULE_KW, compact=False, price=Decimal("19.8"),
                                 reason="ignored")[1] == RULE_BODY_BEFORE


def test_a_shadow_ledger_edit_keeps_the_classic_card_even_with_compact():
    kw = {"account": "shadow", "review_id": 5, "ticker": "", "action": "set cash to $5,000",
          "strategy_id": "", "kind": "shadow_edit"}
    assert events.review_queued(**kw, compact={}) == events.review_queued(**kw)


def _paper_run(tmp_path, *, compact_on_queue: bool):
    """A paper confirm-rule trigger through the real sentinel."""
    s, _store, _ex, q, notifier = make(tmp_path, strategy_yaml(auth="confirm"))
    q.compact_cards = compact_on_queue
    s.review_agent = _Agent("execute", "Because the trend held. More detail follows.")
    s.run_once()
    return q, notifier.sent


def test_sentinel_paper_messages_are_the_classic_ones(tmp_path):
    (tmp_path / "a").mkdir()
    q, sent = _paper_run(tmp_path / "a", compact_on_queue=False)
    [(subject, body)] = sent
    assert subject == "[Paper] [AllPath] AAPL: waiting for your approval"
    assert body == (
        "Item #1 is waiting for you.\n"
        "Proposed: sell all on AAPL\n"
        "Strategy: t\n"
        "Price at trigger: $200.00\n"
        "The agent recommends: execute — Because the trend held. More detail follows."
        + FOOTER)
    assert type(body) is str
    assert q.get(1)["card_format"] is None


def test_sentinel_notify_only_message_is_the_classic_one_with_the_flag_off(tmp_path):
    s, _store, _ex, _q, notifier = make(tmp_path, strategy_yaml(auth="notify"))
    s.run_once()
    [(subject, body)] = notifier.sent
    assert subject == "[Paper] [AllPath] AAPL: rule r1 triggered"
    assert body == ("Strategy t, rule r1 triggered on AAPL.\n"
                    "Condition: price < 250\n"
                    "Disposition: notified." + FOOTER)


def test_app_turns_compact_on_for_shadow_only(tmp_path, monkeypatch):
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: ScriptedLLM([]))
    client = _client(tmp_path, monkeypatch, shadow_compact_cards=True,
                     shadow_card_reason_brief=True)
    c = client.app.state.holder.get()
    paper, shadow = c.accounts["paper"], c.accounts["shadow"]
    assert paper.queue.compact_cards is False and paper.queue.revision_effect is None
    assert paper.sentinel.review_agent.compact_brief is False
    assert shadow.queue.compact_cards is True
    assert shadow.queue.revision_effect == shadow.sentinel.revision_effect
    assert shadow.sentinel.review_agent.compact_brief is True
    client.__exit__(None, None, None)


def test_app_with_the_flag_off_wires_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: ScriptedLLM([]))
    client = _client(tmp_path, monkeypatch, shadow_card_reason_brief=True)
    c = client.app.state.holder.get()
    for account in ("paper", "shadow"):
        assert c.accounts[account].queue.compact_cards is False
        assert c.accounts[account].queue.revision_effect is None
        assert c.accounts[account].sentinel.review_agent.compact_brief is False
    client.__exit__(None, None, None)


def test_paper_pending_page_is_identical_whether_shadow_has_the_flag(tmp_path, monkeypatch):
    pages = []
    for name, flags in (("off", {}), ("on", {"shadow_compact_cards": True})):
        (tmp_path / name).mkdir()
        client = _client(tmp_path / name, monkeypatch, **flags)
        queue, handle = _queue_order(client)
        queue.attach_brief(int(handle), BRIEF)
        queue.add_strategy_revision(strategy_id="tsla-swing", ticker="TSLA",
                                    old_yaml=OLD_TSLA, new_yaml=NEW_TSLA, diff="",
                                    rationale="Long rationale.", source="chat")
        html = client.get("/reviews").text
        assert "compact-card" not in html and "Agent proposes" not in html
        assert "split-diff" in html and "<details" in html
        pages.append((_card(html, 1), _card(html, 2)))
        client.__exit__(None, None, None)
    assert pages[0] == pages[1]


# ---------------------------------------------------------------------------
# one sentence, hard cap
# ---------------------------------------------------------------------------

def test_one_sentence_keeps_the_first_sentence_only():
    assert compact_cards.one_sentence(
        "Guidance was cut. The thesis needs a re-check.") == "Guidance was cut."
    # Decimals, prices and abbreviations do not end a sentence.
    assert compact_cards.one_sentence(
        "Price fell to $135.35 vs. a $185 trigger, i.e. 27% lower. Next.") == (
        "Price fell to $135.35 vs. a $185 trigger, i.e. 27% lower.")
    assert compact_cards.one_sentence("  **Bold**   claim\nhere ") == "Bold claim here"
    assert compact_cards.one_sentence(None) == "" and compact_cards.one_sentence("") == ""
    assert compact_cards.one_sentence("Price * 1.1 beats it. Next.") == "Price * 1.1 beats it."
    assert compact_cards.one_sentence("The answer is no. Wait.") == "The answer is no."


def test_one_sentence_is_cut_at_the_word_cap_and_says_so():
    long = " ".join(f"word{i}" for i in range(60)) + "."
    out = compact_cards.one_sentence(long)
    assert out.endswith("…") and len(out.split()) == compact_cards.MAX_WORDS
    assert out.split()[0] == "word0"
    exact = " ".join(["w"] * compact_cards.MAX_WORDS) + "."
    assert compact_cards.one_sentence(exact) == exact


# ---------------------------------------------------------------------------
# order card
# ---------------------------------------------------------------------------

def test_compact_order_card_leads_with_the_trade_and_the_verdict():
    subject, body = events.review_queued(**CRDO_KW, brief=BRIEF, compact=CRDO_COMPACT)
    assert subject == "[Shadow] CRDO buy $1,500 — agent says skip"
    assert body == (
        "CRDO · Buy $1,500 (~11 sh @ $135.35)\n"
        "Agent: SKIP\n"
        "\n"
        "Why: Revenue guidance was cut last week, so the dip is not just noise.\n"
        "Risk: Adding now deepens a position already down 22%.\n"
        "\n"
        "Shadow: approving only records it; you place the order yourself.")
    assert body.email == body + "\n\n" + compact_cards.EMAIL_FOOTER
    assert body.card_format == "compact"
    # What moved off the card.
    for gone in ("Item #", "Strategy:", "crdo-ladder", "Plan:", "access token",
                 "Est. size", "The agent recommends"):
        assert gone not in body.email
    assert len(body.email) < len(events.review_queued(**CRDO_KW, brief=BRIEF)[1]) / 1.5
    # The machine-readable event is the classic one.
    assert subject.event == events.review_queued(**CRDO_KW)[0].event
    assert_english_only(body.email)


def test_compact_order_card_caps_long_brief_lines():
    brief = {"why": "First reason sentence. Second sentence that must not show.",
             "risk": " ".join(["risk"] * 50) + ".", "plan": ""}
    _, body = events.review_queued(**CRDO_KW, brief=brief, compact=CRDO_COMPACT)
    lines = body.split("\n")
    assert "Why: First reason sentence." in lines
    [risk] = [line for line in lines if line.startswith("Risk: ")]
    assert risk.endswith("…") and len(risk.split()) == compact_cards.MAX_WORDS + 1


def test_compact_order_card_without_a_brief_uses_the_reasoning_first_sentence():
    _, body = events.review_queued(**CRDO_KW, compact=CRDO_COMPACT)
    assert body == (
        "CRDO · Buy $1,500 (~11 sh @ $135.35)\n"
        "Agent: SKIP\n"
        "\n"
        "Why: Guidance was cut last week.\n"
        "\n"
        "Shadow: approving only records it; you place the order yourself.")


def test_compact_order_card_keeps_warnings_link_and_sell_sizes():
    kw = {**CRDO_KW, "account": "paper", "recommendation": "",
          "approve_url": "https://x.test/a/276?k=tok"}
    subject, body = events.review_queued(
        **kw, no_analysis_reason="LLM unavailable", stop_warning="Stop already fired.",
        compact={"side": "sell", "qty": Decimal("57.6726"), "notional": None,
                 "price": Decimal("135.35")})
    assert subject == "[Paper] CRDO sell ~58 sh — no agent analysis"
    assert body.split("\n") == [
        "⚠ No agent analysis: LLM unavailable. Check this order yourself.",
        "⚠ Stop already fired.",
        "CRDO · Sell ~58 sh @ $135.35 (~$7,806)",
        "",
        "Review & approve: https://x.test/a/276?k=tok"]
    assert "Shadow:" not in body  # paper routes real orders


def test_compact_option_order_shows_the_action_and_the_contract():
    subject, body = events.review_queued(
        account="shadow", review_id=9, ticker="NVDA", action="buy_call $1000 dte>=30",
        strategy_id="nvda", recommendation="execute — Momentum intact.", kind="option_order",
        option_preview="2x NVDA261016C00220000 ≈ $930.00",
        compact={"side": "", "notional": None, "qty": None, "price": Decimal("212.4")})
    assert body.split("\n")[:3] == [
        "NVDA · Buy_call $1000 dte>=30",
        "Agent: EXECUTE",
        "Option at trigger: 2x NVDA261016C00220000 ≈ $930.00 (re-priced when you approve)"]
    assert "Shadow:" not in body  # the place-it-yourself line is for stock orders
    assert subject == "[Shadow] NVDA buy_call $1000 dte>=30 — agent says execute"


class _Agent:
    def __init__(self, verdict, reasoning, brief=None):
        self.verdict, self.reasoning, self.brief = verdict, reasoning, brief

    def analyze(self, review):
        analysis = ReviewAnalysis(recommendation=self.verdict, reasoning=self.reasoning)
        analysis.brief = self.brief
        return analysis


def _shadow_run(tmp_path, yaml_text, agent=None, **kw):
    s, store, ex, q, notifier = make(tmp_path, yaml_text, account="shadow", **kw)
    q.compact_cards = True
    q.revision_effect = s.revision_effect
    s.review_agent = agent
    report = s.run_once()
    return s, q, notifier, report, ex, store


def test_sentinel_sends_the_compact_card_and_records_the_format(tmp_path):
    yaml_text = strategy_yaml(auth="confirm", rule_type="soft", action="buy $1500",
                              condition="price < 250")
    _s, q, notifier, report, ex, _store = _shadow_run(
        tmp_path, yaml_text, _Agent("skip", "Guidance was cut. More.", dict(BRIEF)))
    assert report.outcomes[0].disposition == "queued" and ex.calls == []
    [(subject, body)] = notifier.sent
    assert subject == "[Shadow] AAPL buy $1,500 — agent says skip"
    # The email leg gets the card plus the one-line footer.
    assert body == (
        "AAPL · Buy $1,500 (~7.5 sh @ $200)\n"
        "Agent: SKIP\n"
        "\n"
        f"Why: {BRIEF['why']}\n"
        f"Risk: {BRIEF['risk']}\n"
        "\n"
        "Shadow: approving only records it; you place the order yourself.\n"
        "\n" + compact_cards.EMAIL_FOOTER)
    row = q.get(1)
    assert row["card_format"] == "compact" and row["status"] == "pending"
    assert json.loads(row["decision_brief"]) == BRIEF  # stored whole, cut only on the card


def test_sentinel_compact_card_without_an_agent_warns_first(tmp_path):
    yaml_text = strategy_yaml(auth="confirm", rule_type="soft", action="buy $1500")
    _s, _q, notifier, _report, _ex, _store = _shadow_run(tmp_path, yaml_text, None)
    [(subject, body)] = notifier.sent
    assert subject == "[Shadow] AAPL buy $1,500 — no agent analysis"
    assert body.split("\n")[:2] == [
        "⚠ No agent analysis: LLM unavailable. Check this order yourself.",
        "AAPL · Buy $1,500 (~7.5 sh @ $200)"]
    assert "Agent:" not in body


def test_chat_order_compact_card_uses_the_reason_as_why(tmp_path):
    conn = connect(tmp_path / "t.db")
    queue = ReviewQueue(conn, None, account="shadow")
    queue.compact_cards = True
    notifier = SpyNotifier()
    sink = QueueingOrderSink(queue, None, None, None, None, notifier=notifier,
                             account="shadow")
    sink._preview = lambda intent: "ok"
    sink.propose(OrderIntent(ticker="MSFT", side=OrderSide.BUY, notional=Decimal(2000),
                             reason="You asked to start a position. It is 2% of equity."))
    [(subject, body)] = notifier.sent
    assert subject == "[Shadow] MSFT buy $2,000 — waiting for your approval"
    assert body.split("\n")[:4] == ["MSFT · Buy $2,000", "Proposed in chat", "",
                                    "Why: You asked to start a position."]
    assert queue.get(1)["card_format"] == "compact"


# ---------------------------------------------------------------------------
# Telegram: buttons, and the reply hint only where replies work
# ---------------------------------------------------------------------------

def _shadow_queue(tmp_path):
    conn = connect(tmp_path / "t.db")
    app_state = AppState(conn)
    app_state.set("telegram_chat_id", "111")
    queue = ReviewQueue(conn, None, account="shadow")
    rid = int(queue.add(strategy_id="s", rule_id="r", ticker="CRDO", rule_type="soft",
                        condition="price <= 185", action="buy $1500", snapshot={},
                        intent=OrderIntent(ticker="CRDO", side=OrderSide.BUY,
                                           notional=Decimal(1500), reason="r")))
    return conn, app_state, queue, rid


def test_telegram_compact_card_has_the_reply_hint_only_with_order_threads(
        tmp_path, monkeypatch):
    subject, body = events.review_queued(**CRDO_KW, brief=BRIEF, compact=CRDO_COMPACT)
    # Order threads off: no hint, no footer, two buttons.
    api = FakeTelegramAPI("t")
    monkeypatch.setattr(dispatch, "TelegramAPI", lambda token: api)
    (tmp_path / "a").mkdir()
    _conn, app_state, queue, rid = _shadow_queue(tmp_path / "a")
    notifier = SpyNotifier()
    dispatch.notify_review_queued(queue=queue, notifier=notifier, app_state=app_state,
                                  telegram_bot_token="t", review_id=rid, subject=subject,
                                  body=body, account="shadow")
    [(_chat, html, markup)] = api.sent
    assert html.startswith("[Shadow] CRDO · Buy $1,500 (~11 sh @ $135.35)\nAgent: SKIP")
    assert "Reply" not in html and "dashboard" not in html
    assert [b["text"] for b in markup["inline_keyboard"][0]] == ["✅ Approve", "❌ Reject"]
    assert notifier.sent == [(subject, body.email)]
    assert queue.get(rid)["card_format"] == "compact"

    # Order threads on: the short hint, and the card id is recorded.
    IdAPI.instances = []
    monkeypatch.setattr(dispatch, "TelegramAPI", IdAPI)
    (tmp_path / "b").mkdir()
    conn, app_state, queue, rid = _shadow_queue(tmp_path / "b")
    queue.set_telegram_card_recorder(OrderThreadStore(conn, "shadow").add_card)
    dispatch.push_telegram_review_queued(queue=queue, app_state=app_state,
                                         telegram_bot_token="t", review_id=rid,
                                         body=body, account="shadow")
    [(_chat, html, _markup)] = IdAPI.instances[0].sent_with_id
    assert html.rstrip().endswith("↩️ Reply to ask why. Only the buttons approve or reject.")
    assert ORDER_THREAD_CARD_HINT.strip() not in html
    assert find_card(conn, "111", 4242)["review_id"] == rid


def test_a_classic_body_through_dispatch_records_no_format(tmp_path, monkeypatch):
    api = FakeTelegramAPI("t")
    monkeypatch.setattr(dispatch, "TelegramAPI", lambda token: api)
    _conn, app_state, queue, rid = _shadow_queue(tmp_path)
    subject, body = events.review_queued(**ORDER_KW)
    notifier = SpyNotifier()
    dispatch.notify_review_queued(queue=queue, notifier=notifier, app_state=app_state,
                                  telegram_bot_token="t", review_id=rid, subject=subject,
                                  body=body, account="shadow")
    assert notifier.sent == [(subject, ORDER_BODY_BEFORE)]
    assert api.sent[0][1].startswith("[Shadow] Item #12 is waiting for you.")
    assert queue.get(rid)["card_format"] is None


# ---------------------------------------------------------------------------
# notify-only / skipped
# ---------------------------------------------------------------------------

def test_plain_condition():
    plain = compact_cards.plain_condition
    assert plain("price <= 20") == "price ≤ $20"
    assert plain("price < 310 and position_weight < 0.10") == "price < $310 and weight < 10%"
    assert plain("position_weight < target_weight") == "weight < target weight"
    assert plain("pnl_pct >= 15 or price > 400.5") == "P&L ≥ 15% or price > $400.50"
    assert plain("price < -1") == "price < $-1"
    # Brackets survive, whichever way and/or nest (review finding).
    assert plain("(price < 10 or price > 20) and position_qty > 0") == (
        "(price < $10 or price > $20) and you hold shares")
    assert plain("price < 10 or price > 20 and pnl_pct > 5") == (
        "price < $10 or (price > $20 and P&L > 5%)")
    assert plain("position_weight > 0 and price < 75") == "you hold shares and price < $75"
    assert plain("position_qty == 0 and price < 75") == "you hold none and price < $75"
    assert plain("not a (condition") == "not a (condition"  # unreadable: unchanged


def test_notify_only_message_is_one_plain_sentence():
    kw = {**RULE_KW, "account": "shadow"}
    subject, body = events.rule_triggered(**kw, compact=True, price=Decimal("19.80"))
    assert body == "FIG fell to $19.80 (your alert: price ≤ $20). Nothing to approve."
    assert subject == "[Shadow] FIG alert: $19.80 (price ≤ $20)"
    assert subject.event == events.rule_triggered(**kw)[0].event
    up = events.rule_triggered(**{**kw, "condition": "price > 30"}, compact=True,
                               price=Decimal(31))[1]
    assert up == "FIG rose to $31 (your alert: price > $30). Nothing to approve."
    both = events.rule_triggered(
        **{**kw, "condition": "price < 20 and position_weight > 0.05"}, compact=True,
        price=Decimal("19.5"))[1]
    assert both == ("FIG is at $19.50 (your alert: price < $20 and weight > 5%). "
                    "Nothing to approve.")
    for text in (body, up, both):
        assert "Disposition" not in text and "dashboard" not in text
        assert_english_only(text)


def test_skipped_message_gives_a_reason_only_when_one_is_known():
    kw = {"account": "shadow", "strategy_id": "t", "rule_id": "stop", "ticker": "TSLA",
          "condition": "price < 315", "disposition": "skipped", "compact": True,
          "price": Decimal("310.2")}
    subject, body = events.rule_triggered(**kw, reason="you hold no TSLA to sell")
    assert subject == "[Shadow] TSLA: rule stop triggered, no order"
    assert body == ("TSLA: rule stop triggered at $310.20 (price < $315), but no order was "
                    "made: you hold no TSLA to sell. Nothing to approve.")
    assert events.rule_triggered(**kw)[1] == (
        "TSLA: rule stop triggered at $310.20 (price < $315). No order was made. "
        "Nothing to approve.")


def test_sentinel_compact_notify_and_skip_messages(tmp_path):
    (tmp_path / "a").mkdir()
    _s, _q, notifier, report, _ex, _store = _shadow_run(
        tmp_path / "a", strategy_yaml(auth="notify"))
    assert report.outcomes[0].disposition == "notified"
    assert notifier.sent == [(
        "[Shadow] AAPL alert: $200 (price < $250)",
        "AAPL fell to $200 (your alert: price < $250). Nothing to approve.")]

    # A sell rule with nothing held: the code knows why there is no order.
    (tmp_path / "b").mkdir()
    _s, _q, notifier, report, _ex, _store = _shadow_run(
        tmp_path / "b", strategy_yaml(auth="confirm"), qty="0")
    assert report.outcomes[0].disposition == "skipped"
    [(_subject, body)] = notifier.sent
    assert body == ("AAPL: rule r1 triggered at $200 (price < $250), but no order was "
                    "made: you hold no AAPL to sell. Nothing to approve.")

    # An autonomous agent skip: its own first sentence, nothing invented.
    (tmp_path / "c").mkdir()
    _s, _q, notifier, report, _ex, _store = _shadow_run(
        tmp_path / "c", strategy_yaml(auth="auto", rule_type="soft", action="buy $1500"),
        _Agent("skip", "Earnings are tomorrow. Wait for the print."))
    assert report.outcomes[0].disposition == "skipped"
    [(_subject, body)] = notifier.sent
    assert body == ("AAPL: rule r1 triggered at $200 (price < $250), but no order was "
                    "made: the agent decided not to trade: Earnings are tomorrow. "
                    "Nothing to approve.")


# ---------------------------------------------------------------------------
# strategy revision: what changes, computed from the YAML
# ---------------------------------------------------------------------------

def test_revision_changes_on_flow_style_rules():
    # Key changes (a rule turned off) come before threshold tweaks.
    assert compact_cards.revision_changes(OLD_TSLA, NEW_TSLA) == [
        "dip-buy-1: turned off",
        "trim-strength: trigger weight > 14% → 12%"]
    # Layout alone is no change: block style, other quoting, other key order.
    block = """\
version: 9
status: active
name: TSLA swing
authorization: confirm
thesis: "Trade the range. Add on dips toward support while deliveries hold up,
  trim into strength, and never let the position grow past the cap."
position:
  max_weight: 15%
  target_weight: 0.1
  ticker: TSLA
rules:
- id: trim-strength
  type: hard
  condition: position_weight > 0.14
  action: sell 10%
- id: dip-buy-1
  action: buy $1500
  condition: price < 310 and position_weight < 0.10
  type: soft
  rearm: 24h
  max_fires_per_day: 1
- id: stop
  type: hard
  condition: position_weight > 0 and price < 250
  action: sell all
  state: armed
review:
  invalidation: Two quarters of falling deliveries
"""
    assert compact_cards.revision_changes(OLD_TSLA, block) == []


def test_revision_changes_cover_every_kind_of_change():
    new = """\
name: TSLA swing v2
status: paused
version: 5
authorization: auto
horizon: swing
thesis: A different thesis entirely.
position: {ticker: TSLA, target_weight: '0.12', max_weight: '0.15', max_value: 20000}
rules:
- {id: trim-strength, type: soft, condition: 'position_weight > 0.14 or pnl_pct > 30', action: sell 20%, state: armed}
- {id: dip-buy-1, type: soft, condition: 'price < 300 and position_weight < 0.08', action: buy $1500, state: armed, rearm: 12h, max_fires_per_day: 2}
- {id: dip-buy-2, type: soft, condition: 'price < 280', action: buy $2000}
review: {cadence: weekly, invalidation: Something else}
notify_email: false
"""
    assert compact_cards.revision_changes(OLD_TSLA, new) == [
        # key changes first
        "authorization: confirm → auto (hard rules place orders without asking)",
        "status: active → paused",
        "trim-strength: action sell 10% → sell 20%",
        "dip-buy-2: new rule, buy $2000 when price < $280",
        "stop: removed",
        "target weight: 10% → 12%",
        "max value: none → $20,000",
        # then the rest
        "trim-strength: trigger weight > 14% → weight > 14% or P&L > 30%",
        "trim-strength: type hard → soft",
        "dip-buy-1: trigger price < $310 → $300, weight < 10% → 8%",
        "dip-buy-1: re-arm 24h → 12h",
        "dip-buy-1: max fires per day 1 → 2",
        "thesis rewritten",
        "name: TSLA swing → TSLA swing v2",
        "horizon: none → swing",
        "review cadence: daily → weekly",
        "invalidation text rewritten",
        "email notifications: on → off"]


def test_revision_changes_for_a_new_or_unreadable_strategy():
    assert compact_cards.revision_changes("", OLD_TSLA, is_new=True) == [
        "authorization: confirm (every order asks you first)",
        "status after approval: active",
        "target weight: 10%",
        "max weight: 15%",
        "rule trim-strength: sell 10% when weight > 14%",
        "rule dip-buy-1: buy $1500 when price < $310 and weight < 10%",
        "rule stop: sell all when you hold shares and price < $250"]
    assert compact_cards.revision_changes(OLD_TSLA, "- not\n- a mapping") is None
    assert compact_cards.revision_changes(OLD_TSLA, "key: [unclosed") is None


def test_revision_changes_agree_with_the_strategy_loader():
    """Every number the summary prints comes from the parsed documents."""
    old = parse_strategy_text("tsla-swing", OLD_TSLA)
    new = parse_strategy_text("tsla-swing", NEW_TSLA)
    assert old.rules[0].condition == "position_weight > 0.14"
    assert new.rules[0].condition == "position_weight > 0.12"
    assert new.rules[1].state.value == "disabled"


REVISION_COMPACT = {"old_yaml": OLD_TSLA, "new_yaml": NEW_TSLA, "is_new": False,
                    "rationale": ("Measured divergence between the trim trigger and the "
                                  "realised range over the last month. A second sentence "
                                  "with more detail that the card must not show."),
                    "effect": "no armed rule fires at the current price ($332.10).",
                    "web_base_url": "https://x.test"}
TSLA_KW = {"account": "shadow", "review_id": 31, "ticker": "TSLA",
           "action": "revise strategy 'tsla-swing' to v5", "strategy_id": "tsla-swing",
           "kind": "strategy_revision"}


def test_compact_revision_card_is_a_change_summary():
    subject, body = events.review_queued(**TSLA_KW, compact=REVISION_COMPACT)
    assert subject == "[Shadow] tsla-swing — 2 strategy changes proposed"
    assert body == (
        "TSLA · tsla-swing · strategy change\n"
        "Agent proposes 2 changes:\n"
        "• dip-buy-1: turned off\n"
        "• trim-strength: trigger weight > 14% → 12%\n"
        "Effect now: no armed rule fires at the current price ($332.10).\n"
        "Why: Measured divergence between the trim trigger and the realised range over "
        "the last month.\n"
        "\n"
        "Full diff: https://x.test/reviews#review-31")
    assert body.card_format == "compact" and body.email.endswith(compact_cards.EMAIL_FOOTER)
    assert "position_weight" not in body and "version" not in body
    assert_english_only(body.email)


def _many_rules(threshold: int, *, stop: bool = True) -> str:
    text = OLD_TSLA.replace("review: {cadence: daily, invalidation: Two quarters of "
                            "falling deliveries}\n", "")
    if not stop:
        text = "\n".join(line for line in text.split("\n") if "id: stop" not in line)
    return text + "".join(
        f"- {{id: extra-{n}, type: soft, condition: 'price < {threshold + n}', "
        "action: buy $500}\n" for n in range(9))


def test_compact_revision_card_caps_the_list_and_prefers_the_brief_and_the_link():
    brief = {"why": "The range narrowed.", "risk": "More rules, more small buys.", "plan": ""}
    _, body = events.review_queued(
        **TSLA_KW, brief=brief, approve_url="https://x.test/a/31?k=tok",
        compact={"old_yaml": _many_rules(100), "new_yaml": _many_rules(200),
                 "rationale": "Long rationale.", "effect": ""})
    lines = body.split("\n")
    assert lines[1] == "Agent proposes 9 changes:"
    assert sum(line.startswith("• ") for line in lines) == compact_cards.MAX_BULLETS
    assert "+3 smaller changes on the web" in lines
    assert not any(line.startswith("Effect now") for line in lines)  # unknown: left out
    assert "Why: The range narrowed." in lines and "Risk: More rules, more small buys." in lines
    assert lines[-1] == "Full diff & approve: https://x.test/a/31?k=tok"


def test_the_cap_never_hides_a_removed_stop_or_a_raised_limit():
    """Review finding: six threshold tweaks used to push "stop: removed" and
    a new position limit past the cap, behind a collapsed diff."""
    new = _many_rules(200, stop=False).replace("max_weight: '0.15'", "max_weight: '0.5'")
    changes = compact_cards.revision_changes(_many_rules(100), new)
    assert changes[:2] == ["stop: removed", "max weight: 15% → 50%"]
    shown, more = compact_cards.capped(changes)
    assert shown[:2] == changes[:2] and len(shown) == 6 and more == 5
    # More key changes than the cap: every one of them is still shown.
    many_key = compact_cards.revision_changes(OLD_TSLA, _many_rules(100))
    shown, more = compact_cards.capped(many_key)
    assert len(shown) == 9 and all("new rule" in line for line in shown)
    assert more == len(many_key) - 9


def test_a_rewritten_but_unchanged_file_shows_no_changes():
    """Review finding: a draft's new text is the model re-serialised, which
    used to show defaults and spellings as changes."""
    hand_written = """\
name: T
status: active
position: {ticker: nvda, target_weight: 10%}
rules:
- {id: add, type: soft, condition: "price  <  100 and position_weight<0.1", action: buy $500, rearm: 60m}
"""
    import yaml

    doc = parse_strategy_text("t", hand_written)
    dumped = yaml.safe_dump(doc.model_dump(mode="json"), sort_keys=False)
    assert "max_fires_per_day: 3" in dumped and "ticker: NVDA" in dumped
    assert compact_cards.revision_changes(hand_written, dumped) == []
    assert compact_cards.revision_changes("a: {1: x, b: y}", "a: {1: z, b: y}") == ["a changed"]


def test_a_compact_builder_that_raises_falls_back_to_the_classic_card(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("bad yaml")

    monkeypatch.setattr(compact_cards, "revision_card", boom)
    monkeypatch.setattr(compact_cards, "order_card", boom)
    assert events.review_queued(**TSLA_KW, compact=REVISION_COMPACT) == \
        events.review_queued(**TSLA_KW)
    subject, body = events.review_queued(**CRDO_KW, compact=CRDO_COMPACT)
    assert (subject, body) == events.review_queued(**CRDO_KW) and type(body) is str
    assert "sending the classic card" in capsys.readouterr().err


def test_chat_draft_sends_the_compact_revision_card(tmp_path):
    reg, queue, notifier, _sink = _chat_tools(tmp_path)
    (tmp_path / "strategies" / "tsla-swing.yaml").write_text(OLD_TSLA)
    queue.compact_cards = True
    queue.revision_effect = lambda new_yaml, strategy_id: "no armed rule fires at the current price ($332.10)."
    out = _call(reg, "draft_strategy", strategy_id="tsla-swing", yaml_text=NEW_TSLA,
                reason="The realised range narrowed. More detail.")
    assert out.startswith("Draft queued")
    [(subject, body)] = notifier.sent
    assert subject == "[Shadow] tsla-swing — 2 strategy changes proposed"
    assert body.split("\n")[:6] == [
        "TSLA · tsla-swing · strategy change",
        "Agent proposes 2 changes:",
        "• dip-buy-1: turned off",
        "• trim-strength: trigger weight > 14% → 12%",
        "Effect now: no armed rule fires at the current price ($332.10).",
        "Why: The realised range narrowed."]
    assert queue.get(1)["card_format"] == "compact"


def test_chat_draft_with_the_flag_off_is_the_classic_card(tmp_path):
    reg, queue, notifier, _sink = _chat_tools(tmp_path)
    (tmp_path / "strategies" / "tsla-swing.yaml").write_text(OLD_TSLA)
    _call(reg, "draft_strategy", strategy_id="tsla-swing", yaml_text=NEW_TSLA, reason="r")
    [(subject, body)] = notifier.sent
    assert subject == "[Shadow] [AllPath] TSLA: waiting for your approval"
    assert body == ("Item #1 is waiting for you.\n"
                    "Proposed: revise strategy 'tsla-swing' to v5 on TSLA\n"
                    "Strategy: tsla-swing" + FOOTER)
    assert queue.get(1)["card_format"] is None


# ---------------------------------------------------------------------------
# "Effect now": the sentinel's evaluator, dry run
# ---------------------------------------------------------------------------

def _effect(tmp_path, new_yaml, **kw):
    s, store, ex, q, notifier = make(tmp_path, strategy_yaml(), **kw)
    before = (tmp_path / "t.yaml").read_text()
    out = s.revision_effect(new_yaml, "t")
    # A dry run: no order, no review, no message, no file or rule-state write.
    assert ex.calls == [] and q.list(None) == [] and notifier.sent == []
    assert (tmp_path / "t.yaml").read_text() == before
    assert store.last_fire("t", "r1") is None
    return out


@pytest.mark.parametrize(("yaml_kw", "make_kw", "expected"), [
    ({"auth": "confirm", "condition": "price < 150"}, {},
     "no armed rule fires at the current price ($200)."),
    ({"auth": "confirm", "condition": "price < 250"}, {},
     "at the current price ($200) the next check: r1 fires and asks you to approve: sell all."),
    ({"auth": "auto", "condition": "price < 250"}, {},
     "at the current price ($200) the next check: r1 fires and runs without asking: sell all."),
    ({"auth": "auto", "rule_type": "soft", "condition": "price < 250", "action": "buy $500"},
     {}, "at the current price ($200) the next check: r1 fires and goes to the agent "
         "to decide: buy $500."),
    ({"auth": "notify", "condition": "price < 250"}, {},
     "at the current price ($200) the next check: r1 fires and sends an alert, no order."),
    ({"auth": "confirm", "condition": "price < 250"}, {"qty": "0"},
     "at the current price ($200) the next check: r1 fires and makes no order "
     "(nothing to trade)."),
    ({"auth": "confirm", "condition": "price < 250", "status": "paused"}, {},
     "nothing, the strategy would be paused (rules are only checked while it is active)."),
])
def test_revision_effect_matches_what_the_sentinel_would_do(tmp_path, yaml_kw, make_kw,
                                                            expected):
    assert _effect(tmp_path, strategy_yaml(**yaml_kw), **make_kw) == expected


def test_revision_effect_agrees_with_a_real_sentinel_pass(tmp_path):
    """The dry run says r1 fires and asks; the real pass then does that."""
    yaml_text = strategy_yaml(auth="confirm", condition="price < 250")
    s, _store, _ex, q, _notifier = make(tmp_path, yaml_text)
    assert "r1 fires and asks you to approve" in s.revision_effect(yaml_text, "t")
    report = s.run_once()
    assert [o.disposition for o in report.outcomes] == ["queued"]
    assert len(q.list("pending")) == 1


def test_revision_effect_ignores_disabled_rules_and_fails_closed(tmp_path):
    disabled = strategy_yaml(auth="confirm").replace(
        'action: "sell all"}', 'action: "sell all", state: disabled}')
    assert _effect(tmp_path, disabled) == "no armed rule fires at the current price ($200)."
    s, _store, _ex, _q, _notifier = make(tmp_path, strategy_yaml())
    assert s.revision_effect("not: [valid", "t") == ""

    def boom(ticker):
        raise RuntimeError("no quote")

    s.data.get_quote = boom
    assert s.revision_effect(strategy_yaml(), "t") == ""


# ---------------------------------------------------------------------------
# the brief prompt for a compact account
# ---------------------------------------------------------------------------

def test_compact_brief_prompt_asks_for_a_reason_not_the_rule():
    reply = _answer(brief={k: BRIEF[k] for k in ("why", "risk", "plan")},
                    reasoning="Revenue guidance was cut last week; position down 22%.")
    llm = ScriptedLLM([reply])
    agent = ReviewAgent(llm, ToolRegistry(), card_brief=True, compact_brief=True)
    analysis = agent.analyze(REVIEW)
    prompt = llm.seen[0][0]["content"]
    assert prompt == _prompt_for(REVIEW) + card_brief.COMPACT_REVIEW_PROMPT_ADDENDUM
    assert "do not restate the rule condition" in prompt
    assert "at most 20 words" in prompt
    assert len(llm.seen) == 1  # the same single call
    assert analysis.brief["why"] == BRIEF["why"]  # same grounding check, kept


def test_compact_brief_prompt_needs_the_brief_flag_and_keeps_the_grounding_check():
    llm = ScriptedLLM([_answer()])
    ReviewAgent(llm, ToolRegistry(), compact_brief=True).analyze(REVIEW)
    assert llm.seen[0][0]["content"] == _prompt_for(REVIEW)  # brief flag off: old prompt
    llm = ScriptedLLM([_answer(brief={"why": "Volume was 37 times normal.",
                                      "risk": "It keeps falling.", "plan": ""})])
    analysis = ReviewAgent(llm, ToolRegistry(), card_brief=True,
                           compact_brief=True).analyze(REVIEW)
    assert analysis.brief is None
    assert analysis.brief_drop["missing"] == ["37"]


# ---------------------------------------------------------------------------
# Pending page
# ---------------------------------------------------------------------------

def _shadow_client(tmp_path, monkeypatch, **flags):
    client = _client(tmp_path, monkeypatch, shadow_compact_cards=True, **flags)
    client.cookies.set("account", "shadow")
    return client, client.app.state.holder.get().accounts["shadow"].queue


def _add_order(queue, *, analysis=True):
    handle = queue.add(strategy_id="crdo-ladder", rule_id="add-1", ticker="CRDO",
                       rule_type="soft", condition="price <= 185", action="buy $1500",
                       snapshot={"price": "135.35"},
                       intent=OrderIntent(ticker="CRDO", side=OrderSide.BUY,
                                          notional="1500", reason="rule add-1"))
    if analysis:
        queue.attach_analysis(int(handle), ReviewAnalysis(
            recommendation="skip",
            reasoning="Guidance was cut last week. Long analysis text.").model_dump_json())
    return handle


def test_pending_page_compact_order_card(tmp_path, monkeypatch):
    client, queue = _shadow_client(tmp_path, monkeypatch, shadow_card_reason_brief=True)
    handle = _add_order(queue)
    queue.attach_brief(int(handle), BRIEF)
    html = client.get("/reviews").text
    card = _card(html, int(handle))
    assert "compact-card" in html
    assert "<strong>CRDO · Buy $1,500 (~11 sh @ $135.35)</strong>" in card
    assert "<strong>Agent: SKIP</strong>" in card
    assert f"<strong>Why:</strong> {BRIEF['why']}" in card
    assert f"<strong>Risk:</strong> {BRIEF['risk']}" in card
    # Verdict, why and risk come before the disclosure; rule, plan and the
    # full analysis are inside it.
    details = card.index("<details")
    assert card.index("Agent: SKIP") < card.index("Why:") < details
    for inside in ("crdo-ladder/add-1", "price &lt;= 185", f"<strong>Plan:</strong> {BRIEF['plan']}",
                   "Long analysis text."):
        assert card.index(inside) > details
    assert 'action="/reviews/1/approve"' in card
    assert_english_only(card)
    client.__exit__(None, None, None)


def test_pending_page_compact_order_card_without_brief_or_analysis(tmp_path, monkeypatch):
    client, queue = _shadow_client(tmp_path, monkeypatch)
    _add_order(queue)
    _add_order(queue, analysis=False)
    html = client.get("/reviews").text
    with_analysis, without = _card(html, 1), _card(html, 2)
    assert "<strong>Why:</strong> Guidance was cut last week." in with_analysis
    assert "Risk:" not in with_analysis
    assert "No agent analysis: LLM unavailable" in without
    assert "Agent:" not in without and "Why:" not in without
    assert without.index("No agent analysis") < without.index("<details")
    client.__exit__(None, None, None)


def test_pending_page_compact_revision_card(tmp_path, monkeypatch):
    client, queue = _shadow_client(tmp_path, monkeypatch)
    c = client.app.state.holder.get().accounts["shadow"]
    (c.strategies.directory / "tsla-swing.yaml").write_text(OLD_TSLA)
    rid = queue.add_strategy_revision(
        strategy_id="tsla-swing", ticker="TSLA", old_yaml=OLD_TSLA, new_yaml=NEW_TSLA,
        diff="", rationale="The realised range narrowed. Long rationale tail.",
        source="reflection")
    html = client.get("/reviews").text
    card = _card(html, int(rid))
    assert "<strong>TSLA · tsla-swing · strategy change</strong>" in card
    assert "<strong>Agent proposes 2 changes:</strong>" in card
    assert ("<li>dip-buy-1: turned off</li><li>trim-strength: trigger weight &gt; 14% → 12%</li>"
            in card)
    assert "<strong>Effect now:</strong>" in card
    assert "<strong>Why:</strong> The realised range narrowed." in card
    details = card.index("<details")
    assert card.index("Agent proposes") < card.index("Effect now") < details
    # The YAML diff and the full rationale are still there, collapsed.
    assert card.index("split-diff") > details
    assert card.index("Long rationale tail.") > details
    assert "<details class=\"compact-details\" style=\"margin-top:10px\">" in card
    assert " open" not in card[details:details + 80]
    assert_english_only(card)
    client.__exit__(None, None, None)


def test_web_view_handles_kinds_and_resolved_rows():
    assert compact_cards.web_view({"kind": "shadow_edit"}) is None
    view = compact_cards.web_view(
        {"kind": "strategy_revision", "ticker": "TSLA", "strategy_id": "tsla-swing",
         "snapshot": {"old_yaml": OLD_TSLA, "new_yaml": "- broken", "rationale": "R."},
         "decision_brief": []})
    assert view["changes"] == [] and "could not be summarised" in view["header"]
    assert view["effect"] == "" and view["why"] == "R."


# ---------------------------------------------------------------------------
# research export
# ---------------------------------------------------------------------------

def test_research_export_tells_compact_from_classic(tmp_path):
    conn = connect(tmp_path / "t.db")
    queue = ReviewQueue(conn, None, account="shadow")
    kw = {"strategy_id": "s", "rule_id": "r", "ticker": "CRDO", "rule_type": "soft",
          "condition": "price <= 185", "action": "buy $1500", "snapshot": {},
          "intent": None}
    first, second = int(queue.add(**kw)), int(queue.add(**kw))
    queue.record_card_format(second, compact_cards.FORMAT)
    rows = rm.card_format_rows(conn, "2000-01-01", "2999-12-31")
    assert [(r["review_id"], r["account"], r["card_format"]) for r in rows] == [
        (first, "shadow", "classic"), (second, "shadow", "compact")]
    assert set(rows[0]) == set(rm.CARD_FORMAT_COLUMNS)
    assert queue.get(second)["status"] == "pending"

    from allpath_trade.research.cli import write_csv

    write_csv(tmp_path / "card_formats.csv", rm.CARD_FORMAT_COLUMNS, rows)
    with (tmp_path / "card_formats.csv").open() as fh:
        assert [r["card_format"] for r in csv.DictReader(fh)] == ["classic", "compact"]


def test_research_export_tolerates_a_db_without_the_column(tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE pending_reviews (id INTEGER PRIMARY KEY, account TEXT,"
                 " ts TEXT, kind TEXT, source TEXT, status TEXT)")
    conn.execute("INSERT INTO pending_reviews VALUES (1, 'paper',"
                 " '2026-11-10T15:00:00+00:00', 'order', 'sentinel', 'pending')")
    [row] = rm.card_format_rows(conn, "2026-11-01", "2026-11-30")
    assert row["card_format"] == "classic"


def test_cli_json_has_no_card_format_key_for_a_classic_card(tmp_path):
    from allpath_trade.cli import _review_to_json

    conn = connect(tmp_path / "t.db")
    queue = ReviewQueue(conn, None, account="paper")
    rid = int(queue.add(strategy_id="s", rule_id="r", ticker="AAPL", rule_type="soft",
                        condition="price < 205", action="buy $3000", snapshot={},
                        intent=None))
    assert "card_format" not in _review_to_json(queue.get(rid))
    queue.record_card_format(rid, compact_cards.FORMAT)
    assert _review_to_json(queue.get(rid))["card_format"] == "compact"


def test_share_counts_say_when_they_are_rounded():
    head = compact_cards.order_headline
    assert head(ticker="X", side="sell", qty="10.685", price="135.35") == (
        "X · Sell ~11 sh @ $135.35 (~$1,446)")
    assert head(ticker="X", side="sell", qty="20", price="10") == "X · Sell 20 sh @ $10 (~$200)"
    assert head(ticker="X", side="sell", qty="0.004", price="10") == (
        "X · Sell 0.004 sh @ $10 (~$0)")
    assert head(ticker="X", side="buy", notional="1500", price="150") == (
        "X · Buy $1,500 (~10 sh @ $150)")


def test_effect_now_does_not_guess_about_option_rules_and_follows_the_paper_regime(tmp_path):
    option = strategy_yaml(auth="auto", condition="price < 250", action="buy_call $500")
    s, _store, ex, _q, _notifier = make(tmp_path, strategy_yaml())
    assert s.revision_effect(option, "t") == (
        "at the current price ($200) the next check: r1 is true now (option rule: "
        "buy_call $500; what it does depends on market hours).")
    # Paper in the human regime: an auto strategy is treated as confirm,
    # the way run_once treats it.
    s.app_state = AppState(connect(tmp_path / "state.db"))
    auto = strategy_yaml(auth="auto", condition="price < 250")
    assert "runs without asking" in s.revision_effect(auto, "t")
    from allpath_trade.regime import regime_key

    s.app_state.set(regime_key("paper"), "human")
    assert "asks you to approve" in s.revision_effect(auto, "t")
    assert ex.calls == []


def test_flag_off_sentinel_never_computes_the_compact_reason(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("compact-only code ran with the flag off")

    s, _store, _ex, _q, notifier = make(tmp_path, strategy_yaml(auth="confirm"), qty="0")
    monkeypatch.setattr(s, "_no_order_reason", boom)
    monkeypatch.setattr(compact_cards, "rule_message", boom)
    assert s.run_once().outcomes[0].disposition == "skipped"
    assert notifier.sent[0][1].endswith("Disposition: skipped." + FOOTER)


def test_pending_page_survives_a_compact_view_that_raises(tmp_path, monkeypatch):
    client, queue = _shadow_client(tmp_path, monkeypatch)
    _add_order(queue)

    def boom(*a, **k):
        raise RuntimeError("bad row")

    monkeypatch.setattr(compact_cards, "web_view", boom)
    response = client.get("/reviews")
    assert response.status_code == 200
    card = _card(response.text, 1)
    assert "compact-card" not in response.text and "Agent recommends:" in card
    client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# nightly reflection revisions: compact brief wording, compact card recorded
# ---------------------------------------------------------------------------

# The long classic-wording brief of a real shadow reflection revision, as
# reported (one sentence, 33 words).
LONG_WHY = ("Replace CRDO's obsolete entry assumptions with one $500 proposal at or below "
            "$110, guarded by 2.8% pre-trade weight, because the owner's new $100-110 zone "
            "does not override the existing 3% target.")


class _ToolRecordingLLM(ScriptedLLM):
    def __init__(self, responses):
        super().__init__(responses)
        self.tools = []

    def complete(self, messages, tools=None):
        self.tools.append([t.model_dump() for t in tools or []])
        return super().complete(messages, tools)


def _reflection_run(tmp_path, account, **flags):
    """One nightly reflection; returns (first-call messages, tool specs)."""
    from allpath_trade.llm.base import LLMResponse
    from allpath_trade.reflect import Reflector
    from tests.test_reflect import NOW, REPORT_TEXT, make_components, make_settings

    components = make_components(tmp_path, account=account)
    llm = _ToolRecordingLLM([LLMResponse(text=REPORT_TEXT)])
    Reflector(llm=llm, components=components, settings=make_settings(**flags)).run_daily(now=NOW)
    return json.dumps(llm.seen[0], default=str), llm.tools[0]


def _revision_tool(specs):
    [spec] = [s for s in specs if s["name"] == "propose_strategy_revision"]
    return json.dumps(spec)


def test_paper_reflection_prompt_and_tools_ignore_the_shadow_flags(tmp_path):
    runs = {}
    for name, flags in (("off", {}),
                        ("shadow_on", {"shadow_compact_cards": True,
                                       "shadow_card_reason_brief": True})):
        (tmp_path / name).mkdir()
        runs[name] = _reflection_run(tmp_path / name, "paper", **flags)
    assert runs["off"][0].replace(str(tmp_path / "off"), "") == \
        runs["shadow_on"][0].replace(str(tmp_path / "shadow_on"), "")
    assert runs["off"][1] == runs["shadow_on"][1]
    assert "brief_" not in json.dumps(runs["off"][1])


def test_shadow_reflection_gets_the_compact_wording_only_with_both_flags(tmp_path):
    for name in ("brief", "both", "compact"):
        (tmp_path / name).mkdir()
    _, brief_only = _reflection_run(tmp_path / "brief", "shadow",
                                    shadow_card_reason_brief=True)
    messages, both = _reflection_run(tmp_path / "both", "shadow",
                                     shadow_card_reason_brief=True,
                                     shadow_compact_cards=True)
    _, compact_only = _reflection_run(tmp_path / "compact", "shadow",
                                      shadow_compact_cards=True)
    classic = card_brief.TOOL_PROPERTIES["brief_why"]["description"]
    compact = card_brief.COMPACT_REVISION_TOOL_PROPERTIES["brief_why"]["description"]
    assert classic in json.loads(_revision_tool(brief_only))["parameters"]["properties"][
        "brief_why"]["description"]
    props = json.loads(_revision_tool(both))["parameters"]["properties"]
    assert props["brief_why"]["description"] == compact
    assert props["brief_risk"] == card_brief.COMPACT_REVISION_TOOL_PROPERTIES["brief_risk"]
    assert "at most 20 words" in compact and "do not restate them" in compact
    assert "the reason for this change" in compact
    # Compact cards without the brief flag: no brief arguments at all.
    assert "brief_" not in _revision_tool(compact_only)
    # Only the argument descriptions differ; nothing else in the tool list.
    def strip(specs):
        specs = json.loads(json.dumps(specs))
        for spec in specs:
            for name in card_brief.TOOL_PROPERTIES:
                spec["parameters"].get("properties", {}).pop(name, None)
        return specs

    assert strip(both) == strip(brief_only) == strip(compact_only)
    assert_english_only(compact)


def test_reflection_tool_schema_with_compact_brief_off_is_the_old_one(tmp_path):
    from tests.test_card_brief import _reflection_tools, _specs

    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    off = _specs(_reflection_tools(tmp_path / "a")[0])
    # compact_brief alone does nothing: it needs card_brief.
    assert _specs(_reflection_tools(tmp_path / "b", compact_brief=True)[0]) == off
    on = _specs(_reflection_tools(tmp_path / "c", card_brief=True)[0])
    assert card_brief.TOOL_PROPERTIES["brief_why"]["description"] in json.dumps(on)


def test_reflection_revision_records_compact_only_for_a_compact_queue(tmp_path):
    from tests.test_card_brief import PROPOSED, _reflection_tools

    args = {"strategy_id": "s1", "new_yaml": PROPOSED,
            "rationale": "The stop at 100 is too tight; widen it to 90.",
            "brief_why": "Normal swings keep hitting the stop before the thesis can play out.",
            "brief_risk": "A wider stop means a larger loss if it triggers."}
    (tmp_path / "off").mkdir()
    reg, queue = _reflection_tools(tmp_path / "off", card_brief=True, compact_brief=True)
    assert "queued" in _call(reg, "propose_strategy_revision", **args)
    assert queue.get(1)["card_format"] is None  # flag off for the queue: nothing written

    (tmp_path / "on").mkdir()
    reg, queue = _reflection_tools(tmp_path / "on", card_brief=True, compact_brief=True)
    queue.compact_cards = True
    assert "queued" in _call(reg, "propose_strategy_revision", **args)
    row = queue.get(1)
    assert row["card_format"] == "compact" and row["status"] == "pending"
    assert row["source"] == "reflection"
    # Same grounding check: this brief has no figures and is kept whole...
    assert json.loads(row["decision_brief"])["why"] == args["brief_why"]
    assert row["brief_drop"] is None
    # ...and one with a figure the stored review does not back is still dropped.
    out = _call(reg, "propose_strategy_revision", **{
        **args, "new_yaml": PROPOSED.replace("version: 2", "version: 3"),
        "brief_why": "Volatility rose 55% this month."})
    assert "queued" in out
    row = queue.get(2)
    assert row["decision_brief"] is None and row["card_format"] == "compact"
    assert json.loads(row["brief_drop"])["missing"] == ["55"]


def test_recording_the_format_can_never_block_a_reflection_revision(tmp_path, capsys):
    from tests.test_card_brief import PROPOSED, _reflection_tools

    reg, queue = _reflection_tools(tmp_path)
    queue.compact_cards = True

    def boom(*a, **k):
        raise RuntimeError("disk full")

    queue.record_card_format = boom
    out = _call(reg, "propose_strategy_revision", strategy_id="s1", new_yaml=PROPOSED,
                rationale="Widen the stop.")
    assert "queued" in out and queue.get(1)["status"] == "pending"
    assert "could not record card format" in capsys.readouterr().err


def test_no_notification_is_sent_for_a_reflection_revision(tmp_path, monkeypatch):
    """Routing is unchanged: a compact queue still pushes nothing here."""
    from tests.test_card_brief import PROPOSED, _reflection_tools

    def boom(*a, **k):
        raise AssertionError("a reflection revision sent a notification")

    monkeypatch.setattr(dispatch, "notify_review_queued", boom)
    monkeypatch.setattr(dispatch, "push_telegram_review_queued", boom)
    reg, queue = _reflection_tools(tmp_path)
    queue.compact_cards = True
    assert "queued" in _call(reg, "propose_strategy_revision", strategy_id="s1",
                             new_yaml=PROPOSED, rationale="Widen the stop.")


def test_pending_page_cuts_an_already_stored_long_reflection_brief(tmp_path, monkeypatch):
    """Rows queued before the compact wording existed (long classic brief,
    card_format NULL) render in the compact layout, lines cut by the cap."""
    client, queue = _shadow_client(tmp_path, monkeypatch, shadow_card_reason_brief=True)
    c = client.app.state.holder.get().accounts["shadow"]
    (c.strategies.directory / "tsla-swing.yaml").write_text(OLD_TSLA)
    rid = int(queue.add_strategy_revision(
        strategy_id="tsla-swing", ticker="TSLA", old_yaml=OLD_TSLA, new_yaml=NEW_TSLA,
        diff="", rationale="Long rationale. With a tail.", source="reflection"))
    long_risk = "First risk sentence. Second risk sentence that must not show."
    queue.attach_brief(rid, {"why": LONG_WHY, "risk": long_risk, "plan": "",
                             "origin": "reflection"})
    assert queue.get(rid)["card_format"] is None
    card = _card(client.get("/reviews").text, rid)
    assert "compact-card" in card or "Agent proposes 2 changes" in card
    cut = compact_cards.one_sentence(LONG_WHY)
    assert cut == ("Replace CRDO's obsolete entry assumptions with one $500 proposal at or "
                   "below $110, guarded by 2.8% pre-trade weight, because the owner's new "
                   "$100-110 zone does not override the…")
    assert len(cut.split()) == compact_cards.MAX_WORDS
    import html as html_lib

    text = html_lib.unescape(card)
    assert f"<strong>Why:</strong> {cut}</p>" in text
    assert "<strong>Risk:</strong> First risk sentence.</p>" in text
    assert "Second risk sentence" not in text and "existing 3% target" not in text
    assert text.index("Agent proposes 2 changes") < text.index("Why:") < text.index("<details")
    # The stored brief is not modified by rendering.
    assert json.loads(queue.get(rid)["decision_brief"])["why"] == LONG_WHY
    assert queue.get(rid)["card_format"] is None  # a page view writes nothing
    client.__exit__(None, None, None)
