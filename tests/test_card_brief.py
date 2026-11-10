"""CARD_REASON_BRIEF / SHADOW_CARD_REASON_BRIEF: a three-line decision brief
(why, risk, plan) under the action line of a review card. Off by default;
with the flag off card text, prompts and tool schemas are what they were
(paper runs a live experiment). All sample text here is made up."""

from __future__ import annotations

import csv
import hashlib
import json
from decimal import Decimal

from fastapi.testclient import TestClient

from allpath_trade import card_brief
from allpath_trade.agent.action_tools import register_action_tools
from allpath_trade.agent.reflection_tools import register_reflection_tools
from allpath_trade.agent.review import PROMPT, ReviewAgent, ReviewAnalysis
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.cli import _review_to_json
from allpath_trade.config import Settings, account_flag
from allpath_trade.llm.base import LLMResponse, ToolCall
from allpath_trade.notify import dispatch, events
from allpath_trade.order_threads import ORDER_THREAD_CARD_HINT
from allpath_trade.risk.gate import RiskGate, RiskLimits
from allpath_trade.store.app_state import AppState
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal
from allpath_trade.store.order_threads import OrderThreadStore
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.strategy.store import StrategyStore
from allpath_trade.web.app import create_app
from allpath_trade.web.order_sink import QueueingOrderSink
from tests.helpers import CONFIGURED_KEYS, assert_english_only
from tests.test_agent_loop import ScriptedLLM
from tests.test_order_threads import IdAPI
from tests.test_sentinel import FakeBroker, FakeData, make, strategy_yaml
from tests.test_web_dashboard import FakeDataSource

BRIEF = {"why": "Price 204 is under the 205 buy trigger.",
         "risk": "The dip continues after weak guidance.",
         "plan": "Fits the plan to add on pullbacks.",
         "origin": "review_agent"}
BRIEF_LINES = ["Why: Price 204 is under the 205 buy trigger.",
               "Risk: The dip continues after weak guidance.",
               "Plan: Fits the plan to add on pullbacks."]

REVIEW = {"id": 1, "strategy_id": "s", "rule_id": "r1", "ticker": "AAPL",
          "rule_type": "soft", "condition": "price < 205", "action": "buy $3000",
          "snapshot": json.dumps({"price": "204"})}

FOOTER = ("\n\nOpen the AllPath Trade dashboard to act on this. This message never "
          "carries your dashboard access token. If you've opted into approval links "
          "under Settings, an item waiting for you may also include a one-time link "
          "scoped to just that item, valid for 24 hours and dead as soon as it's used.")

ORDER_KW = {"account": "shadow", "review_id": 12, "ticker": "NVDA", "action": "buy $2000",
                "strategy_id": "nvda-ai-infra", "recommendation": "execute — orderly pullback",
                "trigger_price": "$207.30", "est_shares": "9.65",
                "approve_url": "https://x.test/a/12?k=tok", "kind": "order"}
REVISION_KW = {"account": "paper", "review_id": 2, "ticker": "AAPL",
                   "action": "revise strategy 'aapl-services' to v3",
                   "strategy_id": "aapl-services", "kind": "strategy_revision"}

# The bodies main (b960601) produced for these two calls, verbatim.
ORDER_BODY_BEFORE = (
    "Item #12 is waiting for you.\n"
    "Proposed: buy $2000 on NVDA\n"
    "Strategy: nvda-ai-infra\n"
    "Price at trigger: $207.30\n"
    "Est. size: ~9.65 shares at that price\n"
    "The agent recommends: execute — orderly pullback\n"
    "If approved, you'll place it yourself — approving here only records it in "
    "your shadow ledger.\n"
    "Review & approve: https://x.test/a/12?k=tok" + FOOTER)
REVISION_BODY_BEFORE = (
    "Item #2 is waiting for you.\n"
    "Proposed: revise strategy 'aapl-services' to v3 on AAPL\n"
    "Strategy: aapl-services" + FOOTER)
# sha256 of agent/review.py's PROMPT template on main (b960601).
REVIEW_PROMPT_SHA = "ccec831e3e962a99a17f8b17b51884b70b02bd0e01ab6a067fb295f6597a388b"


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


# ---------------------------------------------------------------------------
# flag
# ---------------------------------------------------------------------------

