import pytest
from fastapi.testclient import TestClient

from allpath_trade.agent.review import ReviewAnalysis
from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from allpath_trade.web.routes import dashboard as dashboard_route
from tests.helpers import assert_english_only
from tests.test_sentinel import FakeBroker
from tests.test_web_dashboard import FakeDataSource


@pytest.fixture(autouse=True)
def _clear_quote_cache():
    # Same reasoning as test_web_dashboard.py's fixture of the same name:
    # the cache is module-level (survives across requests on purpose), so
    # it must be cleared between tests sharing a ticker (AAPL) or one
    # test's quote leaks into the next's assertions.
    dashboard_route._quote_cache.clear()
    yield
    dashboard_route._quote_cache.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        # Must be set before the first request -- otherwise the reviews
        # page's price-context lookup (Part B) hits the real, network-backed
        # YFinanceSource. Default price/previous_close deliberately unset
        # (both None) so existing tests that don't care about price context
        # see it degrade to "omitted" rather than asserting on a number.
        monkeypatch.setattr(c.app.state.holder.get(), "data", FakeDataSource())
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


# --- Part C: card readability -------------------------------------------

def test_chat_sourced_card_reason_is_not_the_bold_header(client):
    # Screenshot feedback: a chat proposal's `action` is the LLM's free-text
    # reason (order_sink.py sets action=intent.reason), which used to render
    # as the card's bold <strong> headline. It must now show up only in the
    # muted, capped .review-reason block -- not inside <strong>.
    long_reason = "The RSI dropped below 30 and price broke the 50-day moving average, " * 3
    queue_one(client, source="chat", strategy_id="", rule_id="",
             condition="proposed in conversation", action=long_reason)
    body = client.get("/reviews").text
    assert long_reason in body
    assert f"<strong>{long_reason}" not in body
    assert 'class="review-reason"' in body
    assert "Proposed trade · AAPL" in body


def test_non_chat_card_header_is_unaffected(client):
    queue_one(client)  # default source="sentinel", action="sell all"
    body = client.get("/reviews").text
    assert "<strong>sell all · AAPL</strong>" in body
    assert 'class="review-reason"' not in body


def test_agent_analysis_full_text_is_not_truncated(client):
    # agent/review.py no longer hard-cuts an unparseable analysis at 300
    # chars -- the full text must survive all the way to the rendered page.
    long_text = "x" * 500
    rid = queue_one(client)
    analysis = ReviewAnalysis(recommendation="skip",
                              reasoning=f"unparseable analysis: {long_text}")
    client.app.state.holder.get().queue.attach_analysis(rid, analysis.model_dump_json())
    body = client.get("/reviews").text
    assert long_text in body


def test_agent_analysis_renders_inside_a_details_disclosure(client):
    rid = queue_one(client)
    analysis = ReviewAnalysis(recommendation="execute", reasoning="price broke support")
    client.app.state.holder.get().queue.attach_analysis(rid, analysis.model_dump_json())
    body = client.get("/reviews").text
    assert "<details" in body
    assert "<summary" in body
    assert "price broke support" in body


# --- Part B: price context on pending order cards ------------------------

def test_pending_order_card_shows_trigger_and_current_price(client, monkeypatch):
    from tests.test_web_dashboard import FakeDataSource

    monkeypatch.setattr(client.app.state.holder.get(), "data",
                        FakeDataSource(price="210.00", previous_close="200.00"))
    queue_one(client)  # snapshot price "99"
    body = client.get("/reviews").text
    assert "Triggered at $99.00" in body
    assert "Now $210.00" in body
    assert "+5.0% today" in body


def test_pending_order_card_shows_deviation_since_trigger(client, monkeypatch):
    from tests.test_web_dashboard import FakeDataSource

    monkeypatch.setattr(client.app.state.holder.get(), "data",
                        FakeDataSource(price="110.00"))
    queue_one(client)  # snapshot price "99" -> +11.1% deviation
    body = client.get("/reviews").text
    assert "since trigger" in body


def test_pending_order_card_shows_est_shares_for_notional_intent(client, monkeypatch):
    from tests.test_web_dashboard import FakeDataSource

    monkeypatch.setattr(client.app.state.holder.get(), "data",
                        FakeDataSource(price="200.00"))
    queue_one(client, intent=OrderIntent(ticker="AAPL", side=OrderSide.BUY,
                                         notional="500", reason="r"))
    body = client.get("/reviews").text
    assert "2.50 shares at current price" in body


def test_pending_order_card_qty_intent_shows_no_est_shares_line(client, monkeypatch):
    from tests.test_web_dashboard import FakeDataSource

    monkeypatch.setattr(client.app.state.holder.get(), "data",
                        FakeDataSource(price="200.00"))
    queue_one(client)  # default intent is qty-based
    body = client.get("/reviews").text
    assert "shares at current price" not in body


