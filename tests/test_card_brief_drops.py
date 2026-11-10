"""Decision-brief grounding after the first real drop (review #276, shadow,
2026-11-09: "brief has numbers the stored analysis does not: 9", and the
dropped text was kept nowhere). Covers: the evidence a drop now leaves
behind, the false-drop causes that were real, the ones deliberately still
refused, and keeping Why/Risk when only Plan fails.

The #276 inputs below are the stored fields as reported, with made-up
prose around the same figures."""

from __future__ import annotations

import csv
import json
from datetime import date

import pytest

from allpath_trade import card_brief
from allpath_trade.agent.action_tools import register_action_tools
from allpath_trade.agent.reflection_tools import register_reflection_tools
from allpath_trade.agent.review import ReviewAgent, ReviewAnalysis
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.cli import _review_to_json
from allpath_trade.llm.base import LLMResponse, ToolCall
from allpath_trade.risk.gate import RiskGate, RiskLimits
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.strategy.store import StrategyStore
from allpath_trade.web.order_sink import QueueingOrderSink
from tests.test_agent_loop import ScriptedLLM
from tests.test_card_brief import CURRENT, GOOD, PROPOSED, BriefAgent
from tests.test_sentinel import FakeBroker, FakeData, make, strategy_yaml

TODAY = [date(2026, 11, 9)]

ROW_276 = {
    "id": 276, "account": "shadow", "ticker": "CRDO", "strategy_id": "crdo-ladder",
    "rule_id": "add-1", "rule_type": "soft",
    "condition": "price <= 185 and position_weight <= 0.03", "action": "buy $1500",
    "snapshot": json.dumps({
        "price": "129.485", "position_qty": "57.672575", "position_weight": "0.0281",
        "avg_entry_price": "167.024275", "pnl_pct": "-22.475", "target_weight": "0.03"}),
    "intent": json.dumps({"ticker": "CRDO", "side": "buy", "notional": "1500",
                          "qty": None}),
    "risk_preview": None,
}
REASONING_276 = (
    "Gap down on 11/10 from ~$133 to $129.49 on heavy volume. The thesis is "
    "intact: 157% revenue growth and a $325 target. The rule fires at ≤$185 "
    "with weight ≤3%; the position is at -22.5%. Volume was 16.8M shares 11/5.")
SOURCES_276 = ["get_quote: day range $120.51–$132.89", "earnings date 12/2",
               "get_positions: 57.7 sh @ $167.02, 2.81% of equity"]


def check_276(why: str, risk: str = "The slide continues below $129.49.",
              plan: str = "", *, with_sources: bool = True):
    analysis = card_brief.analysis_sources(REASONING_276,
                                           SOURCES_276 if with_sources else [])
    return card_brief.accept(
        {"why": why, "risk": risk, "plan": plan}, origin="review_agent",
        sources=[*analysis, *card_brief.row_sources(ROW_276)],
        derived=card_brief.row_derived(ROW_276), today=TODAY)


# ---------------------------------------------------------------------------
# #276: briefs that are honest about the stored review now pass
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("why", [
    # plain restatements, rounded or truncated
    "CRDO at $129.49 is under the $185 add trigger with weight at 2.81%.",
    "Price $129 is far below the 185 trigger; weight 2.8% is under the 3% cap.",
    "About $130 against a 185 trigger, position down 22.5%.",
    # the analysis `sources` list
    "Day range was $120.51–$132.89; the position is 57.7 shares at $167.02.",
    # what the card itself computes: Est. size ~11.58 shares
    "Buys about 11.58 shares ($1,500) at $129.49.",
    "Adds roughly 11.6 shares to the 57.7 held.",
    # gaps the trigger data implies
    "Price is 30% under the $185 trigger.",
    "At $129.49 it is $55.5 below the 185 level and 22% under the $167 entry.",
    "Weight 2.81% leaves 0.19 points under the 3% cap.",
    # the review's own date, and a date from the analysis
    "As of 11/9 the price is $129.49, under the 185 trigger.",
    "On Nov 9 it trades at $129.49; earnings are 12/2.",
    "Gapped down 11/10 to $129.49.",
    # magnitude suffixes
    "A $1.5k add at $129.49 after 16.8M shares traded.",
    "About 17M shares traded; adds $1.5K under the 185 trigger.",
    # the rule id carries a digit
    "Rule add-1 fired at $129.49.",
])
def test_276_supported_briefs_are_kept(why):
    checked = check_276(why)
    assert checked.reason == "" and checked.drop is None
    assert checked.brief["why"] == why


