import json

import pytest
from fastapi.testclient import TestClient

from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from tests.test_sentinel import FakeBroker


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        c.post("/login", data={"token": "secret"})
        yield c


def queue_one(client, **over) -> int:
    q = client.app.state.holder.get().queue
    kwargs = {"strategy_id": "s1", "rule_id": "r1", "ticker": "AAPL",
              "rule_type": "soft", "condition": "price < 100",
              "action": "sell all", "snapshot": {"price": "99"},
              "intent": OrderIntent(ticker="AAPL", side=OrderSide.SELL,
                                    qty="1", reason="rule r1")}
    kwargs.update(over)
    return q.add(**kwargs)


def test_pending_items_are_listed(client):
    queue_one(client)
    body = client.get("/reviews").text
    assert "AAPL" in body and "Approve" in body


def test_agent_analysis_is_shown(client):
    rid = queue_one(client)
    client.app.state.holder.get().queue.attach_analysis(
        rid, json.dumps({"recommend": "execute", "reasoning": "guidance raised",
                         "sources": ["https://example.com/pr"]}))
    body = client.get("/reviews").text
    assert "guidance raised" in body


def test_approve_executes_through_the_queue(client):
    rid = queue_one(client)
    r = client.post(f"/reviews/{rid}/approve", follow_redirects=False)
    assert r.status_code in (200, 303)
    row = client.app.state.holder.get().queue.get(rid)
    assert row["status"] == "approved"


def test_reject_records_the_decision(client):
    rid = queue_one(client)
    client.post(f"/reviews/{rid}/reject", follow_redirects=False)
    assert client.app.state.holder.get().queue.get(rid)["status"] == "rejected"


def test_approving_twice_reports_an_error_rather_than_executing_again(client):
    rid = queue_one(client)
    client.post(f"/reviews/{rid}/approve")
    r = client.post(f"/reviews/{rid}/approve")
    assert r.status_code == 200
    assert "not pending" in r.text.lower()


def test_approval_goes_through_the_queue_not_the_executor(client, monkeypatch):
    # Only ReviewQueue.approve writes execution_result and flips the status.
    # If the route ever reached the executor directly, the row would stay
    # pending with an empty execution_result while an order went out.
    rid = queue_one(client)
    client.post(f"/reviews/{rid}/approve")
    row = client.app.state.holder.get().queue.get(rid)
    assert row["status"] == "approved"
    assert row["execution_result"]


def test_a_failing_queue_approve_means_nothing_is_executed(client, monkeypatch):
    executed: list = []
    monkeypatch.setattr(client.app.state.holder.get().executor, "execute",
                        lambda intent: executed.append(intent))
    rid = queue_one(client)
    client.app.state.holder.get().queue.reject(rid, "already handled")
    r = client.post(f"/reviews/{rid}/approve")
    assert "not pending" in r.text.lower()
    assert executed == []
