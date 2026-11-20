"""PRE_TRADE_CONSULT and ACT_PROMPT_CARDS (allpath_trade/advisor.py): the
shadow account as an advisor for the owner's real account. A stated intent
to trade gets a code-computed check; a real trade is booked at the owner's
own fill; a fired rule becomes a prompt with Done / Skipped / Discuss.
Every ticker, price and amount here is invented."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from allpath_trade import advisor
from allpath_trade.agent.review import ReviewAnalysis
from allpath_trade.agent.shadow_tools import apply_shadow_edit_factory, full_snapshot
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.broker.shadow import ShadowLedger
from allpath_trade.config import account_flag
from allpath_trade.data.base import Bar, DataSource, Quote
from allpath_trade.execution import Executor
from allpath_trade.llm.base import ToolCall
from allpath_trade.notify import dispatch
from allpath_trade.risk.gate import RiskGate, limits_for_account
from allpath_trade.sentinel import Sentinel
from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, AppState
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal
from allpath_trade.store.order_threads import OrderThreadStore
from allpath_trade.store.reviews import ReviewQueue, RevisionValidationError
from allpath_trade.store.views import ViewStore
from allpath_trade.strategy.store import StrategyStore
from allpath_trade.telegram import TelegramPoller
from tests.test_agent_loop import ScriptedLLM
from tests.test_card_brief import SpyNotifier, _client, _settings
from tests.test_telegram_poller import (
    FakeChatService,
    FakeTelegramAPI,
    _callback_update,
    pair,
)

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)

STRATEGY = """
name: "ZZZ swing"
status: active
authorization: {auth}
thesis: "Own it while margins expand; cut it if the range breaks."
horizon: long
position: {{ticker: ZZZ, target_weight: 10%, max_weight: 30%}}
rules:
  - {{id: stop, type: hard, condition: "position_weight > 0 and price < 90", action: "sell all"}}
  - {{id: dip, type: soft, condition: "price < 95 and position_weight < 0.01", action: "buy $500"}}
  - {{id: trim, type: hard, condition: "price > 140", action: "sell 25%"}}
