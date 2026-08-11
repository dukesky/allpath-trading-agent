from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from allpath_trade.agent.action_tools import register_action_tools
from allpath_trade.agent.memory_tools import register_memory_tools
from allpath_trade.agent.readonly_tools import register_readonly_tools
from allpath_trade.agent.reflection_tools import register_reflection_tools
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.broker.base import Account, Broker, Position
from allpath_trade.config import Settings
from allpath_trade.data.base import DataSource, Quote
from allpath_trade.llm.base import LLMError, LLMResponse
from allpath_trade.memory.observations import ObservationLog
from allpath_trade.memory.search import SessionSearch
from allpath_trade.memory.store import MemoryStore
from allpath_trade.reflect import Reflector, _parse_report, build_briefing
from allpath_trade.store.conversations import ConversationStore
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal
from allpath_trade.store.reports import ReportStore
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.strategy.store import StrategyStore
from tests.test_agent_loop import ScriptedLLM, tool_response

STRAT = """\
name: "T"
status: active
version: 1
thesis: "AAPL Services segment margin expansion continues through FY25."
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""

# 15:30 UTC on 2024-01-10 is 10:30am ET the same calendar day.
NOW = datetime(2024, 1, 10, 15, 30, tzinfo=UTC)
ET_DATE = "2024-01-10"

REPORT_TEXT = """\
REPORT
Day summary: quiet day, one fill.
Per-strategy check: T is on track.
Lessons: none new.
Proposals: none.
SUMMARY
Quiet day. One trade filled as expected. Strategy T stayed on thesis. \
No lessons or proposals today. Nothing needs your attention.
"""


class FakeBroker(Broker):
    name = "fake"
    is_paper = True

    def __init__(self, positions=None, fail=False):
        self._positions = positions if positions is not None else []
        self.fail = fail

    def get_account(self):
        return Account(equity=Decimal(10000), cash=Decimal(5000),
                       buying_power=Decimal(10000))

    def get_positions(self):
        if self.fail:
            raise RuntimeError("broker down")
        return self._positions

    def get_order(self, order_id):
        raise NotImplementedError

    def get_orders(self, open_only=True):
        return []

    def submit_order(self, intent):
        raise NotImplementedError

    def cancel_order(self, order_id):
        raise NotImplementedError


class FakeData(DataSource):
    def __init__(self, quotes=None, fail_tickers=None):
        self.quotes = quotes or {}
        self.fail_tickers = fail_tickers or set()

    def get_quote(self, ticker):
        if ticker in self.fail_tickers:
            raise RuntimeError("quote unavailable")
        return self.quotes[ticker]

    def get_bars(self, ticker, days=365):
        return []


class FailingJournal:
    """Stands in for TradeJournal at the Reflector level, exercising
    `_trades_today`'s `except Exception: return []` branch directly --
    Reflector methods are called standalone (not via run_daily), so this
    never has to worry about `build_system_prompt`'s own `journal.recent`
    call (a different call site) also blowing up."""

    def recent(self, limit=50):
        raise RuntimeError("journal down")


class FailingObservations:
    """Stands in for ObservationLog, exercising `_observations_today`'s
    except branch."""

    def window(self, since_iso, until_iso, limit=5000):
        raise RuntimeError("observations down")


class FailingQueue:
    """Stands in for ReviewQueue, exercising `_pending_counts`'s except
    branch."""

    def list(self):
        raise RuntimeError("queue down")


@dataclass
class FakeComponents:
    reports: ReportStore
    conn: object
    journal: TradeJournal
    observations: ObservationLog
    broker: Broker
    data: DataSource
    strategies: StrategyStore
    queue: ReviewQueue
    memory: MemoryStore


def make_components(tmp_path, broker=None, data=None):
    conn = connect(tmp_path / "db.sqlite")
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "t.yaml").write_text(STRAT)
    return FakeComponents(
        reports=ReportStore(conn),
        conn=conn,
        journal=TradeJournal(conn),
        observations=ObservationLog(conn),
        broker=broker if broker is not None else FakeBroker(),
        data=data if data is not None else FakeData(),
        strategies=StrategyStore(strategies_dir, conn),
        queue=ReviewQueue(conn, executor=None),
        memory=MemoryStore(tmp_path / "memory", conn))


def make_settings(**overrides):
    kwargs = {"reflection_max_iters": 5, "context_budget_tokens": 60000}
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)


def insert_trade(components, *, ts, ticker="AAPL", side="buy", qty="10",
                 status="filled", filled_qty="10", filled_avg_price="150.00",
                 strategy_id="t", reason="test trade"):
    components.conn.execute(
        "INSERT INTO trades (ts, ticker, side, qty, notional, status, reason,"
        " strategy_id, risk_reasons, broker_order_id, filled_qty, filled_avg_price)"
        " VALUES (?, ?, ?, ?, NULL, ?, ?, ?, '[]', 'o1', ?, ?)",
        (ts, ticker, side, qty, status, reason, strategy_id, filled_qty,
         filled_avg_price))
    components.conn.commit()


# ---------------------------------------------------------------------------
# build_briefing (pure, unit-testable without an LLM)
# ---------------------------------------------------------------------------

def test_build_briefing_fill_pending_wording_keys_off_avg_price():
    briefing = build_briefing(
        et_date=ET_DATE,
        trades=[{"ts": "2024-01-10T15:00:00+00:00", "side": "buy", "ticker": "AAPL",
                "qty": "10", "status": "submitted", "filled_qty": "0",
                "filled_avg_price": None, "strategy_id": "t"}],
        observations=[], positions=[], pending_counts={})
    assert "submitted, fill pending" in briefing
    # filled_qty is "0", never NULL for a submitted-not-yet-filled order --
    # the wording must key off filled_avg_price, not filled_qty.
    assert "filled 0 @" not in briefing


def test_build_briefing_reports_a_real_fill():
    briefing = build_briefing(
        et_date=ET_DATE,
        trades=[{"ts": "2024-01-10T15:00:00+00:00", "side": "buy", "ticker": "AAPL",
                "qty": "10", "status": "filled", "filled_qty": "10",
                "filled_avg_price": "150.25", "strategy_id": "t"}],
        observations=[], positions=[], pending_counts={})
    assert "filled 10 @ 150.25" in briefing


def test_build_briefing_caps_trades_and_observations():
    # Deliberately short field values: MAX_TRADES/MAX_OBSERVATION_LINES are
    # meant to bind before the 2000-char-per-block backstop does (that
    # backstop exists for realistically long free-text fields, not this
    # count check) -- keeping each formatted line short means slicing to
    # the count cap alone decides what survives, with no interaction from
    # the char cap.
    trades = [{"ts": "2024-01-10T15:00:00+00:00", "side": "b", "ticker": "A",
              "qty": "1", "status": "s", "filled_qty": "1",
              "filled_avg_price": "1", "strategy_id": None} for _ in range(40)]
    observations = [{"ts": "2024-01-10T15:00:00+00:00", "source": "s",
                     "subject": "A", "text": f"o{i}"} for i in range(60)]
    briefing = build_briefing(et_date=ET_DATE, trades=trades, observations=observations,
                              positions=[], pending_counts={})
    assert briefing.count("filled 1 @ 1") == 30
    assert briefing.count("[s/A]") == 50


def test_build_briefing_blocks_are_fenced():
    briefing = build_briefing(et_date=ET_DATE, trades=[], observations=[],
                              positions=[], pending_counts={"order": 2})
    # 5 fenced blocks: strategies, trades, observations, positions, pending.
    assert briefing.count("<external-content>") == 5
    assert briefing.count("</external-content>") == 5
    assert "order: 2" in briefing
    assert "no active strategies" in briefing


def test_build_briefing_trade_reason_rendered_and_capped_at_80_chars():
    # build_briefing's own docstring claims trade `reason` ends up in the
    # briefing -- _format_trade must actually render it (Task 4 review
    # finding #9).
    long_reason = "x" * 200
    briefing = build_briefing(
        et_date=ET_DATE,
        trades=[{"ts": "2024-01-10T15:00:00+00:00", "side": "buy", "ticker": "AAPL",
                "qty": "10", "status": "filled", "filled_qty": "10",
                "filled_avg_price": "150.25", "strategy_id": "t",
                "reason": long_reason}],
        observations=[], positions=[], pending_counts={})
    assert f"reason={'x' * 80}" in briefing
    assert "x" * 81 not in briefing


def test_build_briefing_strategies_block_has_thesis_and_rules():
    briefing = build_briefing(
        et_date=ET_DATE, trades=[], observations=[], positions=[],
        pending_counts={},
        strategies=[{"id": "t", "name": "T", "status": "active",
                     "target": "15.0%", "thesis": "Long-term services growth.",
                     "rules": [{"condition": "price < 100", "action": "sell all",
                               "state": "armed"}]}])
    assert "t [active] T target=15.0%" in briefing
    assert "thesis: Long-term services growth." in briefing
    assert "price < 100 -> sell all [armed]" in briefing


def test_build_briefing_strategy_thesis_capped_at_300_chars():
    long_thesis = "x" * 500
    briefing = build_briefing(
        et_date=ET_DATE, trades=[], observations=[], positions=[],
        pending_counts={},
        strategies=[{"id": "t", "name": "T", "status": "active", "target": "n/a",
                     "thesis": long_thesis, "rules": []}])
    assert f"thesis: {'x' * 300}..." in briefing
    assert "x" * 301 not in briefing


def test_build_briefing_observations_char_cap_keeps_newest_not_oldest():
    # The observations block is chronologically ordered oldest->newest; on
    # char-cap overflow the NEWEST rows (the afternoon/close window) must
    # survive, not the oldest (Task 4 review finding #2). Each line is
    # padded well past 2000/30 chars so the char cap -- not the
    # MAX_OBSERVATION_LINES count cap -- is what triggers here.
    observations = [{"ts": "2024-01-10T15:00:00+00:00", "source": "s",
                     "subject": "A", "text": f"obs{i:02d}-" + "x" * 90}
                    for i in range(30)]
    briefing = build_briefing(et_date=ET_DATE, trades=[], observations=observations,
                              positions=[], pending_counts={})
    assert "obs29-" in briefing      # newest survives
    assert "obs00-" not in briefing  # oldest is the one dropped


def test_build_briefing_quote_failure_renders_n_a():
    # Reflector._positions_with_change is what actually calls the data
    # source; build_briefing just renders whatever it's handed, so this
    # exercises the contract at the build_briefing level: a failed lookup
    # must already have been turned into the string "n/a", never an
    # exception, by the time it reaches here.
    briefing = build_briefing(
        et_date=ET_DATE, trades=[], observations=[],
        positions=[{"ticker": "AAPL", "qty": "10", "avg_entry_price": "180",
                   "day_change": "n/a"}],
        pending_counts={})
    assert "day_change=n/a" in briefing


# ---------------------------------------------------------------------------
# Reflector.run_daily -- happy path
# ---------------------------------------------------------------------------

def test_run_daily_happy_path_stores_ok_report_and_conversation(tmp_path):
    components = make_components(
        tmp_path, broker=FakeBroker(positions=[
            Position(ticker="AAPL", qty=Decimal(10), avg_entry_price=Decimal(180),
                     market_value=Decimal(2000), unrealized_pl=Decimal(0))]),
        data=FakeData(quotes={"AAPL": Quote(
            ticker="AAPL", price=Decimal(205), previous_close=Decimal(200),
            as_of=NOW)}))
    insert_trade(components, ts="2024-01-10T15:00:00+00:00")

    llm = ScriptedLLM([
        tool_response("get_portfolio", {}),
        LLMResponse(text=REPORT_TEXT),
    ])
    reflector = Reflector(llm=llm, components=components, settings=make_settings())
    status = reflector.run_daily(now=NOW)

    assert status.startswith("ok:")
    row = components.reports.get(ET_DATE)
    assert row["status"] == "ok"
    assert "Day summary" in row["body"]
    # the leading "REPORT" marker line is stripped from the stored body
    # (Task 4 review finding #6) -- the body starts at the real content.
    assert not row["body"].startswith("REPORT")
    assert row["summary"].startswith("Quiet day.")
    assert row["tokens_used"] == 0
    assert row["conversation_id"] is not None

    conversations = ConversationStore(components.conn)
    turns = conversations.history(row["conversation_id"])
    roles = [t["role"] for t in turns]
    assert roles[0] == "user"           # the seed briefing
    assert "tool" in roles              # the get_portfolio round-trip
    assert roles[-1] == "assistant"
    # the conversation itself is tagged kind="reflection" (kept out of the
    # user-facing chat's history -- ConversationStore.latest filters on it)
    kind_row = components.conn.execute(
        "SELECT kind FROM conversations WHERE id = ?",
        (row["conversation_id"],)).fetchone()
    assert kind_row["kind"] == "reflection"


def test_run_daily_seed_briefing_is_first_user_message(tmp_path):
    components = make_components(tmp_path)
    insert_trade(components, ts="2024-01-10T15:00:00+00:00")
    llm = ScriptedLLM([LLMResponse(text=REPORT_TEXT)])
    reflector = Reflector(llm=llm, components=components, settings=make_settings())
    reflector.run_daily(now=NOW)
    first_call_messages = llm.seen[0]
    briefing_message = next(m for m in first_call_messages if m["role"] == "user")
    assert "Today's trades" in briefing_message["content"]
    assert "<external-content>" in briefing_message["content"]


def test_run_daily_seed_briefing_includes_strategy_thesis_and_rules(tmp_path):
    """build_system_prompt's own per-strategy line only carries
    id/status/rule-states (agent/context.py:45-49), never thesis/conditions/
    actions/target -- the seed briefing's strategies block must actually
    carry those, or the model has to spend a read_strategy call per strategy
    out of the 12-call session budget (Task 4 review finding #4)."""
    components = make_components(tmp_path)
    llm = ScriptedLLM([LLMResponse(text=REPORT_TEXT)])
    reflector = Reflector(llm=llm, components=components, settings=make_settings())
    reflector.run_daily(now=NOW)
    briefing = next(m for m in llm.seen[0] if m["role"] == "user")["content"]
    assert "AAPL Services segment margin expansion" in briefing
    assert "price < 100 -> sell all [armed]" in briefing
    assert "target=15.0%" in briefing


def test_run_daily_wires_fake_data_quote_failure_to_n_a_in_seed_briefing(tmp_path):
    """Reflector-level exercise of the (previously dead) FakeData
    `fail_tickers` fixture -- Task 4 review finding #5. This is the actual
    `_positions_with_change` per-quote except branch, not just the
    already-formatted "n/a" string build_briefing renders verbatim."""
    components = make_components(
        tmp_path,
        broker=FakeBroker(positions=[
            Position(ticker="AAPL", qty=Decimal(10), avg_entry_price=Decimal(180),
                     market_value=Decimal(2000), unrealized_pl=Decimal(0))]),
        data=FakeData(fail_tickers={"AAPL"}))
    llm = ScriptedLLM([LLMResponse(text=REPORT_TEXT)])
    reflector = Reflector(llm=llm, components=components, settings=make_settings())
    reflector.run_daily(now=NOW)
    briefing = next(m for m in llm.seen[0] if m["role"] == "user")["content"]
    assert "day_change=n/a" in briefing


def test_run_daily_wires_fake_broker_failure_to_positions_unavailable(tmp_path):
    """Reflector-level exercise of the (previously dead) FakeBroker
    `fail=True` fixture -- Task 4 review finding #5. Exercises
    `_positions_with_change`'s outer except branch (broker.get_positions()
    itself raising, not just a bad quote)."""
    components = make_components(tmp_path, broker=FakeBroker(fail=True))
    llm = ScriptedLLM([LLMResponse(text=REPORT_TEXT)])
    reflector = Reflector(llm=llm, components=components, settings=make_settings())
    reflector.run_daily(now=NOW)
    briefing = next(m for m in llm.seen[0] if m["role"] == "user")["content"]
    assert "positions unavailable: broker down" in briefing


def test_trades_today_swallows_journal_failure(tmp_path):
    components = make_components(tmp_path)
    components.journal = FailingJournal()
    reflector = Reflector(llm=None, components=components, settings=make_settings())
    assert reflector._trades_today(ET_DATE) == []


def test_observations_today_swallows_observation_log_failure(tmp_path):
    components = make_components(tmp_path)
    components.observations = FailingObservations()
    reflector = Reflector(llm=None, components=components, settings=make_settings())
    assert reflector._observations_today(ET_DATE) == []


def test_pending_counts_swallows_queue_failure(tmp_path):
    components = make_components(tmp_path)
    components.queue = FailingQueue()
    reflector = Reflector(llm=None, components=components, settings=make_settings())
    assert reflector._pending_counts() == {}


def test_trades_today_et_boundary_both_sides(tmp_path):
    """02:00Z on 2024-01-11 is 21:00 ET the PRIOR day (2024-01-10) -- the
    boundary case named in the Task 4 review. Tested on both sides: a trade
    just before ET midnight must count toward 2024-01-10, one right at (or
    after) ET midnight must NOT."""
    components = make_components(tmp_path)
    insert_trade(components, ts="2024-01-11T02:00:00+00:00", ticker="AAPL")
    # 05:00Z on 2024-01-11 is exactly 00:00 ET on 2024-01-11 -- the other
    # side of the same boundary.
    insert_trade(components, ts="2024-01-11T05:00:00+00:00", ticker="MSFT")
    reflector = Reflector(llm=None, components=components, settings=make_settings())
    todays = reflector._trades_today("2024-01-10")
    assert [t["ticker"] for t in todays] == ["AAPL"]


# ---------------------------------------------------------------------------
# Reflector.run_daily -- parse failure / retry / failed row
# ---------------------------------------------------------------------------

def test_run_daily_unparseable_once_then_corrective_retry_succeeds(tmp_path):
    components = make_components(tmp_path)
    llm = ScriptedLLM([
        LLMResponse(text="I looked around but forgot the format."),
        LLMResponse(text=REPORT_TEXT),
    ])
    reflector = Reflector(llm=llm, components=components, settings=make_settings())
    status = reflector.run_daily(now=NOW)
    assert status.startswith("ok:")
    row = components.reports.get(ET_DATE)
    assert row["status"] == "ok"
    # the corrective prompt was actually sent as the second user turn
    second_call_messages = llm.seen[1]
    assert any(m["role"] == "user" and "Reproduce it now" in m["content"]
              for m in second_call_messages)


def test_run_daily_unparseable_twice_records_failed_row(tmp_path):
    components = make_components(tmp_path)
    llm = ScriptedLLM([
        LLMResponse(text="no structure here"),
        LLMResponse(text="still no structure"),
    ])
    reflector = Reflector(llm=llm, components=components, settings=make_settings())
    status = reflector.run_daily(now=NOW)
    assert "failed" in status
    row = components.reports.get(ET_DATE)
    assert row["status"] == "failed"
    assert row["body"] == "reflection failed: unparseable report"
    assert row["summary"] == ""
    assert len(llm.responses) == 0  # exactly two calls were made, none left unused


def test_run_daily_cap_hit_still_gets_one_corrective_turn(tmp_path):
    components = make_components(tmp_path)
    # reflection_max_iters=3: three tool calls burn the ENTIRE first-turn
    # budget with no chance to ever emit REPORT/SUMMARY, so the first
    # run_turn ends on LIMIT_NOTICE. The corrective turn must be capped at
    # exactly one more iteration, not handed a fresh budget of 3 (Task 4
    # review finding #3, AgentSession.max_iters is per-turn but the spec's
    # budget is per-SESSION) -- ScriptedLLM has only one response queued
    # after the three tool calls and raises "script exhausted" if the
    # corrective turn asks for more than that.
    settings = make_settings(reflection_max_iters=3)
    llm = ScriptedLLM([
        tool_response("get_portfolio", {}),
        tool_response("get_portfolio", {}),
        tool_response("get_portfolio", {}),
        LLMResponse(text=REPORT_TEXT),
    ])
    reflector = Reflector(llm=llm, components=components, settings=settings)
    status = reflector.run_daily(now=NOW)
    assert status.startswith("ok:")
    row = components.reports.get(ET_DATE)
    assert row["status"] == "ok"
    # total LLM calls across BOTH turns <= configured cap + 1 (here 3 + 1 =
    # 4): the corrective turn never gets a fresh per-turn budget.
    assert len(llm.seen) <= settings.reflection_max_iters + 1
    assert len(llm.seen) == 4


def test_run_daily_llm_error_on_first_turn_fails_immediately_one_call(tmp_path):
    components = make_components(tmp_path)
    llm = ScriptedLLM([LLMError("provider down")])
    reflector = Reflector(llm=llm, components=components, settings=make_settings())
    status = reflector.run_daily(now=NOW)
    assert "failed" in status
    row = components.reports.get(ET_DATE)
    assert row["status"] == "failed"
    assert "llm error" in row["body"]
    assert len(llm.seen) == 1  # no corrective retry attempted -- the LLM is down


def test_run_daily_llm_error_on_corrective_turn_has_specific_failure_reason(tmp_path):
    """Task 4 review finding #8: an `(llm error: ...)` return from the
    CORRECTIVE turn must not fall through to the generic "unparseable
    report" message -- that discards the actual provider error, and reads
    as a formatting failure when the real cause was the LLM being down."""
    components = make_components(tmp_path)
    llm = ScriptedLLM([
        LLMResponse(text="no structure here"),
        LLMError("provider down"),
    ])
    reflector = Reflector(llm=llm, components=components, settings=make_settings())
    status = reflector.run_daily(now=NOW)
    assert "failed" in status
    row = components.reports.get(ET_DATE)
    assert row["status"] == "failed"
    assert row["body"] == (
        "reflection failed: llm error on corrective turn: (llm error: provider down)")


# ---------------------------------------------------------------------------
# _parse_report (pure, unit-testable)
# ---------------------------------------------------------------------------

def test_parse_report_strips_report_marker_and_preamble():
    text = "Sure, here you go.\nREPORT\nDay summary: fine.\nSUMMARY\nAll good."
    body, summary = _parse_report(text)
    assert body == "Day summary: fine."
    assert summary == "All good."


def test_parse_report_lenient_when_report_marker_missing():
    # Lenient: no REPORT line, but the SUMMARY split is still valid -- the
    # parse contract only strictly requires the bare SUMMARY line.
    text = "Day summary: fine, no REPORT marker.\nSUMMARY\nAll good."
    body, summary = _parse_report(text)
    assert body == "Day summary: fine, no REPORT marker."
    assert summary == "All good."


# ---------------------------------------------------------------------------
# Reflector.run_daily -- idempotency
# ---------------------------------------------------------------------------

def test_run_daily_is_idempotent_second_call_makes_no_llm_calls(tmp_path):
    components = make_components(tmp_path)
    llm = ScriptedLLM([LLMResponse(text=REPORT_TEXT)])
    reflector = Reflector(llm=llm, components=components, settings=make_settings())
    first = reflector.run_daily(now=NOW)
    assert first.startswith("ok:")
    assert len(components.reports.list()) == 1

    second = reflector.run_daily(now=NOW)
    assert "already ran" in second
    assert len(llm.seen) == 1  # the second call made no new LLM calls
    assert len(components.reports.list()) == 1


# ---------------------------------------------------------------------------
# Tool registry: readonly + memory + reflection ONLY
# ---------------------------------------------------------------------------

def test_reflection_registry_excludes_order_and_confirm_tools(tmp_path):
    """A reflection session must never see an order-placing or
    strategy-saving tool -- spec §②: "无下单、无确认类工具". Reflector wires
    up exactly register_readonly_tools + register_memory_tools +
    register_reflection_tools; this test enumerates the actual forbidden
    names by grepping the one place that registers them
    (agent/action_tools.py) rather than guessing, so it stays correct if a
    new confirm-gated tool is ever added there."""
    components = make_components(tmp_path)

    reflection_registry = ToolRegistry()
    register_readonly_tools(reflection_registry, data=components.data,
                            broker=components.broker, journal=components.journal,
                            strategies=components.strategies, queue=components.queue)
    # search=SessionSearch(...) -- Reflector._run registers it too (Task 4
    # review finding #1: session_search is advertised in
    # REFLECTION_INSTRUCTIONS's tool list but was never wired up).
    register_memory_tools(reflection_registry, memory=components.memory,
                          search=SessionSearch(components.conn))
    register_reflection_tools(reflection_registry, strategies=components.strategies,
                              queue=components.queue)
    reflection_names = {s.name for s in reflection_registry.specs()}

    action_registry = ToolRegistry()
    register_action_tools(action_registry, strategies=components.strategies,
                          executor=None, confirm=lambda _: False)
    forbidden_names = {s.name for s in action_registry.specs()}
    assert forbidden_names == {"draft_strategy", "propose_order"}

    assert reflection_names.isdisjoint(forbidden_names)
    assert reflection_names == {
        "get_quote", "get_bars", "web_search", "get_portfolio",
        "list_strategies", "read_strategy", "list_pending_reviews",
        "memory_update", "memory_read", "session_search",
        "propose_strategy_revision"}