def test_flag_defaults_off_and_shadow_flag_only_turns_on_shadow(monkeypatch):
    s = _settings()
    assert account_flag(s, card_brief.FLAG, "paper") is False
    assert account_flag(s, card_brief.FLAG, "shadow") is False
    s = _settings(shadow_card_reason_brief=True)
    assert account_flag(s, card_brief.FLAG, "shadow") is True
    assert account_flag(s, card_brief.FLAG, "paper") is False
    monkeypatch.setenv("SHADOW_CARD_REASON_BRIEF", "true")
    s = _settings()
    assert s.shadow_card_reason_brief is True and s.card_reason_brief is False


# ---------------------------------------------------------------------------
# card text
# ---------------------------------------------------------------------------

def test_card_text_without_a_brief_is_byte_identical_to_before():
    assert events.review_queued(**ORDER_KW)[1] == ORDER_BODY_BEFORE
    assert events.review_queued(**ORDER_KW, brief=None)[1] == ORDER_BODY_BEFORE
    assert events.review_queued(**REVISION_KW)[1] == REVISION_BODY_BEFORE
    assert events.review_queued(**REVISION_KW, brief=None)[1] == REVISION_BODY_BEFORE


def test_order_card_with_a_brief_is_the_short_layout():
    subject, body = events.review_queued(**ORDER_KW, brief=BRIEF)
    assert body == (
        "Item #12 is waiting for you.\n"
        "Proposed: buy $2000 on NVDA\n"
        "Strategy: nvda-ai-infra\n"
        "Price at trigger: $207.30\n"
        "Est. size: ~9.65 shares at that price\n"
        "Why: Price 204 is under the 205 buy trigger.\n"
        "Risk: The dip continues after weak guidance.\n"
        "Plan: Fits the plan to add on pullbacks.\n"
        "The agent recommends: execute\n"
        "If approved, you'll place it yourself — approving here only records it in "
        "your shadow ledger.\n"
        "Review & approve: https://x.test/a/12?k=tok" + events.BRIEF_FOOTER)
    assert len(body) < len(ORDER_BODY_BEFORE)
    assert len(events.BRIEF_FOOTER) < len(events.FOOTER) / 1.5
    assert subject == events.review_queued(**ORDER_KW)[0]
    assert "brief" not in json.dumps(subject.event)
    assert_english_only(body)


def test_short_layout_drops_only_the_reasoning_excerpt():
    long = "execute — " + "The pullback is orderly — volume is falling. " * 6
    _, body = events.review_queued(**{**ORDER_KW, "recommendation": long}, brief=BRIEF)
    assert "The agent recommends: execute\n" in body
    assert "orderly" not in body
    # Without a brief the excerpt is still there, as before.
    assert long in events.review_queued(**{**ORDER_KW, "recommendation": long})[1]


def test_short_layout_keeps_warnings_and_the_shadow_note():
    _, body = events.review_queued(
        **{**ORDER_KW, "recommendation": ""}, brief=BRIEF,
        no_analysis_reason="LLM unavailable", stop_warning="Stop already fired.",
        option_preview="2x NVDA261016C00220000")
    lines = body.split("\n")
    assert lines[0] == "⚠ No agent analysis: LLM unavailable"
    assert lines[1] == "⚠ Stop already fired."
    assert any(line.startswith("Option order at trigger: 2x NVDA261016C00220000")
               for line in lines)
    assert any(line.startswith("If approved, you'll place it yourself") for line in lines)
    assert "Review & approve: https://x.test/a/12?k=tok" in lines
    for fact in ("never carries your dashboard access token", "works once",
                 "this item only", "24 hours"):
        assert fact in events.BRIEF_FOOTER


def test_revision_card_with_a_brief_is_the_short_layout():
    brief = {"why": "The 225 stop gives back most of the gain.",
             "risk": "A stop at 245 can trigger on a normal dip.", "plan": ""}
    _, body = events.review_queued(**REVISION_KW, brief=brief)
    assert body == (
        "Item #2 is waiting for you.\n"
        "Proposed: revise strategy 'aapl-services' to v3 on AAPL\n"
        "Strategy: aapl-services\n"
        "Why: The 225 stop gives back most of the gain.\n"
        "Risk: A stop at 245 can trigger on a normal dip." + events.BRIEF_FOOTER)