review: {{cadence: daily, invalidation: "Two quarters of shrinking margins"}}
"""


class Data(DataSource):
    def __init__(self, price, closes=None):
        self.price = Decimal(str(price))
        self.closes = closes or []

    def get_quote(self, ticker):
        return Quote(ticker=ticker, price=self.price, as_of=NOW)

    def get_bars(self, ticker, days=365):
        start = NOW - timedelta(days=len(self.closes))
        return [Bar(ts=start + timedelta(days=i), open=c, high=c, low=c, close=c, volume=1)
                for i, c in enumerate(self.closes)]


def _world(tmp_path, *, price="100", closes=None, auth="confirm", cash="10000",
           held=("ZZZ", "20", "80"), strategy=True):
    """A shadow ledger with made-up holdings, a strategy store and a queue."""
    conn = connect(tmp_path / "db.sqlite")
    data = Data(price, closes)
    ledger = ShadowLedger(conn, data)
    ledger.set_cash(Decimal(cash))
    if held:
        ledger.set_position(held[0], Decimal(held[1]), Decimal(held[2]))
    (tmp_path / "strategies").mkdir(exist_ok=True)
    if strategy:
        (tmp_path / "strategies" / "zzz-swing.yaml").write_text(STRATEGY.format(auth=auth))
    store = StrategyStore(tmp_path / "strategies", conn, account="shadow")
    gate = RiskGate(limits_for_account(_settings(), "shadow"))
    executor = Executor(ledger, gate, TradeJournal(conn, account="shadow"), data)
    queue = ReviewQueue(conn, executor, account="shadow")
    queue.set_shadow_edit_applier(apply_shadow_edit_factory(ledger, conn))
    return SimpleNamespace(conn=conn, data=data, ledger=ledger, store=store, queue=queue,
                           executor=executor, views=ViewStore(conn, "shadow"))


def _consult(w, side, **kw):
    return advisor.consult("ZZZ", side, broker=w.ledger, data=w.data, strategies=w.store,
                           views=w.views, max_position_weight=Decimal("0.25"),
                           min_cash_reserve=Decimal(0), now=NOW, **kw)


# --- flags ------------------------------------------------------------------

def test_flags_default_off_and_are_per_account():
    s = _settings()
    for name in (advisor.FLAG, advisor.ACT_FLAG):
        assert account_flag(s, name, "shadow") is False
    s = _settings(shadow_pre_trade_consult=True, shadow_act_prompt_cards=True)
    for name in (advisor.FLAG, advisor.ACT_FLAG):
        assert account_flag(s, name, "shadow") is True and account_flag(s, name, "paper") is False


def test_paper_is_unchanged_with_every_new_shadow_flag_on(tmp_path, monkeypatch):
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: ScriptedLLM([]))
    everything = {"shadow_pre_trade_consult": True, "shadow_act_prompt_cards": True,
                  "shadow_holdings_sync": True, "shadow_agent_language": "zh"}

    def paper(sub, **flags):
        (tmp_path / sub).mkdir()
        client = _client(tmp_path / sub, monkeypatch, **flags)
        c = client.app.state.holder.get()
        session = client.app.state.chat_services["paper"].session()
        shadow_session = client.app.state.chat_services["shadow"].session()
        out = SimpleNamespace(
            tools=[json.dumps(s.model_dump(), sort_keys=True, default=str)
                   for s in session.registry.specs()],
            prompt=session.system_prompt.replace(str(tmp_path / sub), ""),
            act=c.accounts["paper"].queue.act_prompts,
            lang=c.accounts["paper"].queue.language,
            shadow_tools={s.name for s in shadow_session.registry.specs()},
            shadow_prompt=shadow_session.system_prompt,
            shadow_act=c.accounts["shadow"].queue.act_prompts)
        client.__exit__(None, None, None)
        return out

    off, on = paper("off"), paper("on", **everything)
    assert on.tools == off.tools and on.prompt == off.prompt
    assert on.act is False and on.lang == "en"
    new_tools = {"pre_trade_check", "record_real_trade", "propose_holdings_sync"}
    assert new_tools <= on.shadow_tools and not (new_tools & off.shadow_tools)
    assert advisor.PRE_TRADE_CHAT_NOTE in on.shadow_prompt
    assert "Pre-trade consult." not in off.shadow_prompt
    assert on.shadow_act is True and off.shadow_act is False


# --- the consult: structure and impulse flags ---------------------------------

def test_a_planned_trade_inside_limits_has_no_flags_and_a_go_floor(tmp_path):
    w = _world(tmp_path, price="100", closes=[98, 99, 100, 99, 100, 99])
    c = _consult(w, "buy", qty="5")
    assert c.flags == [] and c.floor == "go"
    assert c.plan["strategy_id"] == "zzz-swing" and "margins expand" in c.plan["thesis"]
    assert c.value == Decimal(500) and c.cash_after == Decimal(9500)
    assert c.weight_before == Decimal(2000) / Decimal(12000)
    assert c.weight_after == Decimal(2500) / Decimal(12000) and c.weight_limit == Decimal("0.25")
    kinds = {(lv["rule_id"], lv["kind"]) for lv in c.levels}
    assert kinds == {("stop", "stop"), ("dip", "entry"), ("trim", "take_profit")}
    stop = next(lv for lv in c.levels if lv["rule_id"] == "stop")
    assert stop["level"] == Decimal(90) and stop["distance_pct"] == Decimal("11.1")
    assert c.moves["day_pct"] == Decimal("1.0")
    text = advisor.render_consult(c)
    for part in ("PRE-TRADE CHECK", "VERDICT FLOOR: GO", "IMPULSE / RISK FLAGS: none",
                 "PLAN: strategy zzz-swing", "POSITION: 20 sh, avg $80", "CASH: $10,000 -> $9,500",
                 "- stop zzz-swing/stop: price < $90 -> sell all [armed]", "RECENT MOVE: today +1.0%",
                 "UPCOMING EVENTS:"):
        assert part in text, part


def test_no_plan_and_chasing_give_a_wait_floor(tmp_path):
    w = _world(tmp_path, price="108", closes=[100, 100, 100], strategy=False, held=None)
    c = _consult(w, "buy", notional="1080")
    assert c.flag_codes == ["no_plan", "chasing"] and c.floor == "wait"
    assert c.qty == Decimal(10) and "up 8.0% today" in c.flags[1]["text"]
    assert "PLAN: no active strategy for this name" in advisor.render_consult(c)
    # A recorded view for the name counts as a plan; a long-term VISION is shown too.
    w.views.add(subject="ZZZ", stance="accumulate under 110", horizon="1y", conviction="high",
                invalidation="margin drop", source="chat", expires_days=30)
    w.views.add(subject="VISION", stance="own compounders for ten years", horizon="long-term",
                conviction="high", invalidation="none", source="chat", expires_days=180)
    c = advisor.consult("ZZZ", "buy", notional="1080", broker=w.ledger, data=w.data,
                        strategies=w.store, views=w.views)
    assert "no_plan" not in c.flag_codes and len(c.views) == 2
    # Selling into a sharp drop is the same flag.
    (tmp_path / "x").mkdir()
    w2 = _world(tmp_path / "x", price="90", closes=[100, 100])
    assert "chasing" in _consult(w2, "sell", qty="5").flag_codes


def test_buying_below_your_own_stop_or_right_after_a_stop_out_is_a_dont(tmp_path):
    w = _world(tmp_path, price="85", closes=[86, 86, 85])
    c = _consult(w, "buy", qty="5")
    assert "below_own_stop" in c.flag_codes and c.floor == "dont"
    assert "below your own stop zzz-swing/stop at $90" in c.flags[0]["text"]
    w.data.price, w.data.closes = Decimal(100), [100, 100]
    w.store.record_fire("zzz-swing", "stop", NOW - timedelta(days=2))
    c = _consult(w, "buy", qty="5")
    assert c.flag_codes == ["rebuy_after_stop"] and c.floor == "dont"
    assert (NOW - timedelta(days=2)).date().isoformat() in c.flags[0]["text"]
    later = advisor.consult("ZZZ", "buy", qty="5", broker=w.ledger, data=w.data,
                            strategies=w.store, now=NOW + timedelta(days=30))
    assert "rebuy_after_stop" not in later.flag_codes


def test_oversize_cash_short_and_oversell(tmp_path):
    w = _world(tmp_path, price="100", cash="1000")
    c = _consult(w, "buy", qty="30")
    assert c.flag_codes == ["oversize", "cash_short"] and c.floor == "dont"
    c = _consult(w, "buy", qty="1")
    assert c.flag_codes == ["oversize"] and c.floor == "wait"     # 2,100 / 3,000 > 25%
    c = _consult(w, "sell", qty="21")
    assert c.flag_codes == ["oversell"] and c.floor == "dont"
    assert _consult(w, "sell", qty="20").flags == []


def test_tool_logs_each_consult_without_free_text(tmp_path):
    w = _world(tmp_path, price="108", closes=[100, 100])
    registry = ToolRegistry()
    advisor.register_advisor_tools(
        registry, ledger=w.ledger, data=w.data, queue=w.queue, strategies=w.store,
        views=w.views, conn=w.conn, settings=_settings(), conversation_id_fn=lambda: 3)
    out = registry.execute(ToolCall(id="x", name="pre_trade_check",
                                    arguments={"ticker": "zzz", "side": "buy", "qty": "5"}))
    assert out.startswith("PRE-TRADE CHECK #1: BUY ZZZ 5 sh @ $108 (current quote) = $540")
    assert "VERDICT FLOOR: WAIT" in out and "- chasing:" in out
    [row] = [dict(r) for r in w.conn.execute("SELECT * FROM trade_consults")]
    assert (row["ticker"], row["side"], row["floor"], row["conversation_id"]) == (
        "ZZZ", "buy", "wait", 3)
    assert json.loads(row["flags"]) == ["chasing"] and row["outcome"] is None
    assert registry.execute(ToolCall(id="x", name="pre_trade_check", arguments={
        "ticker": "zzz", "side": "hold"})) == "error: side must be buy or sell"
    assert full_snapshot(w.ledger)["cash"] == "10000"


# --- a real trade is booked at the owner's fill ---------------------------------

def _tools(w, lang="en"):
    w.queue.language = lang
    registry = ToolRegistry()
    notifier = SpyNotifier()
    advisor.register_advisor_tools(
        registry, ledger=w.ledger, data=w.data, queue=w.queue, strategies=w.store,
        views=w.views, conn=w.conn, settings=_settings(), conversation_id_fn=lambda: 3,
        notifier=notifier)

    def call(name, **kw):
        return registry.execute(ToolCall(id="x", name=name, arguments=kw))

    return call, notifier


def test_real_trade_is_queued_then_booked_at_his_price_not_the_quote(tmp_path):
    w = _world(tmp_path, price="100")
    call, notifier = _tools(w)
    call("pre_trade_check", ticker="ZZZ", side="buy", qty="10")
    before = full_snapshot(w.ledger)
    out = call("record_real_trade", ticker="ZZZ", side="buy", qty="10", price="$97.25",
               consult_id="1")
    assert out.startswith("QUEUED as #1")
    assert full_snapshot(w.ledger) == before, "nothing is booked before the confirmation"
    [(subject, body)] = notifier.sent
    assert body.split("\n")[:4] == ["Record real trade: BUY 10 ZZZ @ $97.25", "Total $972.50",
                                    "Position: 20 → 30 sh", "Cash: $10,000 → $9,027.50"]
    assert "The app places no order." in body
    w.queue.approve(1)
    after = full_snapshot(w.ledger)
    assert Decimal(after["cash"]) == Decimal("9027.50")
    assert Decimal(after["positions"]["ZZZ"]["qty"]) == 30
    assert Decimal(after["positions"]["ZZZ"]["avg_cost"]) == (
        Decimal(20 * 80) + Decimal("972.5")) / 30
    [order] = w.ledger.get_orders(open_only=False)
    assert order.filled_avg_price == Decimal("97.25") and order.filled_qty == Decimal(10)
    note = w.conn.execute("SELECT note FROM shadow_orders WHERE id = ?",
                          (int(order.id),)).fetchone()["note"]
    assert note == "real fill reported by the owner"
    assert w.conn.execute("SELECT outcome FROM trade_consults").fetchone()["outcome"] == "recorded"
    # A sell, in Chinese.
    call, notifier = _tools(w, lang="zh")
    call("record_real_trade", ticker="ZZZ", side="sell", qty="30", price="101")
    assert notifier.sent[0][1].split("\n")[0] == "记录真实成交：卖出 30 ZZZ @ $101"
    w.queue.approve(2)
    assert "ZZZ" not in full_snapshot(w.ledger)["positions"]


def test_a_trade_the_ledger_cannot_hold_is_refused_and_staleness_is_checked(tmp_path):
    w = _world(tmp_path, price="100", cash="100")
    call, _notifier = _tools(w)
    assert "sync your holdings" in call("record_real_trade", ticker="ZZZ", side="sell",
                                        qty="25", price="100")
    assert "less than the" in call("record_real_trade", ticker="ZZZ", side="buy",
                                   qty="5", price="100")
    assert call("record_real_trade", ticker="ZZZ", side="buy", qty="0", price="1").startswith(
        "error")
    assert w.queue.list() == []
    call("record_real_trade", ticker="ZZZ", side="sell", qty="5", price="100")
    w.ledger.set_cash(Decimal(5))
    with pytest.raises(RevisionValidationError):
        w.queue.approve(1)
    with pytest.raises(ValueError):
        w.ledger.record_real_fill("ZZZ", "sell", Decimal(99), Decimal(1))
    assert Decimal(full_snapshot(w.ledger)["positions"]["ZZZ"]["qty"]) == 20


# --- act-prompt cards: Done @ price / Skipped / Discuss -------------------------

class _Agent:
    def analyze(self, review):
        analysis = ReviewAnalysis(recommendation="execute", reasoning="The stop level broke.")
        analysis.brief = None
        return analysis


class _CardAPI:
    sent: list = []

    def __init__(self, token):
        pass

    def send_message(self, chat_id, html, reply_markup=None):
        _CardAPI.sent.append((html, reply_markup))
        return True

    def send_message_id(self, chat_id, html, reply_markup=None):
        _CardAPI.sent.append((html, reply_markup))
        return 500 + len(_CardAPI.sent)


def _fire(tmp_path, monkeypatch, *, auth="confirm", act=True, lang="en", threads=False):
    """The stop rule fires at 88 on a shadow account holding 20 ZZZ."""
    monkeypatch.setattr(dispatch, "TelegramAPI", _CardAPI)
    _CardAPI.sent = []
    w = _world(tmp_path, price="88", auth=auth)
    w.queue.act_prompts = act
    w.queue.language = lang
    w.queue.compact_cards = True
    if threads:
        w.queue.set_telegram_card_recorder(OrderThreadStore(w.conn, "shadow").add_card)
    app_state = AppState(w.conn)
    app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
    notifier = SpyNotifier()
    sentinel = Sentinel(w.store, w.data, w.ledger, w.executor, w.queue, notifier,
                        app_state=app_state, telegram_bot_token="t", account="shadow")
    sentinel.review_agent = _Agent()
    report = sentinel.run_once()
    w.app_state, w.notifier, w.report = app_state, notifier, report
    return w


def _poller(w, api, chat=None):
    pair(w.app_state, "111")
    bundle = SimpleNamespace(queue=w.queue, strategies=w.store, broker=w.ledger)
    paper = SimpleNamespace(queue=ReviewQueue(w.conn, None, account="paper"), strategies=w.store)
    components = SimpleNamespace(
        app_state=w.app_state, conn=w.conn, queue=paper.queue, strategies=w.store,
        settings=SimpleNamespace(web_token="tok", shadow_telegram_order_threads=True),
        accounts={"paper": paper, "shadow": bundle})
    holder = SimpleNamespace(get=lambda: components)
    return TelegramPoller(api, chat or FakeChatService(), holder, threading.Event())


def test_fired_rule_is_a_prompt_to_act_with_three_buttons(tmp_path, monkeypatch):
    w = _fire(tmp_path, monkeypatch, auth="auto")
    # auto would have recorded the sale at once; with act prompts nothing is booked.
    assert [o.disposition for o in w.report.outcomes] == ["queued"]
    assert Decimal(full_snapshot(w.ledger)["positions"]["ZZZ"]["qty"]) == 20
    [(html, markup)] = _CardAPI.sent
    assert html.startswith("[Shadow] ZZZ · Sell 20 sh @ $88")
    assert ("Your move: place this at your broker. Then tap Done (books it at $88), or reply "
            "to this message with your real fill price.") in html
    assert "approving only records it" not in html
    buttons = markup["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["✅ Done @ $88", "⏭ Skipped", "💬 Discuss"]
    nonce = w.queue.get(1)["approval_token_hash"][:16]
    assert [b["callback_data"] for b in buttons] == [
        f"rv:approve:1:{nonce}", f"rv:reject:1:{nonce}", f"rv:discuss:1:{nonce}"]
    [(_subject, email)] = w.notifier.sent
    assert "Your move: place this at your broker." in email


def test_without_the_flag_the_card_and_buttons_are_the_old_ones(tmp_path, monkeypatch):
    w = _fire(tmp_path, monkeypatch, act=False)
    [(html, markup)] = _CardAPI.sent
    assert "Shadow: approving only records it; you place the order yourself." in html
    assert [b["text"] for b in markup["inline_keyboard"][0]] == ["✅ Approve", "❌ Reject"]
    assert "Your move" not in html


def test_chinese_act_card(tmp_path, monkeypatch):
    _fire(tmp_path, monkeypatch, lang="zh")
    [(html, markup)] = _CardAPI.sent
    assert html.startswith("[Shadow] ZZZ · 卖出 20 股 @ $88")
    assert "轮到你了：请在券商下这笔单。然后点「已成交」（按 $88 记账）" in html
    assert [b["text"] for b in markup["inline_keyboard"][0]] == [
        "✅ 已成交 @ $88", "⏭ 没做", "💬 讨论"]


def test_done_books_the_fill_at_the_button_price_even_if_the_quote_moved(tmp_path, monkeypatch):
    w = _fire(tmp_path, monkeypatch)
    nonce = w.queue.get(1)["approval_token_hash"][:16]
    w.data.price = Decimal("86.40")                  # the market moved before the tap
    api = FakeTelegramAPI(batches=[[_callback_update(1, 111, 111, f"rv:approve:1:{nonce}")]])
    _poller(w, api).poll_once()
    assert w.queue.get(1)["status"] == "approved"
    [order] = w.ledger.get_orders(open_only=False)
    assert order.filled_avg_price == Decimal(88) and order.filled_qty == Decimal(20)
    assert Decimal(full_snapshot(w.ledger)["cash"]) == Decimal(10000 + 20 * 88)
    assert api.answered_callbacks == [("cb1", "[Shadow] Booked")]
    [(_chat, line)] = api.sent_messages
    assert line == ("[Shadow] ✅ Booked #1: SELL 20 ZZZ @ $88 in your shadow ledger. "
                    "Fill different? Reply to the card with your price.")


def test_skipped_leaves_the_ledger_alone(tmp_path, monkeypatch):
    w = _fire(tmp_path, monkeypatch)
    before = full_snapshot(w.ledger)
    nonce = w.queue.get(1)["approval_token_hash"][:16]
    api = FakeTelegramAPI(batches=[[_callback_update(1, 111, 111, f"rv:reject:1:{nonce}")]])
    _poller(w, api).poll_once()
    assert w.queue.get(1)["status"] == "rejected" and full_snapshot(w.ledger) == before
    assert api.sent_messages[0][1] == "[Shadow] ⏭ Skipped #1. The ledger is unchanged."
    assert api.answered_callbacks == [("cb1", "[Shadow] Skipped")]


def test_discuss_starts_a_chat_turn_about_the_card_and_resolves_nothing(tmp_path, monkeypatch):
    w = _fire(tmp_path, monkeypatch)
    nonce = w.queue.get(1)["approval_token_hash"][:16]
    update = _callback_update(1, 111, 111, f"rv:discuss:1:{nonce}", message_id=42)
    update["callback_query"]["message"]["text"] = "ZZZ · Sell 20 sh @ $88"
    api, chat = FakeTelegramAPI(batches=[[update]]), FakeChatService(reply="here is why")
    poller = _poller(w, api, chat)
    poller._chat_services = {"shadow": chat, "paper": chat}
    poller.poll_once()
    assert w.queue.get(1)["status"] == "pending" and api.edited_markups == []
    [turn] = chat.calls
    assert turn["text"].startswith("[Order thread: review #1 (Shadow)]")
    assert "He tapped Discuss" in turn["text"] and "ZZZ · Sell 20 sh @ $88" in turn["text"]
    assert api.answered_callbacks == [("cb1", "Let's talk it through.")]
    assert api.sent_messages[-1][1].endswith("here is why")
    # A stale or foreign nonce does nothing.
    api2 = FakeTelegramAPI(batches=[[_callback_update(2, 111, 111, "rv:discuss:1:" + "0" * 16)]])
    poller2 = _poller(w, api2, chat)
    poller2.poll_once()
    assert len(chat.calls) == 1


def _reply(text, to_message_id, update_id=5):
    return {"update_id": update_id, "message": {
        "message_id": 900 + update_id, "chat": {"id": 111, "type": "private"},
        "from": {"id": 111}, "text": text,
        "reply_to_message": {"message_id": to_message_id, "text": "card"}}}


def test_replying_with_a_price_books_that_price_and_can_correct_a_done(tmp_path, monkeypatch):
    w = _fire(tmp_path, monkeypatch, threads=True)
    card_id = w.conn.execute("SELECT telegram_message_id m FROM telegram_cards").fetchone()["m"]
    chat = FakeChatService()
    api = FakeTelegramAPI(batches=[[_reply("成交 87.10", card_id)]])
    _poller(w, api, chat).poll_once()
    assert w.queue.get(1)["status"] == "approved" and chat.calls == []
    [order] = w.ledger.get_orders(open_only=False)
    assert order.filled_avg_price == Decimal("87.10")
    assert api.sent_messages[0][1] == (
        "[Shadow] ✅ Booked #1: SELL 20 ZZZ @ $87.10 in your shadow ledger.")
    # A second reply corrects the booked fill.
    api = FakeTelegramAPI(batches=[[_reply("@ $87.35", card_id, update_id=6)]])
    _poller(w, api, chat).poll_once()
    assert w.ledger.get_orders(open_only=False)[0].filled_avg_price == Decimal("87.35")
    assert Decimal(full_snapshot(w.ledger)["cash"]) == Decimal(10000) + 20 * Decimal("87.35")
    assert api.sent_messages[0][1] == "[Shadow] ✅ #1 corrected: fill price is now $87.35."


def test_a_question_under_the_card_is_still_an_order_thread(tmp_path, monkeypatch):
    w = _fire(tmp_path, monkeypatch, threads=True)
    card_id = w.conn.execute("SELECT telegram_message_id m FROM telegram_cards").fetchone()["m"]
    chat = FakeChatService()
    api = FakeTelegramAPI(batches=[[_reply("why sell all 20 at 88?", card_id)]])
    poller = _poller(w, api, chat)
    poller._chat_services = {"shadow": chat, "paper": chat}
    poller.poll_once()
    assert w.queue.get(1)["status"] == "pending"
    assert chat.calls and chat.calls[0]["text"].startswith("[Order thread: review #1")


def test_price_reply_parser():
    for text, want in (("135.10", "135.10"), ("done 135.1", "135.1"), ("成交 135.1", "135.1"),
                       ("@ $1,350.25", "1350.25"), ("Filled at 99", "99"), ("已成交：88.5。", "88.5")):
        assert advisor.price_from_reply(text) == Decimal(want), text
    for text in ("why 135?", "sell half at 135 instead", "", "0", "no", "135 or 136"):
        assert advisor.price_from_reply(text) is None, text


def test_consult_export_has_codes_only(tmp_path):
    from allpath_trade.research import regime_metrics

    w = _world(tmp_path, price="108", closes=[100, 100])
    call, _notifier = _tools(w)
    call("pre_trade_check", ticker="ZZZ", side="buy", qty="5")
    w.conn.execute("UPDATE trade_consults SET ts = '2026-11-20T15:00:00+00:00'")
    [row] = regime_metrics.trade_consult_rows(w.conn, "2026-11-01", "2026-11-30")
    assert (row["side"], row["floor"], row["flags"], row["n_flags"], row["outcome"],
            row["shadow_segment"]) == ("buy", "wait", "chasing", 1, "", "pre_sync")
    assert "ZZZ" not in json.dumps(row)
    assert regime_metrics.trade_consult_rows(connect(":memory:"), "2026-11-01",
                                             "2026-11-30") == []