@pytest.mark.parametrize(("why", "missing"), [
    # a count nothing stored carries
    ("Earnings in 9 days; price $129.49 is under the 185 trigger.", "9"),
    # a percentage nothing implies
    ("Down 9% this week to $129.49.", "9"),
    # a level that is not in the analysis
    ("Support sits at $118; price is $129.49.", "118"),
    # a date that is neither stored nor today
    ("Fed meets 12/17; price $129.49.", "17"),
    # arithmetic between prose numbers is not a trigger gap
    ("$325 target is 196 above the price of $129.49.", "196"),
    # a suffix does not rescue a wrong magnitude
    ("Adds $15k at $129.49.", "15k"),
])
def test_276_invented_figures_still_drop_the_brief(why, missing):
    checked = check_276(why)
    assert checked.brief is None
    assert missing in checked.drop["missing"]
    assert checked.drop["raw"]["why"] == why
    assert checked.drop["kept_brief"] is False


def test_276_sources_list_is_part_of_the_stored_analysis():
    why = "The position is 57.7 shares at $167.02; day low was $120.51."
    assert check_276(why).brief is not None
    without = check_276(why, with_sources=False)
    assert without.brief is None and "120.51" in without.drop["missing"]


def test_a_date_is_only_free_when_it_is_the_reviews_own_date():
    def missing(text, today):
        return card_brief.ungrounded(text, ["price 129.49"], today=today)

    assert missing("As of 11/9 the price is 129.49.", TODAY) == []
    assert missing("As of November 9th, 2026 the price is 129.49.", TODAY) == []
    assert missing("As of 2026-11-09 the price is 129.49.", TODAY) == []
    assert missing("As of 11/9 the price is 129.49.", [date(2026, 11, 12)]) == ["11", "9"]
    # Being today's day number does not make a bare 9 a date.
    assert missing("Down 9% to 129.49.", TODAY) == ["9"]
    assert card_brief.review_dates()  # New York and UTC dates of now


def test_gaps_come_only_from_the_trigger_pairs():
    derived = card_brief.row_derived(ROW_276)
    assert len(derived) == 9  # price/185, price/entry, weight/limit: 3 values each
    backed = [n for n in range(1, 101)
              if not card_brief.ungrounded(str(n), [], derived=derived, today=[])]
    # 55/56: $55.5 under the trigger, truncated or rounded; 30 and 42/43:
    # that gap as a percent of 185 and of the price; 37/38, 22, 28/29: the
    # same against the entry price; 6/7: the 0.19-point weight gap as a
    # percent of the 3% cap and of the weight. Nothing else, so not 9.
    assert backed == [6, 7, 22, 28, 29, 30, 37, 38, 42, 43, 55, 56]
    assert card_brief.row_derived({"snapshot": "not json", "condition": None}) == []


def test_round_tens_and_suffixes_do_not_stretch_far():
    def ok(text, source):
        return card_brief.ungrounded(text, [source], today=[]) == []

    assert ok("about $130", "129.485") and not ok("about $140", "129.485")
    assert not ok("20%", "22.475")          # nearest-ten rounding only from 100 up
    assert ok("$1.5k", "1500") and ok("1.5 thousand", "$1,500")
    assert ok("17M", "16.8M shares") and ok("16.8 million", "16,800,000")
    assert not ok("$2k", "1500") and not ok("1.5M", "1500")
    assert ok("57 shares", "57.672575") and ok("58 shares", "57.672575")
    assert not ok("59 shares", "57.672575")


# ---------------------------------------------------------------------------
# softer failure: Plan can go on its own, Why and Risk cannot
# ---------------------------------------------------------------------------

def test_ungrounded_plan_is_left_off_and_the_rest_is_kept():
    checked = check_276("Price $129.49 is under the $185 trigger.",
                        plan="Third of 4 planned adds.")
    assert checked.brief == {"why": "Price $129.49 is under the $185 trigger.",
                             "risk": "The slide continues below $129.49.",
                             "plan": "", "origin": "review_agent"}
    assert card_brief.brief_lines(checked.brief) == [
        "Why: Price $129.49 is under the $185 trigger.",
        "Risk: The slide continues below $129.49."]
    assert checked.reason.startswith("plan line left off")
    assert checked.drop["kept_brief"] is True
    assert checked.drop["missing"] == ["4"]
    assert checked.drop["raw"]["plan"] == "Third of 4 planned adds."