def test_brief_and_order_thread_hint_share_one_telegram_card(tmp_path, monkeypatch):
    IdAPI.instances = []
    monkeypatch.setattr(dispatch, "TelegramAPI", IdAPI)
    conn = connect(tmp_path / "t.db")
    app_state = AppState(conn)
    app_state.set("telegram_chat_id", "111")
    queue = ReviewQueue(conn, None, account="shadow")
    queue.set_telegram_card_recorder(OrderThreadStore(conn, "shadow").add_card)
    rid = int(queue.add(strategy_id="s", rule_id="r", ticker="NVDA", rule_type="soft",
                        condition="price < 215", action="buy $2000", snapshot={},
                        intent=OrderIntent(ticker="NVDA", side=OrderSide.BUY,
                                           notional=Decimal(2000), reason="r")))
    _, body = events.review_queued(**ORDER_KW, brief=BRIEF)
    dispatch.push_telegram_review_queued(queue=queue, app_state=app_state,
                                         telegram_bot_token="t", review_id=rid,
                                         body=body, account="shadow")
    [(_, html, markup)] = IdAPI.instances[0].sent_with_id
    assert html.startswith("[Shadow] Item #12 is waiting for you.")
    assert html.index("Why: Price 204") < html.index("The agent recommends")
    assert html.rstrip().endswith(ORDER_THREAD_CARD_HINT.strip())
    assert len(markup["inline_keyboard"][0]) == 2


# ---------------------------------------------------------------------------
# grounding: a brief is only kept when the stored fields back it
# ---------------------------------------------------------------------------

SOURCES = ["Orderly pullback of 4% to 207.30; weight after the buy about 10%.",
           "price < 215 and position_weight < 0.12", '{"position_weight": "0.0834"}']


def _raw(**over):
    raw = {"why": "Price 207.30 is under the 215 trigger after a 4% pullback.",
           "risk": "Weight rises to about 10%.", "plan": ""}
    raw.update(over)
    return raw


def test_grounded_brief_is_accepted_and_cleaned():
    brief, dropped, drop = card_brief.accept(
        _raw(why="  Price 207.30 is under\n the 215 trigger. "),
        sources=SOURCES, origin="review_agent")
    assert dropped == "" and drop is None
    assert brief == {"why": "Price 207.30 is under the 215 trigger.",
                     "risk": "Weight rises to about 10%.", "plan": "",
                     "origin": "review_agent"}


def test_rounded_numbers_and_fractions_as_percent_count_as_grounded():
    checked = card_brief.accept(
        _raw(why="Price 207 is under 215.", risk="Weight is 8.3% now, 12% is the limit."),
        sources=SOURCES, origin="x")
    assert checked.brief is not None


def test_number_missing_from_the_stored_fields_drops_the_brief():
    brief, dropped, _drop = card_brief.accept(_raw(risk="Earnings in 3 days could cut 20%."),
                                              sources=SOURCES, origin="x")
    assert brief is None
    assert "3" in dropped and "20" in dropped
    assert card_brief.ungrounded_numbers(_raw(), SOURCES) == []


def test_malformed_or_overlong_briefs_are_dropped():
    def accept(raw):
        return card_brief.accept(raw, sources=SOURCES, origin="x")[0]

    assert accept(None) is None
    assert accept("buy it") is None
    assert accept(["why"]) is None
    assert accept(_raw(risk="")) is None
    assert accept(_raw(why=7)) is None
    assert accept(_raw(why="word " * 81)) is None
    assert accept(_raw(why="x" * (card_brief.MAX_FIELD_CHARS + 1))) is None


def test_tool_args_without_any_brief_are_just_no_brief():
    assert card_brief.from_tool_args(None, "", None, sources=[], origin="chat") == (
        None, "no brief", None)


def test_load_tolerates_bad_stored_values():
    assert card_brief.load(None) is None
    assert card_brief.load("not json") is None
    assert card_brief.load('{"why": "only why"}') is None
    assert card_brief.brief_lines(card_brief.load(json.dumps(BRIEF))) == BRIEF_LINES


# ---------------------------------------------------------------------------
# review agent: same model call, prompt untouched with the flag off
# ---------------------------------------------------------------------------

def _answer(**extra) -> LLMResponse:
    data = {"recommendation": "execute",
            "reasoning": "Price 204 sits under the 205 trigger; guidance was weak.",
            "sources": []}
    data.update(extra)
    return LLMResponse(text=json.dumps(data))