def test_pending_order_card_market_note_reflects_is_market_hours(client, monkeypatch):
    from allpath_trade.web.routes import reviews as reviews_route

    monkeypatch.setattr(reviews_route, "order_price_context",
                        lambda *a, **k: {
                            "trigger_price": None, "current_price": None,
                            "day_change_pct": None, "price_class": "",
                            "deviation_pct": None, "est_shares": None,
                            "market_open": True})
    queue_one(client)
    body = client.get("/reviews").text
    assert "Market open — fills near current price" in body


def test_pending_order_card_quote_failure_omits_price_context_gracefully(client, monkeypatch):
    class FailingData:
        def get_quote(self, ticker):
            raise RuntimeError("no network")

        def get_bars(self, ticker, days=365):
            raise NotImplementedError

    monkeypatch.setattr(client.app.state.holder.get(), "data", FailingData())
    queue_one(client)  # has a trigger price in its snapshot
    r = client.get("/reviews")
    assert r.status_code == 200
    body = r.text
    assert "Triggered at $99.00" in body  # trigger price still shown
    assert "Now $" not in body  # current price gracefully omitted
    assert "shares at current price" not in body


def test_chat_sourced_review_card_has_no_trigger_price_but_has_current_price(client, monkeypatch):
    from tests.test_web_dashboard import FakeDataSource

    monkeypatch.setattr(client.app.state.holder.get(), "data",
                        FakeDataSource(price="210.00"))
    queue_one(client, source="chat", strategy_id="", rule_id="",
             condition="proposed in conversation",
             snapshot={"proposed_ts": "2024-01-01T00:00:00+00:00"})
    body = client.get("/reviews").text
    assert "Triggered at" not in body
    assert "Now $210.00" in body


def test_resolved_order_card_has_no_price_context(client, monkeypatch):
    from tests.test_web_dashboard import FakeDataSource

    monkeypatch.setattr(client.app.state.holder.get(), "data",
                        FakeDataSource(price="210.00"))
    rid = queue_one(client)
    client.post(f"/reviews/{rid}/reject")
    body = client.get("/reviews").text
    assert "review-price-context" not in body


CURRENT_S1_YAML = """\
name: "S1"
status: active
version: 1
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""

VALID_REVISION_YAML = """\
name: "S1"
status: active
version: 2
position: {ticker: AAPL, target_weight: 10%}
rules:
  - {id: r1, type: hard, condition: "price < 90", action: "sell all"}