def test_overlong_plan_is_left_off_and_the_rest_is_kept():
    checked = check_276("Price $129.49 is under the $185 trigger.", plan="plan " * 90)
    assert checked.brief is not None and checked.brief["plan"] == ""
    assert "over" in checked.reason and checked.drop["kept_brief"] is True


@pytest.mark.parametrize("field", ["why", "risk"])
def test_ungrounded_why_or_risk_still_drops_the_whole_brief(field):
    lines = {"why": "Price $129.49 is under the $185 trigger.",
             "risk": "The slide continues below $129.49.",
             "plan": "Fits the ladder."}
    lines[field] = "Analysts see 40% upside."
    checked = check_276(**lines)
    assert checked.brief is None
    assert checked.drop["missing"] == ["40"] and checked.drop["kept_brief"] is False


# ---------------------------------------------------------------------------
# evidence: what a drop leaves behind
# ---------------------------------------------------------------------------

def test_drop_record_holds_the_raw_text_reason_and_figures():
    checked = check_276("Down 9% this week to $129.49.")
    drop = checked.drop
    assert set(drop) == {"reason", "origin", "kept_brief", "missing", "raw", "ts"}
    assert drop["reason"] == "brief has numbers the stored analysis does not: 9"
    assert drop["origin"] == "review_agent" and drop["missing"] == ["9"]
    assert drop["raw"] == {"why": "Down 9% this week to $129.49.",
                           "risk": "The slide continues below $129.49.", "plan": ""}
    json.dumps(drop)


def test_drop_record_caps_and_tolerates_odd_raw_values():
    def accept(raw):
        return card_brief.accept(raw, sources=[], origin="x", today=[])

    assert accept(None).drop["raw"] is None and accept(None).drop["reason"] == "no brief"
    assert accept("buy " * 500).drop["raw"] == ("buy " * 500)[:card_brief.MAX_RAW_CHARS]
    raw = accept({"why": "w" * 5000, "risk": {"nested": 1}}).drop["raw"]
    assert len(raw["why"]) == card_brief.MAX_RAW_CHARS and raw["risk"] == '{"nested": 1}'
    assert card_brief.from_tool_args(None, None, None, sources=[], origin="chat").drop is None


def _answer(brief) -> LLMResponse:
    return LLMResponse(text=json.dumps({
        "recommendation": "execute", "reasoning": REASONING_276,
        "sources": SOURCES_276, "brief": brief}))


def test_review_agent_uses_sources_card_values_and_trigger_gaps():
    brief = {"why": "Price is 30% under the $185 trigger; day low was $120.51.",
             "risk": "Adds 11.58 shares into a slide.", "plan": ""}
    agent = ReviewAgent(ScriptedLLM([_answer(brief)]), ToolRegistry(), card_brief=True)
    analysis = agent.analyze(dict(ROW_276))
    assert analysis.brief is not None and analysis.brief_drop is None
    assert "brief" not in analysis.model_dump_json()


def test_review_agent_keeps_the_dropped_brief_as_evidence():
    brief = {"why": "Down 9% this week.", "risk": "More downside.", "plan": ""}
    agent = ReviewAgent(ScriptedLLM([_answer(brief)]), ToolRegistry(), card_brief=True)
    analysis = agent.analyze(dict(ROW_276))
    assert analysis.brief is None
    assert analysis.brief_drop["raw"]["why"] == "Down 9% this week."
    assert analysis.brief_drop["missing"] == ["9"]
    assert "brief" not in analysis.model_dump_json()
    # Flag off: nothing is checked, so nothing is recorded.
    off = ReviewAgent(ScriptedLLM([_answer(brief)]), ToolRegistry()).analyze(dict(ROW_276))
    assert off.brief is None and off.brief_drop is None


def test_review_agent_records_a_missing_brief_too():
    llm = ScriptedLLM([LLMResponse(text=json.dumps(
        {"recommendation": "skip", "reasoning": "No edge."}))])
    analysis = ReviewAgent(llm, ToolRegistry(), card_brief=True).analyze(dict(ROW_276))
    assert analysis.brief is None and analysis.brief_drop["reason"] == "no brief"


class DropAgent(BriefAgent):
    def __init__(self, brief, drop):
        super().__init__(brief)
        self.drop = drop

    def analyze(self, review):
        analysis = super().analyze(review)
        analysis.brief_drop = self.drop
        return analysis