def _prompt_for(review: dict) -> str:
    return PROMPT.format(**{k: review[k] for k in (
        "strategy_id", "rule_id", "rule_type", "ticker", "condition", "action", "snapshot")})


def test_flag_off_review_prompt_is_byte_identical_and_ignores_a_brief():
    assert hashlib.sha256(PROMPT.encode()).hexdigest() == REVIEW_PROMPT_SHA
    llm = ScriptedLLM([_answer(brief=dict(BRIEF))])
    analysis = ReviewAgent(llm, ToolRegistry()).analyze(REVIEW)
    assert llm.seen[0][0]["content"] == _prompt_for(REVIEW)
    assert analysis.brief is None
    assert analysis.model_dump_json() == ReviewAnalysis(
        recommendation="execute", reasoning=analysis.reasoning).model_dump_json()
    assert "brief" not in analysis.model_dump_json()


def test_flag_on_asks_for_the_brief_in_the_same_call_and_keeps_it():
    llm = ScriptedLLM([_answer(brief={k: BRIEF[k] for k in ("why", "risk", "plan")})])
    views = [{"id": 3, "subject": "AAPL", "ts": "2026-11-01T00:00:00+00:00",
              "conviction": "high", "horizon": "6 months",
              "expires_ts": "2026-12-01T00:00:00+00:00",
              "stance": "add on pullbacks", "invalidation": "not stated"}]
    agent = ReviewAgent(llm, ToolRegistry(), card_brief=True, views_for=lambda t: views)
    analysis = agent.analyze(REVIEW)
    prompt = llm.seen[0][0]["content"]
    assert prompt.startswith(_prompt_for(REVIEW))
    assert prompt.endswith(card_brief.REVIEW_PROMPT_ADDENDUM)
    assert "add on pullbacks" in prompt
    assert len(llm.seen) == 1  # one model call, no second one for the brief
    assert analysis.brief == BRIEF
    assert "brief" not in analysis.model_dump_json()


def test_flag_on_ungrounded_brief_is_dropped_but_the_analysis_stays():
    llm = ScriptedLLM([_answer(brief={"why": "Price fell 31% in a week.",
                                      "risk": "More downside.", "plan": ""})])
    analysis = ReviewAgent(llm, ToolRegistry(), card_brief=True).analyze(REVIEW)
    assert analysis.brief is None
    assert analysis.recommendation == "execute" and not analysis.is_unparseable()


def test_malformed_brief_never_makes_the_analysis_unparseable():
    for flag in (False, True):
        llm = ScriptedLLM([_answer(brief="just buy it")])
        analysis = ReviewAgent(llm, ToolRegistry(), card_brief=flag).analyze(REVIEW)
        assert analysis.recommendation == "execute" and analysis.brief is None


def test_unparseable_reply_gets_no_brief():
    llm = ScriptedLLM([LLMResponse(text="I think maybe buy?")])
    analysis = ReviewAgent(llm, ToolRegistry(), card_brief=True).analyze(REVIEW)
    assert analysis.is_unparseable() and analysis.brief is None


# ---------------------------------------------------------------------------
# sentinel: stored on the review, shown on the card, old layout without it
# ---------------------------------------------------------------------------

class BriefAgent:
    def __init__(self, brief):
        self.brief = brief

    def analyze(self, review):
        analysis = ReviewAnalysis(recommendation="execute", reasoning="because")
        analysis.brief = self.brief
        return analysis


def _run_confirm(tmp_path, agent):
    s, _store, ex, q, notifier = make(tmp_path, strategy_yaml(auth="confirm"),
                                      account="shadow")
    s.review_agent = agent
    report = s.run_once()
    assert report.outcomes[0].disposition == "queued" and ex.calls == []
    [(_, body)] = [(sub, b) for sub, b in notifier.sent if "waiting" in sub]
    return q.get(1), body


def test_sentinel_stores_the_brief_and_puts_it_on_the_card(tmp_path):
    row, body = _run_confirm(tmp_path, BriefAgent(dict(BRIEF)))
    assert json.loads(row["decision_brief"]) == BRIEF
    assert row["status"] == "pending"
    assert "brief" not in row["agent_analysis"]
    lines = body.split("\n")
    at = lines.index(BRIEF_LINES[0])
    assert lines[at:at + 3] == BRIEF_LINES
    # The verdict only: the brief replaces the reasoning excerpt on the card,
    # and the full reasoning stays on the review.
    assert lines[at + 3] == "The agent recommends: execute"
    assert "because" not in body and "because" in row["agent_analysis"]
    assert body.endswith(events.BRIEF_FOOTER)


