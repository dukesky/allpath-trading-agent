from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from tradewind.broker.base import (
    Account,
    Broker,
    Position,
)
from tradewind.data.base import DataSource, Quote
from tradewind.execution import ExecutionError
from tradewind.risk.gate import RiskDecision
from tradewind.sentinel import Sentinel
from tradewind.store.db import connect
from tradewind.store.reviews import ReviewQueue
from tradewind.strategy.model import RuleState
from tradewind.strategy.store import StrategyStore


def strategy_yaml(auth="auto", rule_type="hard", condition="price < 250",
                  action="sell all", status="active"):
    return f"""
name: "T"
status: {status}
authorization: {auth}
position: {{ticker: AAPL, target_weight: 15%}}
rules:
  - {{id: r1, type: {rule_type}, condition: "{condition}", action: "{action}"}}
"""


class FakeData(DataSource):
    def __init__(self, price="200"):
        self.price = Decimal(price)

    def get_quote(self, ticker):
        return Quote(ticker=ticker, price=self.price,
                     as_of=datetime.now(UTC))

    def get_bars(self, ticker, days=365):
        return []


class FakeBroker(Broker):
    name = "fake"
    is_paper = True

    def __init__(self, qty="10"):
        self.qty = Decimal(qty)

    def get_account(self):
        return Account(equity=Decimal(10000), cash=Decimal(5000),
                       buying_power=Decimal(10000))

    def get_positions(self):
        if self.qty <= 0:
            return []
        return [Position(ticker="AAPL", qty=self.qty,
                         avg_entry_price=Decimal(180),
                         market_value=self.qty * Decimal(200),
                         unrealized_pl=Decimal(0))]

    def get_order(self, order_id):
        raise NotImplementedError

    def get_orders(self, open_only=True):
        return []

    def submit_order(self, intent):
        raise NotImplementedError

    def cancel_order(self, order_id):
        pass


class SpyExecutor:
    def __init__(self, fail=False, raise_exc=None, reject_reasons=None):
        self.fail = fail
        self.raise_exc = raise_exc
        self.reject_reasons = reject_reasons
        self.calls = []

    def execute(self, intent):
        if self.fail:
            raise ExecutionError("boom")
        if self.raise_exc is not None:
            raise self.raise_exc
        self.calls.append(intent)
        from tradewind.execution import ExecutionResult
        if self.reject_reasons is not None:
            return ExecutionResult(
                submitted=False, order=None,
                decision=RiskDecision(approved=False, reasons=self.reject_reasons))
        return ExecutionResult(submitted=True, order=None,
                               decision=RiskDecision(approved=True))


class SpyNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))


def make(tmp_path: Path, yaml_text: str, *, price="200", qty="10", fail=False):
    (tmp_path / "t.yaml").write_text(yaml_text)
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    executor = SpyExecutor(fail=fail)
    queue = ReviewQueue(conn, executor)
    notifier = SpyNotifier()
    s = Sentinel(store, FakeData(price), FakeBroker(qty), executor, queue, notifier)
    return s, store, executor, queue, notifier


def test_no_trigger_no_noise(tmp_path):
    s, _store, ex, _q, n = make(tmp_path, strategy_yaml(condition="price < 100"))
    report = s.run_once()
    assert report.strategies_checked == 1
    assert report.outcomes == [] and n.sent == [] and ex.calls == []


def test_hard_auto_executes_and_marks_triggered(tmp_path):
    s, store, ex, _q, n = make(tmp_path, strategy_yaml())
    report = s.run_once()
    [o] = report.outcomes
    assert o.disposition == "executed"
    assert len(ex.calls) == 1 and ex.calls[0].qty == Decimal(10)
    assert store.load("t").rules[0].state == RuleState.TRIGGERED
    assert len(n.sent) == 1
    # second run: rule stays triggered, nothing happens
    assert s.run_once().outcomes == []


def test_soft_auto_enqueues(tmp_path):
    s, _store, ex, q, _n = make(tmp_path, strategy_yaml(rule_type="soft"))
    report = s.run_once()
    assert report.outcomes[0].disposition == "queued"
    assert ex.calls == [] and len(q.list()) == 1


def test_confirm_auth_enqueues_hard_rule(tmp_path):
    s, _store, ex, _q, _n = make(tmp_path, strategy_yaml(auth="confirm"))
    assert s.run_once().outcomes[0].disposition == "queued"
    assert ex.calls == []


def test_notify_auth_only_notifies(tmp_path):
    s, _store, ex, q, n = make(tmp_path, strategy_yaml(auth="notify"))
    assert s.run_once().outcomes[0].disposition == "notified"
    assert ex.calls == [] and q.list() == [] and len(n.sent) == 1


