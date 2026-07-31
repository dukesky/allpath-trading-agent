import json
from decimal import Decimal

import pytest

from tradewind.broker.base import OrderIntent, OrderSide
from tradewind.store.db import connect
from tradewind.store.reviews import ReviewError, ReviewQueue


class StubExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, intent):
        self.calls.append(intent)
        from tradewind.execution import ExecutionResult
        from tradewind.risk.gate import RiskDecision
        return ExecutionResult(submitted=True, order=None,
                               decision=RiskDecision(approved=True))


INTENT = OrderIntent(ticker="AAPL", side=OrderSide.BUY, notional=Decimal("500"),  # noqa: FURB157
                     reason="dip", strategy_id="s1")


@pytest.fixture()
def queue(tmp_path):
    return ReviewQueue(connect(tmp_path / "t.db"), StubExecutor())


def add(queue, intent=INTENT):
    return queue.add(strategy_id="s1", rule_id="r1", ticker="AAPL",
                     rule_type="soft", condition="price < 205", action="buy $500",
                     snapshot={"price": Decimal("204.5")}, intent=intent)


def test_add_and_list(queue):
    rid = add(queue)
    [row] = queue.list()
    assert row["id"] == rid and row["status"] == "pending"
    assert json.loads(row["snapshot"])["price"] == "204.5"


def test_approve_executes_and_resolves(queue):
    rid = add(queue)
    result = queue.approve(rid)
    assert result.submitted
    assert queue._executor.calls[0].ticker == "AAPL"
    assert queue._executor.calls[0].notional == Decimal("500")  # noqa: FURB157
    row = queue.get(rid)
    assert row["status"] == "approved" and row["resolved_ts"]
    assert queue.list() == []


def test_reject(queue):
    rid = add(queue)
    queue.reject(rid, note="not now")
    row = queue.get(rid)
    assert row["status"] == "rejected" and row["resolution_note"] == "not now"


def test_approve_twice_raises(queue):
    rid = add(queue)
    queue.approve(rid)
    with pytest.raises(ReviewError):
        queue.approve(rid)


def test_double_approve_claims_atomically(queue):
    rid = add(queue)
    queue.approve(rid)
    with pytest.raises(ReviewError):
        queue.approve(rid)
    # executor must have run exactly once
    assert len(queue._executor.calls) == 1


def test_approve_without_intent_raises(queue):
    rid = add(queue, intent=None)
    with pytest.raises(ReviewError):
        queue.approve(rid)


def test_get_missing_raises(queue):
    with pytest.raises(ReviewError):
        queue.get(999)