def test_sentinel_card_without_a_brief_keeps_the_old_layout(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    row, with_none = _run_confirm(tmp_path / "a", BriefAgent(None))
    assert row["decision_brief"] is None
    _, with_brief = _run_confirm(tmp_path / "b", BriefAgent(dict(BRIEF)))
    assert "Why:" not in with_none
    assert "The agent recommends: execute — because" in with_none
    assert with_none.endswith(events.FOOTER)
    # Same card apart from the brief, the verdict-only line and the footer.
    assert "\n".join(line for line in with_brief.split("\n")
                     if line not in BRIEF_LINES).replace(
        "The agent recommends: execute", "The agent recommends: execute — because"
    ).replace(events.BRIEF_FOOTER, events.FOOTER) == with_none


def test_sentinel_card_survives_a_brief_that_cannot_be_stored(tmp_path):
    (tmp_path / "a").mkdir()
    s, _store, _ex, q, notifier = make(tmp_path / "a", strategy_yaml(auth="confirm"))
    s.review_agent = BriefAgent(dict(BRIEF))

    def boom(*a, **k):
        raise RuntimeError("disk full")

    q.attach_brief = boom
    assert s.run_once().outcomes[0].disposition == "queued"
    [body] = [b for sub, b in notifier.sent if "waiting" in sub]
    assert "Why:" not in body and "The agent recommends: execute" in body


# ---------------------------------------------------------------------------
# chat tools (draft_strategy, propose_order) and the reflection tool
# ---------------------------------------------------------------------------

GOOD = """\
name: "New"
status: draft
version: 1
position: {ticker: MSFT, target_weight: 10%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""


class SpyNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))


class SpySink:
    def __init__(self):
        self.calls = []

    def propose(self, *args):
        self.calls.append(args)
        return "queued"


def _chat_tools(tmp_path, **kw):
    (tmp_path / "strategies").mkdir(exist_ok=True)
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path / "strategies", conn)
    queue = ReviewQueue(conn, None, account="shadow")
    notifier, sink, reg = SpyNotifier(), SpySink(), ToolRegistry()
    register_action_tools(reg, strategies=store, executor=None, confirm=lambda _p: False,
                          order_sink=sink, queue=queue, notifier=notifier,
                          account="shadow", **kw)
    return reg, queue, notifier, sink


def _call(reg, name, **kw):
    return reg.execute(ToolCall(id="x", name=name, arguments=kw))


def _specs(registry) -> list[dict]:
    return [spec.model_dump() for spec in registry.specs()]


def _without_brief_args(specs):
    specs = json.loads(json.dumps(specs))
    for spec in specs:
        text = json.dumps(spec)
        for name in card_brief.TOOL_PROPERTIES:
            assert (name in text) == (spec["name"] in (
                "draft_strategy", "propose_order", "propose_strategy_revision"))

        def strip(node):
            if isinstance(node, dict):
                for name in card_brief.TOOL_PROPERTIES:
                    node.pop(name, None)
                for value in node.values():
                    strip(value)
            elif isinstance(node, list):
                for value in node:
                    strip(value)

        strip(spec)
    return specs


def test_chat_tool_schemas_are_unchanged_with_the_flag_off(tmp_path):
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    off = _specs(_chat_tools(tmp_path / "off")[0])
    on = _specs(_chat_tools(tmp_path / "on", card_brief=True)[0])
    assert "brief_" not in json.dumps(off)
    assert _without_brief_args(on) == off


def test_flag_off_chat_tools_ignore_brief_arguments(tmp_path):
    reg, queue, notifier, sink = _chat_tools(tmp_path)
    out = _call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="init",
                brief_why="Sell under 100.", brief_risk="Whipsaw.")
    assert "queued" in out
    assert queue.get(1)["decision_brief"] is None
    assert "Why:" not in notifier.sent[0][1]
    _call(reg, "propose_order", ticker="MSFT", side="buy", notional="500", reason="dip",
          brief_why="Dip.", brief_risk="More dip.")
    [(intent,)] = sink.calls
    assert intent.ticker == "MSFT"


def test_flag_on_draft_strategy_stores_and_shows_the_brief(tmp_path):
    reg, queue, notifier, _ = _chat_tools(tmp_path, card_brief=True)
    _call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD,
          reason="Hard stop under 100 protects the 10% position.",
          brief_why="A hard stop under 100 protects the 10% position.",
          brief_risk="A gap down can fill well under the stop.")
    row = queue.get(1)
    assert json.loads(row["decision_brief"]) == {
        "why": "A hard stop under 100 protects the 10% position.",
        "risk": "A gap down can fill well under the stop.", "plan": "", "origin": "chat"}
    assert row["status"] == "pending"
    body = notifier.sent[0][1]
    assert ("Strategy: new\nWhy: A hard stop under 100 protects the 10% position.\n"
            "Risk: A gap down can fill well under the stop.") in body


def test_flag_on_draft_strategy_drops_an_ungrounded_brief(tmp_path):
    reg, queue, notifier, _ = _chat_tools(tmp_path, card_brief=True)
    out = _call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="init",
                brief_why="Analysts see 40% upside.", brief_risk="None.")
    assert "queued" in out
    assert queue.get(1)["decision_brief"] is None
    assert "Why:" not in notifier.sent[0][1]


def test_flag_on_propose_order_hands_the_brief_to_the_sink(tmp_path):
    reg, _queue, _n, sink = _chat_tools(tmp_path, card_brief=True)
    _call(reg, "propose_order", ticker="MSFT", side="buy", notional="500",
          reason="Trim cash: buy 500 of MSFT on the dip.",
          brief_why="Puts 500 of idle cash into MSFT on the dip.",
          brief_risk="The dip continues.")
    _call(reg, "propose_order", ticker="MSFT", side="buy", notional="500", reason="dip")
    [(_, brief, drop), (_intent,)] = sink.calls
    assert drop is None
    assert brief["origin"] == "chat" and brief["why"].startswith("Puts 500")


def test_order_sink_stores_and_shows_a_brief(tmp_path):
    conn = connect(tmp_path / "t.db")
    queue = ReviewQueue(conn, None, account="shadow")
    notifier = SpyNotifier()
    sink = QueueingOrderSink(queue, RiskGate(RiskLimits()), FakeBroker(), FakeData(),
                             TradeJournal(conn, account="shadow"), conversation_id=7,
                             notifier=notifier, account="shadow")
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional="1000",
                         reason="rebalance")
    sink.propose(intent, dict(BRIEF))
    sink.propose(intent)
    assert json.loads(queue.get(1)["decision_brief"]) == BRIEF
    assert queue.get(2)["decision_brief"] is None
    assert "Proposed: rebalance on AAPL\n" + "\n".join(BRIEF_LINES) in notifier.sent[0][1]
    assert "Why:" not in notifier.sent[1][1]


CURRENT = """\
name: "S1"
status: active
version: 1
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""
PROPOSED = CURRENT.replace("version: 1", "version: 2").replace("price < 100", "price < 90")


def _reflection_tools(tmp_path, **kw):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "s1.yaml").write_text(CURRENT)
    conn = connect(tmp_path / "db.sqlite")
    queue = ReviewQueue(conn, executor=None)
    reg = ToolRegistry()
    register_reflection_tools(reg, strategies=StrategyStore(strategies_dir, conn),
                              queue=queue, **kw)
    return reg, queue


