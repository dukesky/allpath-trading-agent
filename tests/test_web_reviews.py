import pytest
from fastapi.testclient import TestClient

from allpath_trade.agent.review import ReviewAnalysis
from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from tests.helpers import assert_english_only
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
    # Built by round-tripping through the real ReviewAnalysis model (the
    # shape sentinel.py/review.py actually produce), not a hand-written
    # dict -- a hand-written `{"recommend": ...}` fixture is exactly what
    # let Finding 2 (the template reading the wrong field) go unnoticed:
    # the old fixture happened to match the template's typo instead of the
    # model's real field name.
    rid = queue_one(client)
    analysis = ReviewAnalysis(recommendation="execute", reasoning="guidance raised",
                              sources=["https://example.com/pr"])
    client.app.state.holder.get().queue.attach_analysis(rid, analysis.model_dump_json())
    body = client.get("/reviews").text
    assert "guidance raised" in body
    assert "Agent recommends:</strong> execute" in body
    assert "no recommendation" not in body


def test_reviews_page_is_english_only(client):
    rid = queue_one(client)
    analysis = ReviewAnalysis(recommendation="execute", reasoning="guidance raised",
                              sources=["https://example.com/pr"])
    client.app.state.holder.get().queue.attach_analysis(rid, analysis.model_dump_json())
    assert_english_only(client.get("/reviews").text)


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
    # Nothing was claimed by this second click (it's a no-op, not a broker
    # call that may or may not have gone through) -- the message says so.
    assert "not processed" in r.text.lower()


def test_approval_goes_through_the_queue_not_the_executor(client):
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


def test_gate_rejection_is_visible_now_and_on_a_later_visit(client):
    # notional far above RiskLimits.max_order_value(5000) and above the
    # FakeBroker AAPL position value (qty=10 * $200 = $2000): the gate
    # rejects deterministically, with no dependency on live quote data
    # (notional intents skip the price fetch).
    rid = queue_one(client, intent=OrderIntent(
        ticker="AAPL", side=OrderSide.SELL, notional="10000", reason="rule r1"))
    r = client.post(f"/reviews/{rid}/approve")
    assert r.status_code == 200
    assert "rejected by the risk gate" in r.text.lower()

    row = client.app.state.holder.get().queue.get(rid)
    # The review WAS claimed -- approve() flips status before executing --
    # even though the order never went out.
    assert row["status"] == "approved"
    assert row["execution_result"]

    # This is Finding 1: reloading the page later must still be able to
    # tell "blocked by the gate" apart from "the order was filled", even
    # though both persist as status == "approved".
    body = client.get("/reviews").text
    assert "blocked by the risk gate" in body.lower()
    assert "order submitted" not in body.lower()


def test_execution_failure_is_visible_now_and_on_a_later_visit(client, monkeypatch):
    def _boom(intent):
        raise ConnectionError("connection reset")

    monkeypatch.setattr(client.app.state.holder.get().broker, "submit_order", _boom)
    # notional intent: passes the gate (small, within the AAPL position
    # value) and skips the price fetch, so the only way to reach the broker
    # call -- and thus the patched failure -- is deterministic.
    rid = queue_one(client, intent=OrderIntent(
        ticker="AAPL", side=OrderSide.SELL, notional="100", reason="rule r1"))
    r = client.post(f"/reviews/{rid}/approve")
    assert r.status_code == 200
    assert "review claimed, but execution failed" in r.text.lower()
    assert "connection reset" in r.text.lower()

    row = client.app.state.holder.get().queue.get(rid)
    # Claimed, unlike a ReviewError -- this is the state Finding 3 is about:
    # the user must be told the review is claimed, not free to retry.
    assert row["status"] == "approved"

    body = client.get("/reviews").text
    assert "execution failed" in body.lower()
    assert "connection reset" in body.lower()
    assert "order submitted" not in body.lower()


def test_malicious_source_scheme_is_rendered_as_text_not_a_link(client):
    rid = queue_one(client)
    analysis = ReviewAnalysis(recommendation="execute", reasoning="x",
                              sources=["javascript:alert(1)"])
    client.app.state.holder.get().queue.attach_analysis(rid, analysis.model_dump_json())
    body = client.get("/reviews").text
    assert "javascript:alert(1)" in body
    assert '<a href="javascript:alert(1)"' not in body


def test_http_source_is_rendered_as_a_safe_link(client):
    rid = queue_one(client)
    analysis = ReviewAnalysis(recommendation="execute", reasoning="x",
                              sources=["https://example.com/pr"])
    client.app.state.holder.get().queue.attach_analysis(rid, analysis.model_dump_json())
    body = client.get("/reviews").text
    assert 'href="https://example.com/pr"' in body
    assert 'rel="noopener noreferrer"' in body


def test_chat_sourced_review_does_not_render_a_bare_strategy_slash_rule(client):
    # Chat-originated proposals (allpath_trade/web/order_sink.py) have empty
    # strategy_id/rule_id -- the card must say something sensible for them
    # instead of the sentinel-card's "{strategy_id}/{rule_id}" fragment.
    queue_one(client, source="chat", strategy_id="", rule_id="",
             condition="proposed in conversation")
    body = client.get("/reviews").text
    assert "/ — triggered" not in body
    assert "from chat" in body.lower()