def test_sentinel_stores_the_drop_and_sends_the_old_card(tmp_path, capsys):
    drop = check_276("Down 9% this week to $129.49.").drop
    s, _store, ex, q, notifier = make(tmp_path, strategy_yaml(auth="confirm"),
                                      account="shadow")
    s.review_agent = DropAgent(None, drop)
    assert s.run_once().outcomes[0].disposition == "queued" and ex.calls == []
    row = q.get(1)
    assert row["decision_brief"] is None and row["status"] == "pending"
    assert json.loads(row["brief_drop"]) == drop
    [body] = [b for sub, b in notifier.sent if "waiting" in sub]
    assert "Why:" not in body and "The agent recommends: execute — because" in body
    assert ("[review] #1: card brief dropped: brief has numbers the stored analysis "
            "does not: 9") in capsys.readouterr().err


def test_sentinel_stores_a_partial_drop_next_to_the_kept_brief(tmp_path):
    checked = check_276("Price $129.49 is under the $185 trigger.",
                        plan="Third of 4 planned adds.")
    s, _store, _ex, q, notifier = make(tmp_path, strategy_yaml(auth="confirm"),
                                       account="shadow")
    s.review_agent = DropAgent(checked.brief, checked.drop)
    s.run_once()
    row = q.get(1)
    assert json.loads(row["decision_brief"])["plan"] == ""
    assert json.loads(row["brief_drop"])["kept_brief"] is True
    [body] = [b for sub, b in notifier.sent if "waiting" in sub]
    assert "Why: Price $129.49" in body and "Plan:" not in body


def test_sentinel_without_a_brief_or_drop_writes_neither_column(tmp_path):
    s, _store, _ex, q, _n = make(tmp_path, strategy_yaml(auth="confirm"))
    s.review_agent = DropAgent(None, None)
    s.run_once()
    row = q.get(1)
    assert row["decision_brief"] is None and row["brief_drop"] is None


def test_a_later_clean_brief_clears_the_old_drop(tmp_path):
    conn = connect(tmp_path / "t.db")
    queue = ReviewQueue(conn, None, account="shadow")
    rid = int(queue.add(strategy_id="s", rule_id="r", ticker="CRDO", rule_type="soft",
                        condition="price <= 185", action="buy $1500", snapshot={},
                        intent=None))
    card_brief.store(queue, rid, check_276("Down 9% this week."), log="review")
    assert queue.get(rid)["brief_drop"] is not None
    card_brief.store(queue, rid, check_276("Price $129.49 is under the $185 trigger."),
                     log="review")
    row = queue.get(rid)
    assert row["brief_drop"] is None and row["decision_brief"] is not None


def test_store_never_raises(tmp_path, capsys):
    class Broken:
        def attach_brief(self, *a):
            raise RuntimeError("disk full")

        def record_brief_drop(self, *a):
            raise RuntimeError("disk full")

    kept = check_276("Price $129.49 is under the $185 trigger.")
    assert card_brief.store(Broken(), 1, kept, log="review") is None
    assert "could not store card brief for #1" in capsys.readouterr().err


def _call(reg, name, **kw):
    return reg.execute(ToolCall(id="x", name=name, arguments=kw))


def test_chat_draft_drop_is_recorded_on_the_review(tmp_path):
    (tmp_path / "strategies").mkdir()
    conn = connect(tmp_path / "db.sqlite")
    queue = ReviewQueue(conn, None, account="shadow")
    reg = ToolRegistry()
    register_action_tools(reg, strategies=StrategyStore(tmp_path / "strategies", conn),
                          executor=None, confirm=lambda _p: False, queue=queue,
                          account="shadow", card_brief=True)
    _call(reg, "draft_strategy", strategy_id="new", yaml_text=GOOD, reason="init",
          brief_why="Analysts see 40% upside.", brief_risk="None.")
    _call(reg, "draft_strategy", strategy_id="other", yaml_text=GOOD, reason="init")
    drop = json.loads(queue.get(1)["brief_drop"])
    assert drop["origin"] == "chat" and drop["raw"]["why"] == "Analysts see 40% upside."
    assert queue.get(1)["decision_brief"] is None
    assert queue.get(2)["brief_drop"] is None  # no brief arguments: not a drop


def test_chat_order_drop_is_recorded_on_the_review(tmp_path):
    conn = connect(tmp_path / "t.db")
    queue = ReviewQueue(conn, None, account="shadow")
    sink = QueueingOrderSink(queue, RiskGate(RiskLimits()), FakeBroker(), FakeData(),
                             TradeJournal(conn, account="shadow"), account="shadow")
    reg = ToolRegistry()
    register_action_tools(reg, strategies=None, executor=None, confirm=lambda _p: False,
                          order_sink=sink, account="shadow", card_brief=True)
    out = _call(reg, "propose_order", ticker="MSFT", side="buy", notional="500",
                reason="dip", brief_why="Down 12% this week.", brief_risk="More dip.")
    assert "queued" in out
    row = queue.get(1)
    assert row["decision_brief"] is None and row["status"] == "pending"
    assert json.loads(row["brief_drop"])["missing"] == ["12"]
    sink.propose(OrderIntent(ticker="MSFT", side=OrderSide.BUY, notional="500",
                             reason="dip"))
    assert queue.get(2)["brief_drop"] is None


