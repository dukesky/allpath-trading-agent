from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from allpath_trade import reflect as reflect_module
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
    conversations: ConversationStore
    journal: TradeJournal
    observations: ObservationLog
    search: SessionSearch
    broker: Broker
    data: DataSource
    strategies: StrategyStore
    queue: ReviewQueue
    memory: MemoryStore
    account: str = "paper"


def make_components(tmp_path, broker=None, data=None, account="paper"):
    conn = connect(tmp_path / "db.sqlite")
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "t.yaml").write_text(STRAT)
    return FakeComponents(
        reports=ReportStore(conn),
        conn=conn,
        conversations=ConversationStore(conn),
        journal=TradeJournal(conn),
        observations=ObservationLog(conn),
        search=SessionSearch(conn),
        broker=broker if broker is not None else FakeBroker(),
        data=data if data is not None else FakeData(),
        strategies=StrategyStore(strategies_dir, conn),
        queue=ReviewQueue(conn, executor=None),
        memory=MemoryStore(tmp_path / "memory", conn),
        account=account)


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


def test_build_briefing_fill_line_includes_the_actual_fill_time_when_known():
    # Motivating bug: the agent once conflated submission ts with fill time,
    # 17 hours off. When filled_at is known, the briefing must show it
    # distinctly from the submission ts already at the start of the line.
    briefing = build_briefing(
        et_date=ET_DATE,
        trades=[{"ts": "2024-01-10T09:00:00+00:00", "side": "buy", "ticker": "AAPL",
                "qty": "10", "status": "filled", "filled_qty": "10",
                "filled_avg_price": "150.25", "filled_at": "2024-01-10T15:00:00+00:00",
                "strategy_id": "t"}],
        observations=[], positions=[], pending_counts={})
    assert "filled 10 @ 150.25 at 2024-01-10T15:00:00" in briefing


def test_build_briefing_fill_line_degrades_gracefully_without_filled_at():
    # Rows journaled before this column existed (or not yet refreshed) have
    # no filled_at -- the line must still read cleanly, no "at None".
    briefing = build_briefing(
        et_date=ET_DATE,
        trades=[{"ts": "2024-01-10T09:00:00+00:00", "side": "buy", "ticker": "AAPL",
                "qty": "10", "status": "filled", "filled_qty": "10",
                "filled_avg_price": "150.25", "strategy_id": "t"}],
        observations=[], positions=[], pending_counts={})
    assert "filled 10 @ 150.25" in briefing
    assert " at None" not in briefing


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


# -- shadow-dual-active T4 review Important 1: the briefing header states
# which account it reflects on --------------------------------------------

def test_build_briefing_defaults_to_paper_account_header():
    briefing = build_briefing(
        et_date=ET_DATE, trades=[], observations=[], positions=[], pending_counts={})
    assert "Account: paper" in briefing


def test_build_briefing_shadow_account_header():
    briefing = build_briefing(
        et_date=ET_DATE, trades=[], observations=[], positions=[], pending_counts={},
        account="shadow")
    assert "Account: shadow" in briefing
    assert "Account: paper" not in briefing


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


# -- shadow-dual-active T4 review Important 1: the Reflector wires its own
# bundle's account into BOTH the system prompt and the seed briefing, so a
# shadow reflection can never reason about the local ledger as if it were
# paper's real simulated execution. -----------------------------------------

def test_run_daily_paper_system_prompt_and_briefing_carry_the_paper_account(tmp_path):
    components = make_components(tmp_path, account="paper")
    llm = ScriptedLLM([LLMResponse(text=REPORT_TEXT)])
    reflector = Reflector(llm=llm, components=components, settings=make_settings())

    reflector.run_daily(now=NOW)

    system_prompt = llm.seen[0][0]["content"]
    assert "ACCOUNT: paper" in system_prompt
    assert "Alpaca paper sandbox" in system_prompt
    assert "LOCAL LEDGER" not in system_prompt

    briefing = llm.seen[0][1]["content"]
    assert "Account: paper" in briefing


def test_run_daily_shadow_system_prompt_and_briefing_carry_the_shadow_account(tmp_path):
    components = make_components(tmp_path, account="shadow")
    llm = ScriptedLLM([LLMResponse(text=REPORT_TEXT)])
    reflector = Reflector(llm=llm, components=components, settings=make_settings())

    reflector.run_daily(now=NOW)

    system_prompt = llm.seen[0][0]["content"]
    assert "ACCOUNT: shadow" in system_prompt
    assert "LOCAL LEDGER" in system_prompt
    assert "user executes them manually" in system_prompt
    assert "Alpaca paper sandbox" not in system_prompt

    briefing = llm.seen[0][1]["content"]
    assert "Account: shadow" in briefing


# ---------------------------------------------------------------------------
# notifier wiring (Task 5): send_report fires once on a successful ("ok")
# report, never on a failed one, and a notifier=None Reflector never crashes.
# ---------------------------------------------------------------------------

class _SpyNotifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls: list[tuple[str, str]] = []

    def send(self, subject, body):
        self.calls.append((subject, body))
        return self.ok


