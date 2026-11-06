"""`allpath-trade reviews --account X reanalyze <id>` (ops 2026-11-06): re-run
the review agent on a pending order review that went out with no analysis
(the LLM was down). Pending `kind='order'` rows only."""

from __future__ import annotations

import json

import pytest

from allpath_trade.agent.review import ReviewAgent, ReviewAnalysis
from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.cli import cmd_reviews_reanalyze, main
from allpath_trade.llm.base import LLMError
from allpath_trade.store.db import connect
from allpath_trade.store.reviews import ReviewQueue
from tests.test_cli import FakeBroker


class StubAgent:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.seen: list[dict] = []

    def analyze(self, review):
        self.seen.append(review)
        if self.fail:
            raise LLMError("llm request failed: Error code: 402")
        return ReviewAnalysis(recommendation="skip", reasoning="REANALYZEDMARK")


def add_order(q: ReviewQueue, **over) -> int:
    kwargs = {"strategy_id": "s1", "rule_id": "r1", "ticker": "NVDA",
              "rule_type": "hard", "condition": "price < 213",
              "action": "buy $3000", "snapshot": {"price": "191"},
              "intent": OrderIntent(ticker="NVDA", side=OrderSide.BUY,
                                    notional="3000", reason="r1")}
    kwargs.update(over)
    return int(q.add(**kwargs))


@pytest.fixture
def queue(tmp_path):
    return ReviewQueue(connect(tmp_path / "t.db"), None)


def test_reanalyze_attaches_a_fresh_analysis_to_a_pending_order(queue, capsys):
    rid = add_order(queue)
    agent = StubAgent()
    assert cmd_reviews_reanalyze(queue, agent, rid) == 0
    assert agent.seen[0]["id"] == rid
    stored = json.loads(queue.get(rid)["agent_analysis"])
    assert stored["reasoning"] == "REANALYZEDMARK"
    assert queue.get(rid)["status"] == "pending"   # never approves or rejects
    out = capsys.readouterr().out
    assert "skip" in out and "REANALYZEDMARK" in out


def test_reanalyze_refuses_a_resolved_row(queue, capsys):
    rid = add_order(queue)
    queue.reject(rid, note="no")
    agent = StubAgent()
    assert cmd_reviews_reanalyze(queue, agent, rid) == 1
    assert agent.seen == []
    assert "not pending" in capsys.readouterr().err


def test_reanalyze_refuses_a_non_order_kind(queue, capsys):
    rid = int(queue.add_strategy_revision(
        strategy_id="s1", ticker="NVDA", old_yaml="a: 1\n", new_yaml="a: 2\n",
        diff="", rationale="r"))
    agent = StubAgent()
    assert cmd_reviews_reanalyze(queue, agent, rid) == 1
    assert agent.seen == []
    assert "order" in capsys.readouterr().err


def test_reanalyze_reports_a_missing_row(queue, capsys):
    assert cmd_reviews_reanalyze(queue, StubAgent(), 999) == 1
    assert "999" in capsys.readouterr().err


def test_reanalyze_without_an_llm_says_so(queue, capsys):
    rid = add_order(queue)
    assert cmd_reviews_reanalyze(queue, None, rid) == 2
    assert "LLM not configured" in capsys.readouterr().err


def test_reanalyze_llm_failure_leaves_the_row_untouched(queue, capsys):
    rid = add_order(queue)
    assert cmd_reviews_reanalyze(queue, StubAgent(fail=True), rid) == 1
    assert queue.get(rid)["agent_analysis"] is None
    assert "402" in capsys.readouterr().err


def test_main_builds_the_accounts_review_agent_like_serve(tmp_path, capsys, monkeypatch):
    # End to end through main(): the agent is the one build_components wires
    # onto that account's sentinel (review tier, that account's memory).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("REVIEW_MODEL", "REVIEWTIERMODEL")
    conn = connect(tmp_path / "allpath-trade.db")
    rid = add_order(ReviewQueue(conn, None, account="shadow"))
    conn.close()

    seen: dict = {}

    def fake_analyze(self, review):
        seen["model"] = self.llm.model
        seen["memory_account"] = self.memory.account
        seen["review_id"] = review["id"]
        return ReviewAnalysis(recommendation="execute", reasoning="E2EMARK")

    monkeypatch.setattr(ReviewAgent, "analyze", fake_analyze)
    code = main(["reviews", "--account", "shadow", "reanalyze", str(rid)],
                broker_factory=lambda s: FakeBroker())
    assert code == 0, capsys.readouterr().err
    assert seen == {"model": "REVIEWTIERMODEL", "memory_account": "shadow",
                    "review_id": rid}
    conn = connect(tmp_path / "allpath-trade.db")
    row = ReviewQueue(conn, None, account="shadow").get(rid)
    assert "E2EMARK" in row["agent_analysis"]