"""


def queue_revision(client, strategy_id="s1", old_yaml=CURRENT_S1_YAML,
                   new_yaml=VALID_REVISION_YAML, rationale="reflection rationale") -> int:
    # The applier's staleness gate (Finding 1) compares the file's CURRENT
    # text against the proposal's recorded base -- so a test that wants
    # `approve` to actually succeed has to make sure a real file with
    # exactly `old_yaml` exists on disk, not just a queue row that claims it
    # did.
    strategies_dir = client.app.state.holder.get().strategies.directory
    path = strategies_dir / f"{strategy_id}.yaml"
    if not path.exists():
        path.write_text(old_yaml)
    q = client.app.state.holder.get().queue
    return q.add_strategy_revision(
        strategy_id=strategy_id, ticker="AAPL", old_yaml=old_yaml, new_yaml=new_yaml,
        diff="d", rationale=rationale)


def test_approve_revision_applies_it_and_shows_success_note(client):
    rid = queue_revision(client)
    r = client.post(f"/reviews/{rid}/approve", follow_redirects=False)
    assert r.status_code == 303  # not a 500 from `result.submitted` on None
    row = client.app.state.holder.get().queue.get(rid)
    assert row["status"] == "approved"
    strategies_dir = client.app.state.holder.get().strategies.directory
    assert (strategies_dir / "s1.yaml").read_text() == VALID_REVISION_YAML

    # Finding 4: a successful approve goes through the `notice` channel, not
    # `error` -- the page renders it with `.flash-ok`, not red error styling.
    assert "notice=" in r.headers["location"]
    assert "error=" not in r.headers["location"]
    body = client.get(r.headers["location"]).text
    assert "Revision applied to s1." in body
    assert 'class="flash-ok"' in body


def test_stale_sibling_proposal_approve_does_not_revert_the_first_approval(client):
    # Route-level reproduce of Finding 1: two proposals drafted from the
    # same base. Approving the first must succeed; approving the second
    # afterward must fail re-validation and leave the file exactly as the
    # first approval left it.
    tightened = VALID_REVISION_YAML
    rid_a = queue_revision(client, new_yaml=tightened)
    sibling = CURRENT_S1_YAML.replace("version: 1", "version: 2").replace(
        "target_weight: 15%", "target_weight: 20%")
    rid_b = queue_revision(client, new_yaml=sibling)

    client.post(f"/reviews/{rid_a}/approve", follow_redirects=False)
    strategies_dir = client.app.state.holder.get().strategies.directory
    assert (strategies_dir / "s1.yaml").read_text() == tightened

    r = client.post(f"/reviews/{rid_b}/approve", follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]

    row_b = client.app.state.holder.get().queue.get(rid_b)
    assert row_b["status"] == "pending"
    assert (strategies_dir / "s1.yaml").read_text() == tightened


def test_approve_stale_revision_leaves_it_pending_with_a_message(client):
    # Missing `position` -- fails re-validation in the applier.
    rid = queue_revision(client, new_yaml="name: Bad\nstatus: active\n")
    r = client.post(f"/reviews/{rid}/approve", follow_redirects=False)
    assert r.status_code == 303  # not a 500

    row = client.app.state.holder.get().queue.get(rid)
    assert row["status"] == "pending"

    body = client.get(r.headers["location"]).text
    assert "pending" in body.lower()

    # still resolvable -- the whole point of leaving it pending
    client.post(f"/reviews/{rid}/reject", follow_redirects=False)
    assert client.app.state.holder.get().queue.get(rid)["status"] == "rejected"


def test_pending_revision_card_does_not_say_triggered_on(client):
    rid = queue_revision(client)
    body = client.get("/reviews").text
    assert f"#{rid}" in body
    assert "revision" in body.lower()
    assert "triggered on" not in body.lower()


def test_revision_card_is_english_only(client):
    # Finding 7: the shared English-only invariant (see helpers.py) had
    # never actually been exercised against a rendered strategy_revision
    # card specifically -- only against order-review cards.
    rid = queue_revision(client)
    body = client.get("/reviews").text
    assert f"#{rid}" in body
    assert_english_only(body)


def test_pending_revision_card_shows_regenerated_diff(client):
    # Task 6: the card renders a diff regenerated at render time (against
    # the CURRENT file) rather than trusting the stored `diff` field --
    # here the file matches the recorded base, so the regenerated diff is
    # the "normal" case and should still show the real +/- content.
    queue_revision(client)
    body = client.get("/reviews").text
    assert 'class="diff-add"' in body
    assert 'class="diff-del"' in body
    assert "target_weight: 10%" in body  # proposed (added)
    assert "target_weight: 15%" in body  # current (removed)


def test_pending_revision_card_shows_stale_warning_when_file_changed(client):
    rid = queue_revision(client)
    strategies_dir = client.app.state.holder.get().strategies.directory
    # An intervening edit after the proposal was drafted -- the file's
    # current text no longer matches the proposal's recorded base
    # (`old_yaml`), the exact scenario `apply_revision_factory`'s
    # base-match gate rejects at approval time (see reflection_tools.py).
    (strategies_dir / "s1.yaml").write_text(
        CURRENT_S1_YAML.replace("target_weight: 15%", "target_weight: 20%"))
    body = client.get("/reviews").text
    assert "has changed since this proposal" in body
    # The regenerated diff reflects the file as it is NOW, not the stale
    # recorded base -- 20% (current), not 15% (stale base), should appear.
    assert "target_weight: 20%" in body

    # Approving still fails safely and leaves the row pending -- the card's
    # warning isn't lying about what clicking Approve would do.
    r = client.post(f"/reviews/{rid}/approve", follow_redirects=False)
    assert r.status_code == 303
    assert client.app.state.holder.get().queue.get(rid)["status"] == "pending"


def test_approve_revision_warns_when_the_revised_rule_is_still_triggered(client):
    # Finding F2: the applier writes the revision's YAML verbatim and never
    # touches rule_states -- a rule that fired (state TRIGGERED) before the
    # reflection agent proposed a tightened version of that SAME rule id
    # stays TRIGGERED after this approval, silently, unless the flash notice
    # says so. Never auto re-arms (re-arming could re-fire a stop against an
    # already-sold position) -- purely informational.
    from allpath_trade.strategy.model import RuleState

    rid = queue_revision(client)
    strategies = client.app.state.holder.get().strategies
    strategies.set_rule_state("s1", "r1", RuleState.TRIGGERED)

    r = client.post(f"/reviews/{rid}/approve", follow_redirects=False)
    assert r.status_code == 303
    body = client.get(r.headers["location"]).text

    assert "Revision applied to s1." in body
    assert "r1 is still triggered" in body
    assert "re-arm" in body
    # Never silently re-armed.
    assert strategies.load("s1").rules[0].state == RuleState.TRIGGERED


def test_approve_revision_omits_the_note_when_the_rule_is_armed(client):
    rid = queue_revision(client)
    r = client.post(f"/reviews/{rid}/approve", follow_redirects=False)
    body = client.get(r.headers["location"]).text
    assert "Revision applied to s1." in body
    assert "re-arm" not in body


def test_resolved_revision_card_shows_recorded_diff_not_a_live_recompute(client):
    rid = queue_revision(client)
    client.post(f"/reviews/{rid}/reject", follow_redirects=False)
    strategies_dir = client.app.state.holder.get().strategies.directory
    # An unrelated edit made well after resolution -- a live recompute
    # against this would be misleading for a row that's already closed.
    (strategies_dir / "s1.yaml").write_text(
        CURRENT_S1_YAML.replace("target_weight: 15%", "target_weight: 99%"))
    body = client.get("/reviews").text
    assert ">d</span>" in body  # the recorded snapshot diff (queue_revision's diff="d")
    assert "target_weight: 99%" not in body
