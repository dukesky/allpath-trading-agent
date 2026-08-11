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
from allpath_trade.memory.store import MemoryStore
from allpath_trade.reflect import Reflector, build_briefing
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
    assert briefing.count("<external-content>") == 4
    assert briefing.count("</external-content>") == 4
    assert "order: 2" in briefing


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
    settings = make_settings(reflection_max_iters=1)
    # max_iters=1: the first run_turn burns its single iteration on a tool
    # call and returns LIMIT_NOTICE without ever producing REPORT/SUMMARY.
    llm = ScriptedLLM([
        tool_response("get_portfolio", {}),
        LLMResponse(text=REPORT_TEXT),
    ])
    reflector = Reflector(llm=llm, components=components, settings=settings)
    status = reflector.run_daily(now=NOW)
    assert status.startswith("ok:")
    row = components.reports.get(ET_DATE)
    assert row["status"] == "ok"


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
    register_memory_tools(reflection_registry, memory=components.memory)
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
        "memory_update", "memory_read", "propose_strategy_revision"}