def test_run_daily_success_sends_notification_via_send_report(tmp_path, monkeypatch):
    calls = []

    def fake_send_report(notifier, subject, summary_body, full_body):
        calls.append((notifier, subject, summary_body, full_body))
        return True

    monkeypatch.setattr("allpath_trade.reflect.send_report", fake_send_report)
    components = make_components(tmp_path)
    insert_trade(components, ts="2024-01-10T15:00:00+00:00")
    notifier = _SpyNotifier()
    llm = ScriptedLLM([LLMResponse(text=REPORT_TEXT)])
    reflector = Reflector(llm=llm, components=components, settings=make_settings(),
                          notifier=notifier)

    status = reflector.run_daily(now=NOW)

    assert status.startswith("ok:")
    assert len(calls) == 1
    sent_notifier, subject, summary_body, full_body = calls[0]
    assert sent_notifier is notifier
    row = components.reports.get(ET_DATE)
    assert subject == f"[Paper] [AllPath] Daily reflection {ET_DATE}"
    assert summary_body == row["summary"]
    assert row["body"] in full_body


def test_run_daily_failed_report_does_not_notify(tmp_path):
    components = make_components(tmp_path)
    notifier = _SpyNotifier()
    llm = ScriptedLLM([
        LLMResponse(text="no structure here"),
        LLMResponse(text="still no structure"),
    ])
    reflector = Reflector(llm=llm, components=components, settings=make_settings(),
                          notifier=notifier)

    status = reflector.run_daily(now=NOW)

    assert "failed" in status
    assert notifier.calls == []


def test_run_daily_notifier_none_does_not_crash(tmp_path):
    components = make_components(tmp_path)
    llm = ScriptedLLM([LLMResponse(text=REPORT_TEXT)])
    reflector = Reflector(llm=llm, components=components, settings=make_settings(),
                          notifier=None)

    status = reflector.run_daily(now=NOW)

    assert status.startswith("ok:")


def test_run_daily_notification_push_failure_does_not_fail_the_run(tmp_path, capsys):
    components = make_components(tmp_path)
    notifier = _SpyNotifier(ok=False)
    llm = ScriptedLLM([LLMResponse(text=REPORT_TEXT)])
    reflector = Reflector(llm=llm, components=components, settings=make_settings(),
                          notifier=notifier)

    status = reflector.run_daily(now=NOW)

    assert status.startswith("ok:")
    assert len(notifier.calls) == 1
    assert "reflection" in capsys.readouterr().err.lower()


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


class SlowFakeData(DataSource):
    """Records every ticker it's actually asked to quote, sleeping past a
    (monkeypatched, tiny) QUOTES_BUDGET_SECONDS on the first call -- Finding
    F4's slow-Yahoo reproduction, without a real 10-second sleep in the test
    suite."""

    def __init__(self, sleep_seconds: float) -> None:
        self.sleep_seconds = sleep_seconds
        self.calls: list[str] = []

    def get_quote(self, ticker):
        self.calls.append(ticker)
        time.sleep(self.sleep_seconds)
        return Quote(ticker=ticker, price=Decimal(110), previous_close=Decimal(100),
                    as_of=NOW)

    def get_bars(self, ticker, days=365):
        return []


def test_positions_with_change_stops_quoting_once_the_deadline_is_spent(
        tmp_path, monkeypatch):
    """Finding F4: _positions_with_change must not let one hung
    `data.get_quote` call stall every position behind it. A single shared
    deadline (QUOTES_BUDGET_SECONDS) is checked before EACH call -- a
    position whose turn comes up after the budget is already spent must
    render "n/a" without even attempting a call, not just eventually time
    out on its own."""
    monkeypatch.setattr(reflect_module, "QUOTES_BUDGET_SECONDS", 0.05)
    slow_data = SlowFakeData(sleep_seconds=0.2)
    positions = [
        Position(ticker=t, qty=Decimal(1), avg_entry_price=Decimal(100),
                 market_value=Decimal(100), unrealized_pl=Decimal(0))
        for t in ("AAPL", "MSFT", "GOOG")]
    components = make_components(
        tmp_path, broker=FakeBroker(positions=positions), data=slow_data)
    reflector = Reflector(llm=None, components=components, settings=make_settings())

    result = reflector._positions_with_change()

    # Only the first position's call actually ran -- the deadline (0.05s)
    # was already blown by its 0.2s sleep before the second position's turn.
    assert slow_data.calls == ["AAPL"]
    by_ticker = {r["ticker"]: r["day_change"] for r in result}
    assert by_ticker["AAPL"] == "+10.00%"  # completed before the check fired
    assert by_ticker["MSFT"] == "n/a"
    assert by_ticker["GOOG"] == "n/a"


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


# ---------------------------------------------------------------------------
# I9: a `failed` row must not block the day's retry
# ---------------------------------------------------------------------------