def test_no_position_sell_is_skipped(tmp_path):
    s, store, _ex, _q, _n = make(tmp_path, strategy_yaml(), qty="0")
    report = s.run_once()
    assert report.outcomes[0].disposition == "skipped"
    assert store.load("t").rules[0].state == RuleState.TRIGGERED


def test_execution_error_reported_not_raised(tmp_path):
    s, _store, _ex, _q, _n = make(tmp_path, strategy_yaml(), fail=True)
    report = s.run_once()
    assert report.outcomes[0].disposition == "error"
    assert "boom" in report.outcomes[0].detail


def test_bad_quote_collects_error_and_continues(tmp_path):
    class BadData(FakeData):
        def get_quote(self, ticker):
            raise ValueError("no price")

    (tmp_path / "t.yaml").write_text(strategy_yaml())
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    ex = SpyExecutor()
    s = Sentinel(store, BadData(), FakeBroker(), ex, ReviewQueue(conn, ex), SpyNotifier())
    report = s.run_once()
    assert report.errors and "no price" in report.errors[0]


def test_draft_strategy_ignored(tmp_path):
    s, *_ = make(tmp_path, strategy_yaml(status="draft"))
    assert s.run_once().strategies_checked == 0


def test_hard_auto_gate_rejected_reported_as_rejected_not_executed(tmp_path):
    (tmp_path / "t.yaml").write_text(strategy_yaml())
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    ex = SpyExecutor(reject_reasons=["exceeds max position size"])
    n = SpyNotifier()
    s = Sentinel(store, FakeData(), FakeBroker(), ex, ReviewQueue(conn, ex), n)
    report = s.run_once()
    [o] = report.outcomes
    assert o.disposition == "rejected"
    assert "exceeds max position size" in o.detail
    assert len(n.sent) == 1


def test_hard_auto_unexpected_exception_reported_as_error_not_raised(tmp_path):
    (tmp_path / "t.yaml").write_text(strategy_yaml())
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    ex = SpyExecutor(raise_exc=RuntimeError("db connection lost"))
    n = SpyNotifier()
    s = Sentinel(store, FakeData(), FakeBroker(), ex, ReviewQueue(conn, ex), n)
    report = s.run_once()  # must not raise
    [o] = report.outcomes
    assert o.disposition == "error"
    assert "db connection lost" in o.detail
    assert len(n.sent) == 1


def test_one_bad_yaml_does_not_halt_other_strategies(tmp_path):
    (tmp_path / "bad.yaml").write_text("name: x\nstatus: active\n")
    (tmp_path / "t.yaml").write_text(strategy_yaml(condition="price < 100"))
    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore(tmp_path, conn)
    ex = SpyExecutor()
    s = Sentinel(store, FakeData(), FakeBroker(), ex, ReviewQueue(conn, ex), SpyNotifier())
    report = s.run_once()
    assert report.strategies_checked == 1
    assert report.errors and any("bad.yaml" in e for e in report.errors)


class StubReviewAgent:
    def __init__(self, recommendation="execute", fail=False):
        self.recommendation = recommendation
        self.fail = fail

    def analyze(self, review):
        from tradewind.agent.review import ReviewAnalysis
        if self.fail:
            raise RuntimeError("llm down")
        return ReviewAnalysis(recommendation=self.recommendation, reasoning="because")


def test_auto_soft_with_agent_execute_recommendation(tmp_path):
    s, _store, ex, q, _n = make(tmp_path, strategy_yaml(rule_type="soft"))
    s.review_agent = StubReviewAgent("execute")
    report = s.run_once()
    assert report.outcomes[0].disposition == "executed"
    assert len(ex.calls) == 1
    row = q.get(1)
    assert row["status"] == "approved" and "because" in row["agent_analysis"]


def test_auto_soft_with_agent_skip_recommendation(tmp_path):
    s, _store, ex, q, _n = make(tmp_path, strategy_yaml(rule_type="soft"))
    s.review_agent = StubReviewAgent("skip")
    report = s.run_once()
    assert report.outcomes[0].disposition == "skipped"
    assert ex.calls == []
    assert q.get(1)["status"] == "rejected"


def test_confirm_with_agent_attaches_analysis_stays_queued(tmp_path):
    s, _store, ex, q, _n = make(tmp_path, strategy_yaml(auth="confirm"))
    s.review_agent = StubReviewAgent("execute")
    report = s.run_once()
    assert report.outcomes[0].disposition == "queued"
    row = q.get(1)
    assert row["status"] == "pending" and row["agent_analysis"]
    assert ex.calls == []


def test_agent_failure_leaves_trigger_queued(tmp_path):
    s, _store, _ex, q, _n = make(tmp_path, strategy_yaml(rule_type="soft"))
    s.review_agent = StubReviewAgent(fail=True)
    report = s.run_once()
    assert report.outcomes[0].disposition == "queued"
    assert "review failed" in report.outcomes[0].detail
    assert q.get(1)["status"] == "pending"