def test_reflection_tool_schema_is_unchanged_with_the_flag_off(tmp_path):
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    off = _specs(_reflection_tools(tmp_path / "off")[0])
    on = _specs(_reflection_tools(tmp_path / "on", card_brief=True)[0])
    assert "brief_" not in json.dumps(off)
    assert _without_brief_args(on) == off


def test_reflection_brief_is_stored_only_with_the_flag_on(tmp_path):
    args = {"strategy_id": "s1", "new_yaml": PROPOSED,
                "rationale": "The stop at 100 is too tight; widen it to 90.",
                "brief_why": "The stop at 100 is too tight, so widen it to 90.",
                "brief_risk": "A wider stop means a larger loss if it triggers."}
    (tmp_path / "off").mkdir()
    reg, queue = _reflection_tools(tmp_path / "off")
    assert "queued" in _call(reg, "propose_strategy_revision", **args)
    assert queue.get(1)["decision_brief"] is None

    (tmp_path / "on").mkdir()
    reg, queue = _reflection_tools(tmp_path / "on", card_brief=True)
    assert "queued" in _call(reg, "propose_strategy_revision", **args)
    brief = json.loads(queue.get(1)["decision_brief"])
    assert brief["origin"] == "reflection" and brief["why"].endswith("widen it to 90.")