def test_run_daily_retries_after_a_failed_row_and_replaces_it(tmp_path):
    """I9: the idempotency guard reads `exists_ok`, not `exists` -- a night
    whose first attempt stored `status="failed"` (LLM down at 16:05) must
    still be retried by the next tick, and the successful retry replaces
    the failed row instead of dying on the (account, date) UNIQUE."""
    components = make_components(tmp_path)
    failing = ScriptedLLM([LLMError("provider down")])
    Reflector(llm=failing, components=components, settings=make_settings()
              ).run_daily(now=NOW)
    assert components.reports.get(ET_DATE)["status"] == "failed"

    retry = ScriptedLLM([LLMResponse(text=REPORT_TEXT)])
    status = Reflector(llm=retry, components=components, settings=make_settings()
                       ).run_daily(now=NOW)

    assert status.startswith("ok:")
    assert len(retry.seen) == 1          # the retry really did call the LLM
    rows = components.reports.list()
    assert len(rows) == 1                # still one row for the day
    assert rows[0]["status"] == "ok"
    assert "Day summary" in rows[0]["body"]


def test_run_daily_still_skips_when_an_ok_row_exists(tmp_path):
    """The other half of I9: `exists_ok` must not weaken the guard for the
    case it was written for -- a stored SUCCESS still costs no LLM call."""
    components = make_components(tmp_path)
    llm = ScriptedLLM([LLMResponse(text=REPORT_TEXT)])
    reflector = Reflector(llm=llm, components=components, settings=make_settings())
    reflector.run_daily(now=NOW)
    assert reflector.run_daily(now=NOW).startswith("already ran")
    assert len(llm.seen) == 1


# ---------------------------------------------------------------------------
# I8: per-account wall-clock deadline on the reflection pass
# ---------------------------------------------------------------------------

class FakeClock:
    """A monotonic clock the test drives by hand -- no real sleeps, so the
    deadline tests stay instant and deterministic."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SlowLLM(ScriptedLLM):
    """ScriptedLLM that burns `seconds_per_call` of fake wall clock on every
    completion, standing in for a genuinely slow provider/tool round-trip."""

    def __init__(self, clock, seconds_per_call, responses):
        super().__init__(responses)
        self.clock = clock
        self.seconds_per_call = seconds_per_call

    def complete(self, messages, tools=None):
        self.clock.advance(self.seconds_per_call)
        return super().complete(messages, tools=tools)


def test_run_daily_deadline_forces_the_wrap_up_prompt(tmp_path, monkeypatch):
    """I8: a slow reflection must be bounded by wall clock, not only by the
    tool-call cap -- one hung account otherwise holds up the whole nightly
    chain. Crossing the deadline ends the research turn exactly the way the
    iteration cap does: unparseable text, then the one corrective wrap-up
    turn -- which is deliberately NOT deadline-gated, so the day still gets
    a real report instead of a `failed` row."""
    components = make_components(tmp_path)
    clock = FakeClock()
    monkeypatch.setattr(reflect_module, "_monotonic", clock)
    llm = SlowLLM(clock, 400, [
        tool_response("get_portfolio", {}),   # t+400, still inside 600s
        tool_response("get_portfolio", {}),   # t+800, deadline now blown
        LLMResponse(text=REPORT_TEXT),        # the wrap-up turn's answer
    ])
    settings = make_settings(reflection_max_iters=12,
                             reflection_deadline_seconds=600)

    status = Reflector(llm=llm, components=components, settings=settings
                       ).run_daily(now=NOW)

    assert status.startswith("ok:")
    assert components.reports.get(ET_DATE)["status"] == "ok"
    # Exactly 3 provider calls: two research iterations, then the wrap-up.
    # The deadline-expired third iteration never reached the provider --
    # without the deadline the cap would have allowed 12.
    assert len(llm.seen) == 3
    assert any(m["role"] == "user" and "Reproduce it now" in m["content"]
               for m in llm.seen[-1])


def test_run_daily_deadline_not_reached_leaves_the_pass_alone(tmp_path, monkeypatch):
    components = make_components(tmp_path)
    clock = FakeClock()
    monkeypatch.setattr(reflect_module, "_monotonic", clock)
    llm = SlowLLM(clock, 10, [
        tool_response("get_portfolio", {}),
        LLMResponse(text=REPORT_TEXT),
    ])
    settings = make_settings(reflection_deadline_seconds=600)

    status = Reflector(llm=llm, components=components, settings=settings
                       ).run_daily(now=NOW)

    assert status.startswith("ok:")
    assert len(llm.seen) == 2            # no corrective turn was needed


def test_run_daily_deadline_zero_disables_the_deadline(tmp_path, monkeypatch):
    """A deadline of 0 means "no wall-clock bound" -- the tool-call cap is
    then the only limit, exactly as it was before I8."""
    components = make_components(tmp_path)
    clock = FakeClock()
    monkeypatch.setattr(reflect_module, "_monotonic", clock)
    llm = SlowLLM(clock, 100_000, [
        tool_response("get_portfolio", {}),
        LLMResponse(text=REPORT_TEXT),
    ])
    settings = make_settings(reflection_deadline_seconds=0)

    status = Reflector(llm=llm, components=components, settings=settings
                       ).run_daily(now=NOW)

    assert status.startswith("ok:")
    assert len(llm.seen) == 2
