from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tradewind.broker.base import (
    Account, Broker, Order, OrderStatus, Position,
)
from tradewind.data.base import Bar, DataSource, Quote
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
                     as_of=datetime.now(timezone.utc))

    def get_bars(self, ticker, days=365):
        return []


class FakeBroker(Broker):
    name = "fake"
    is_paper = True

    def __init__(self, qty="10"):
        self.qty = Decimal(qty)

    def get_account(self):
        return Account(equity=Decimal("10000"), cash=Decimal("5000"),
                       buying_power=Decimal("10000"))

    def get_positions(self):
        if self.qty <= 0:
            return []
        return [Position(ticker="AAPL", qty=self.qty,
                         avg_entry_price=Decimal("180"),
                         market_value=self.qty * Decimal("200"),
                         unrealized_pl=Decimal("0"))]

    def get_order(self, order_id):
        raise NotImplementedError

    def get_orders(self, open_only=True):
        return []

    def submit_order(self, intent):
        raise NotImplementedError

    def cancel_order(self, order_id):
        pass


class SpyExecutor:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def execute(self, intent):
        if self.fail:
            raise ExecutionError("boom")
        self.calls.append(intent)
        from tradewind.execution import ExecutionResult
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
    s, store, ex, q, n = make(tmp_path, strategy_yaml(condition="price < 100"))
    report = s.run_once()
    assert report.strategies_checked == 1
    assert report.outcomes == [] and n.sent == [] and ex.calls == []


def test_hard_auto_executes_and_marks_triggered(tmp_path):
    s, store, ex, q, n = make(tmp_path, strategy_yaml())
    report = s.run_once()
    [o] = report.outcomes
    assert o.disposition == "executed"
    assert len(ex.calls) == 1 and ex.calls[0].qty == Decimal("10")
    assert store.load("t").rules[0].state == RuleState.TRIGGERED
    assert len(n.sent) == 1
    # second run: rule stays triggered, nothing happens
    assert s.run_once().outcomes == []


def test_soft_auto_enqueues(tmp_path):
    s, store, ex, q, n = make(tmp_path, strategy_yaml(rule_type="soft"))
    report = s.run_once()
    assert report.outcomes[0].disposition == "queued"
    assert ex.calls == [] and len(q.list()) == 1


def test_confirm_auth_enqueues_hard_rule(tmp_path):
    s, store, ex, q, n = make(tmp_path, strategy_yaml(auth="confirm"))
    assert s.run_once().outcomes[0].disposition == "queued"
    assert ex.calls == []


def test_notify_auth_only_notifies(tmp_path):
    s, store, ex, q, n = make(tmp_path, strategy_yaml(auth="notify"))
    assert s.run_once().outcomes[0].disposition == "notified"
    assert ex.calls == [] and q.list() == [] and len(n.sent) == 1


def test_no_position_sell_is_skipped(tmp_path):
    s, store, ex, q, n = make(tmp_path, strategy_yaml(), qty="0")
    report = s.run_once()
    assert report.outcomes[0].disposition == "skipped"
    assert store.load("t").rules[0].state == RuleState.TRIGGERED


def test_execution_error_reported_not_raised(tmp_path):
    s, store, ex, q, n = make(tmp_path, strategy_yaml(), fail=True)
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