def test_reflection_drop_is_recorded_on_the_review(tmp_path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "s1.yaml").write_text(CURRENT)
    conn = connect(tmp_path / "db.sqlite")
    queue = ReviewQueue(conn, executor=None)
    reg = ToolRegistry()
    register_reflection_tools(reg, strategies=StrategyStore(strategies_dir, conn),
                              queue=queue, card_brief=True)
    out = _call(reg, "propose_strategy_revision", strategy_id="s1", new_yaml=PROPOSED,
                rationale="Widen the stop.", brief_why="Volatility rose 55% this month.",
                brief_risk="Larger loss.")
    assert "queued" in out
    drop = json.loads(queue.get(1)["brief_drop"])
    assert drop["origin"] == "reflection" and drop["missing"] == ["55"]


# ---------------------------------------------------------------------------
# reading it back: CLI JSON and the research export
# ---------------------------------------------------------------------------

def test_cli_json_shows_a_drop_only_when_there_is_one(tmp_path):
    conn = connect(tmp_path / "t.db")
    queue = ReviewQueue(conn, None)
    rid = int(queue.add(strategy_id="s", rule_id="r", ticker="CRDO", rule_type="soft",
                        condition="price <= 185", action="buy $1500", snapshot={},
                        intent=None))
    assert "brief_drop" not in _review_to_json(queue.get(rid))
    queue.record_brief_drop(rid, check_276("Down 9% this week.").drop)
    assert _review_to_json(queue.get(rid))["brief_drop"]["missing"] == ["9"]


def test_research_export_lists_drops_with_text_and_reason(tmp_path):
    from allpath_trade.research.cli import main
    from tests.research_fixtures import DBBuilder

    b = DBBuilder(tmp_path / "prod.db")
    b.report(date="2026-12-02")
    ids = [b.review(ts=f"2026-12-02T15:0{i}:00+00:00", kind="order", source="sentinel",
                    status="pending", account="shadow") for i in range(3)]
    queue = ReviewQueue(b.conn, None, account="shadow")
    full = check_276("Down 9% this week to $129.49.")
    partial = check_276("Price $129.49 is under the $185 trigger.",
                        plan="Third of 4 planned adds.")
    queue.record_brief_drop(ids[0], full.drop)
    queue.attach_brief(ids[1], partial.brief)
    queue.record_brief_drop(ids[1], partial.drop)
    path = b.close()
    out = tmp_path / "out"
    assert main(["regime-metrics", "--db", str(path), "--from", "2026-12-01",
                 "--to", "2026-12-03", "--out", str(out)]) == 0
    with open(out / "card_brief_drops.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [(r["review_id"], r["kept_brief"], r["missing"]) for r in rows] == [
        (str(ids[0]), "0", "9"), (str(ids[1]), "1", "4")]
    assert rows[0]["reason"] == "brief has numbers the stored analysis does not: 9"
    assert rows[0]["raw_why"] == "Down 9% this week to $129.49."
    assert rows[1]["raw_plan"] == "Third of 4 planned adds."
    assert rows[0]["account"] == "shadow" and rows[0]["origin"] == "review_agent"
    with open(out / "card_briefs.csv", newline="") as fh:
        assert [r["review_id"] for r in csv.DictReader(fh)] == [str(ids[1])]


def test_research_drop_export_tolerates_a_db_without_the_column(tmp_path):
    import sqlite3

    from allpath_trade.research.regime_metrics import card_brief_drop_rows

    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE pending_reviews (id INTEGER PRIMARY KEY, ts TEXT,"
                 " decision_brief TEXT)")
    assert card_brief_drop_rows(conn, "2026-01-01", "2026-12-31") == []


def test_analysis_json_never_carries_brief_or_drop():
    analysis = ReviewAnalysis(recommendation="execute", reasoning="r")
    analysis.brief, analysis.brief_drop = {"why": "w"}, {"reason": "x"}
    assert analysis.model_dump_json() == ReviewAnalysis(
        recommendation="execute", reasoning="r").model_dump_json()