def test_reflection_ungrounded_brief_is_dropped_and_the_revision_still_queues(tmp_path):
    reg, queue = _reflection_tools(tmp_path, card_brief=True)
    out = _call(reg, "propose_strategy_revision", strategy_id="s1", new_yaml=PROPOSED,
                rationale="Widen the stop.", brief_why="Volatility rose 55% this month.",
                brief_risk="Larger loss.")
    assert "queued" in out
    row = queue.get(1)
    assert row["decision_brief"] is None and row["status"] == "pending"


# ---------------------------------------------------------------------------
# web: Pending page and approve page
# ---------------------------------------------------------------------------

def _client(tmp_path, monkeypatch, **flags):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir(exist_ok=True)
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret",
                        web_base_url="https://x.test", **flags, **CONFIGURED_KEYS)
    client = TestClient(create_app(settings, broker=FakeBroker()))
    client.__enter__()
    monkeypatch.setattr(client.app.state.holder.get(), "data", FakeDataSource())
    client.post("/login", data={"token": "secret"})
    return client


def _queue_order(client):
    queue = client.app.state.holder.get().queue
    handle = queue.add(strategy_id="s1", rule_id="r1", ticker="AAPL", rule_type="soft",
                       condition="price < 205", action="buy $3000",
                       snapshot={"price": "204"},
                       intent=OrderIntent(ticker="AAPL", side=OrderSide.BUY,
                                          notional="3000", reason="rule r1"))
    queue.attach_analysis(int(handle), ReviewAnalysis(
        recommendation="execute", reasoning="Long analysis text.").model_dump_json())
    return queue, handle


def _card(html: str, rid: int) -> str:
    start = html.index(f'id="review-{rid}"')
    return html[start:html.index("</form>\n  </div>", start)]


def test_pending_page_is_unchanged_with_the_flag_off_even_if_a_brief_is_stored(
        tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    queue, handle = _queue_order(client)
    before = _card(client.get("/reviews").text, int(handle))
    queue.attach_brief(int(handle), BRIEF)
    after = _card(client.get("/reviews").text, int(handle))
    assert after == before
    assert "decision-brief" not in after and "Why:" not in after
    page = client.get(f"/a/{int(handle)}?k={handle.token}").text
    assert "decision-brief" not in page
    client.__exit__(None, None, None)


def test_pending_page_shows_the_brief_above_the_collapsed_analysis(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, card_reason_brief=True)
    queue, handle = _queue_order(client)
    without = _card(client.get("/reviews").text, int(handle))
    assert "decision-brief" not in without  # no brief stored: old layout
    queue.attach_brief(int(handle), BRIEF)
    card = _card(client.get("/reviews").text, int(handle))
    for label, text in (("Why", BRIEF["why"]), ("Risk", BRIEF["risk"]),
                        ("Plan", BRIEF["plan"])):
        assert f"<strong>{label}:</strong> {text}" in card
    assert card.index("decision-brief") < card.index("<details")
    assert "Long analysis text." in card  # the full analysis is still there
    assert 'action="/reviews/1/approve"' in card
    assert_english_only(card)
    page = client.get(f"/a/{int(handle)}?k={handle.token}").text
    assert f"<strong>Why:</strong> {BRIEF['why']}" in page
    client.__exit__(None, None, None)


def test_pending_page_shows_the_brief_on_a_strategy_revision(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, card_reason_brief=True)
    queue = client.app.state.holder.get().queue
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="", new_yaml=CURRENT, diff="",
        rationale="Long rationale.", source="chat", is_new=True)
    queue.attach_brief(int(rid), BRIEF)
    card = _card(client.get("/reviews").text, int(rid))
    assert card.index("decision-brief") < card.index("Rationale:")
    assert "Long rationale." in card
    client.__exit__(None, None, None)


def test_brief_is_shown_only_for_an_account_with_the_flag_on():
    raw = json.dumps(BRIEF)
    shadow_only = _settings(shadow_card_reason_brief=True)
    assert card_brief.shown(shadow_only, "paper", raw) == []
    assert [label for label, _ in card_brief.shown(shadow_only, "shadow", raw)] == [
        "Why", "Risk", "Plan"]
    assert card_brief.shown(None, "shadow", raw) == []
    assert card_brief.shown(shadow_only, "shadow", "broken") == []


def test_app_wires_the_brief_to_shadow_only(tmp_path, monkeypatch):
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: ScriptedLLM([]))
    client = _client(tmp_path, monkeypatch, shadow_card_reason_brief=True)
    c = client.app.state.holder.get()
    assert c.accounts["shadow"].sentinel.review_agent.card_brief is True
    assert c.accounts["shadow"].sentinel.review_agent.views_for is not None
    paper = c.accounts["paper"].sentinel.review_agent
    assert paper.card_brief is False and paper.views_for is None

    def tool_text(account):
        session = client.app.state.chat_services[account].session()
        return json.dumps(_specs(session.registry))

    assert "brief_why" in tool_text("shadow")
    assert "brief_" not in tool_text("paper")
    client.__exit__(None, None, None)


def test_app_with_the_flag_off_wires_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: ScriptedLLM([]))
    client = _client(tmp_path, monkeypatch)
    c = client.app.state.holder.get()
    for account in ("paper", "shadow"):
        agent = c.accounts[account].sentinel.review_agent
        assert agent.card_brief is False and agent.views_for is None
        session = client.app.state.chat_services[account].session()
        assert "brief_" not in json.dumps(_specs(session.registry))
    client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# CLI JSON, research export, demo seed
# ---------------------------------------------------------------------------

def test_cli_json_has_no_brief_key_without_a_brief(tmp_path):
    conn = connect(tmp_path / "t.db")
    queue = ReviewQueue(conn, None)
    rid = int(queue.add(strategy_id="s", rule_id="r", ticker="AAPL", rule_type="soft",
                        condition="price < 205", action="buy $3000", snapshot={},
                        intent=None))
    assert "decision_brief" not in _review_to_json(queue.get(rid))
    queue.attach_brief(rid, BRIEF)
    assert _review_to_json(queue.get(rid))["decision_brief"] == BRIEF


def test_research_export_writes_the_same_brief_text(tmp_path):
    from allpath_trade.research.cli import main
    from tests.research_fixtures import DBBuilder

    b = DBBuilder(tmp_path / "prod.db")
    b.report(date="2026-12-02")
    rid = b.review(ts="2026-12-02T15:00:00+00:00", kind="order", source="sentinel",
                   status="pending", account="shadow")
    b.review(ts="2026-12-02T15:05:00+00:00", kind="order", source="sentinel",
             status="pending", account="paper")
    ReviewQueue(b.conn, None, account="shadow").attach_brief(rid, BRIEF)
    path = b.close()
    out = tmp_path / "out"
    assert main(["regime-metrics", "--db", str(path), "--from", "2026-12-01",
                 "--to", "2026-12-03", "--out", str(out)]) == 0
    with open(out / "card_briefs.csv", newline="") as fh:
        [row] = list(csv.DictReader(fh))
    assert (row["review_id"], row["account"], row["kind"], row["origin"]) == (
        str(rid), "shadow", "order", "review_agent")
    assert [row["why"], row["risk"], row["plan"]] == [BRIEF[k] for k in ("why", "risk", "plan")]


def test_research_export_tolerates_a_db_without_the_column(tmp_path):
    import sqlite3

    from allpath_trade.research.regime_metrics import card_brief_rows

    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE pending_reviews (id INTEGER PRIMARY KEY, ts TEXT)")
    assert card_brief_rows(conn, "2026-01-01", "2026-12-31") == []


def test_demo_seed_briefs_are_grounded_in_the_demo_analysis(tmp_path):
    from allpath_trade.demo import seed_demo

    conn = connect(tmp_path / "demo.db")
    seed_demo(conn, tmp_path / "strategies", tmp_path / "memory")
    rows = conn.execute("SELECT * FROM pending_reviews ORDER BY id").fetchall()
    assert [r["kind"] for r in rows] == ["order", "strategy_revision"]
    for row in rows:
        brief = card_brief.load(row["decision_brief"])
        assert brief is not None
        analysis = json.loads(row["agent_analysis"] or "{}").get("reasoning", "")
        sources = [analysis, *card_brief.row_sources(row)]
        assert card_brief.ungrounded_numbers(brief, sources) == []
        assert_english_only(" ".join(card_brief.brief_lines(brief)))
