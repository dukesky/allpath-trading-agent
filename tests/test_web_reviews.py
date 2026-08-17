import threading
import time

import pytest
from fastapi.testclient import TestClient

from allpath_trade.agent.review import ReviewAnalysis
from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from allpath_trade.web.routes import dashboard as dashboard_route
from allpath_trade.web.routes.reviews import split_diff_rows
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
    # Task 6 / split-diff-round: the card renders a diff regenerated at
    # render time (against the CURRENT file) rather than trusting the
    # stored `diff` field -- here the file matches the recorded base, so
    # the regenerated diff is the "normal" case and should still show the
    # real added/removed content, now as split-diff table cells.
    queue_revision(client)
    body = client.get("/reviews").text
    assert 'class="add"' in body
    assert 'class="del"' in body
    assert "Current" in body and "Proposed" in body  # pending column labels
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


def test_resolved_revision_card_shows_frozen_diff_not_a_live_recompute(client):
    rid = queue_revision(client)
    client.post(f"/reviews/{rid}/reject", follow_redirects=False)
    strategies_dir = client.app.state.holder.get().strategies.directory
    # An unrelated edit made well after resolution -- a live recompute
    # against this would be misleading for a row that's already closed.
    (strategies_dir / "s1.yaml").write_text(
        CURRENT_S1_YAML.replace("target_weight: 15%", "target_weight: 99%"))
    body = client.get("/reviews").text
    # split-diff-round: a resolved row's diff is built from the frozen
    # snapshot old_yaml/new_yaml (not the raw stored `diff` field, and
    # never a live recompute), labelled honestly as the base the proposal
    # was actually drafted against rather than "Current".
    assert "Base (at proposal)" in body
    assert "target_weight: 15%" in body  # frozen base
    assert "target_weight: 10%" in body  # frozen proposed
    assert "target_weight: 99%" not in body


def test_revision_card_escapes_script_tags_in_yaml_lines(client):
    # No |safe anywhere in the split-diff render path -- a proposal whose
    # YAML happens to contain something that looks like markup must come
    # out as inert escaped text, same as every other server-rendered value
    # on this page.
    rid = queue_revision(
        client,
        old_yaml=CURRENT_S1_YAML,
        new_yaml=CURRENT_S1_YAML.replace(
            'target_weight: 15%', 'target_weight: "<script>alert(1)</script>"'))
    body = client.get("/reviews").text
    assert f"#{rid}" in body
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


# --- I1: /reviews' price-context loop pays one shared budget, not N *
# BROKER_TIMEOUT_SECONDS -------------------------------------------------

def test_reviews_page_renders_within_budget_when_every_quote_hangs(client, monkeypatch):
    # Measured before the fix: 7 pending rows against a stalled data source
    # cost 22.3s serially (each row paying its own BROKER_TIMEOUT_SECONDS).
    # _attach_price_context now shares one QUOTES_BUDGET_SECONDS deadline
    # across the whole loop (dashboard.py's own pattern), so rows past the
    # deadline omit price context instead of each queuing another call.
    monkeypatch.setattr(dashboard_route, "BROKER_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(dashboard_route, "QUOTES_BUDGET_SECONDS", 0.05)
    data = client.app.state.holder.get().data
    release = threading.Event()

    def hang(ticker):
        release.wait(timeout=5)
        raise RuntimeError("never resolves in time")

    monkeypatch.setattr(data, "get_quote", hang)
    dashboard_route._quote_cache.clear()

    for _ in range(7):
        queue_one(client)

    start = time.monotonic()
    r = client.get("/reviews")
    elapsed = time.monotonic() - start

    assert r.status_code == 200
    # 7 rows * 0.3s BROKER_TIMEOUT_SECONDS would be 2.1s without the shared
    # budget; the budget bounds total time to roughly one timeout.
    assert elapsed < 1.0
    release.set()  # let the background call finish so it doesn't linger


def test_resolved_rows_are_never_passed_through_attach_price_context(client, monkeypatch):
    # M7: the no-op `_attach_price_context(c, recent)` call was dropped --
    # resolved rows simply never get a `price_context` key now, and the
    # card template already treats a missing key as "omit gracefully"
    # (same as an explicit None), so the section renders exactly as before.
    from allpath_trade.web.routes import reviews as reviews_route

    calls: list[list] = []
    original = reviews_route._attach_price_context

    def spy(c, items):
        calls.append(list(items))
        original(c, items)

    rid = queue_one(client)
    client.post(f"/reviews/{rid}/reject", follow_redirects=False)

    monkeypatch.setattr(reviews_route, "_attach_price_context", spy)
    body = client.get("/reviews").text

    assert len(calls) == 1  # called once (pending items), not twice for recent
    assert "review-price-context" not in body


# --- split-diff-round: split_diff_rows unit tests ---------------------------

def test_split_diff_rows_no_changes_collapses_to_a_single_row():
    text = "a\nb\nc\n"
    rows = split_diff_rows(text, text)
    assert rows == [{"kind": "none", "text": "No changes"}]


def test_split_diff_rows_replace_pairs_lines_side_by_side():
    old = "x\nsame\ny\n"
    new = "1\nsame\n2\n"
    rows = split_diff_rows(old, new)
    changed = [r for r in rows if r["kind"] == "row" and not r["ctx"]]
    assert {"cls": "del", "text": "x"} == changed[0]["left"]
    assert {"cls": "add", "text": "1"} == changed[0]["right"]
    assert {"cls": "del", "text": "y"} == changed[1]["left"]
    assert {"cls": "add", "text": "2"} == changed[1]["right"]


def test_split_diff_rows_replace_pads_the_shorter_side_with_empty_cells():
    old = "one\ntwo\nthree\n"
    new = "uno\n"
    rows = split_diff_rows(old, new)
    changed = [r for r in rows if r["kind"] == "row" and not r["ctx"]]
    assert len(changed) == 3  # zip_longest over the longer (3-line) side
    assert changed[0]["left"] == {"cls": "del", "text": "one"}
    assert changed[0]["right"] == {"cls": "add", "text": "uno"}
    # "uno" is the only new line -- the rest of the pairing is delete-only,
    # padded on the right with an empty cell.
    assert changed[1]["left"] == {"cls": "del", "text": "two"}
    assert changed[1]["right"] == {"cls": "empty", "text": ""}
    assert changed[2]["left"] == {"cls": "del", "text": "three"}
    assert changed[2]["right"] == {"cls": "empty", "text": ""}


def test_split_diff_rows_pure_insert_pads_the_left_side():
    rows = split_diff_rows("a\n", "a\nb\n")
    changed = [r for r in rows if r["kind"] == "row" and not r["ctx"]]
    assert changed == [{"kind": "row", "ctx": False,
                        "left": {"cls": "empty", "text": ""},
                        "right": {"cls": "add", "text": "b"}}]


def test_split_diff_rows_pure_delete_pads_the_right_side():
    rows = split_diff_rows("a\nb\n", "a\n")
    changed = [r for r in rows if r["kind"] == "row" and not r["ctx"]]
    assert changed == [{"kind": "row", "ctx": False,
                        "left": {"cls": "del", "text": "b"},
                        "right": {"cls": "empty", "text": ""}}]


def test_split_diff_rows_collapses_long_equal_runs_with_2_lines_of_context():
    # 10 unchanged lines between two single-line changes -- only 2 lines of
    # context should survive on each side of the run, with a `gap` row
    # standing in for the other 6.
    old_lines = ["del"] + [f"ctx{i}" for i in range(10)] + ["also-del"]
    new_lines = ["add"] + [f"ctx{i}" for i in range(10)] + ["also-add"]
    rows = split_diff_rows("\n".join(old_lines), "\n".join(new_lines))
    gaps = [r for r in rows if r["kind"] == "gap"]
    assert len(gaps) == 1
    assert gaps[0]["text"] == "… 6 unchanged lines"
    ctx_rows = [r for r in rows if r["kind"] == "row" and r["ctx"]]
    assert [r["left"]["text"] for r in ctx_rows] == ["ctx0", "ctx1", "ctx8", "ctx9"]


def test_split_diff_rows_short_equal_run_is_not_collapsed():
    # A run of only 3 unchanged lines between two changes is at or below
    # the 2+2 context budget -- nothing hidden, so no gap row.
    old_lines = ["del", "a", "b", "c", "also-del"]
    new_lines = ["add", "a", "b", "c", "also-add"]
    rows = split_diff_rows("\n".join(old_lines), "\n".join(new_lines))
    assert not [r for r in rows if r["kind"] == "gap"]
    ctx_rows = [r for r in rows if r["kind"] == "row" and r["ctx"]]
    assert [r["left"]["text"] for r in ctx_rows] == ["a", "b", "c"]


# --- Task 4: proposer chip, New-strategy badge, auto/status warnings ------


def new_strategy_yaml(*, authorization=None, status="draft"):
    lines = f'name: "New one"\nstatus: {status}\nversion: 1\n'
    if authorization:
        lines += f"authorization: {authorization}\n"
    lines += ('position: {ticker: AAPL, target_weight: 5%}\n'
             'rules:\n  - {id: r1, type: hard, condition: "price < 90", action: "sell all"}\n')
    return lines


def test_reflection_card_shows_reflection_chip(client):
    queue_revision(client)
    body = client.get("/reviews").text
    assert '<span class="chip">reflection</span>' in body


def test_chat_card_shows_chat_chip(client):
    client.app.state.holder.get().queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml=CURRENT_S1_YAML,
        new_yaml=VALID_REVISION_YAML, diff="d", rationale="user asked",
        source="chat")
    (client.app.state.holder.get().strategies.directory / "s1.yaml").write_text(CURRENT_S1_YAML)
    body = client.get("/reviews").text
    assert '<span class="chip">chat</span>' in body
    assert "Strategy draft from your chat for s1" in body


def test_new_strategy_card_shows_new_badge_and_empty_left_column(client):
    client.app.state.holder.get().queue.add_strategy_revision(
        strategy_id="brandnew", ticker="AAPL", old_yaml="",
        new_yaml=new_strategy_yaml(), diff="d", rationale="brand new idea",
        source="chat", is_new=True)
    body = client.get("/reviews").text
    assert '<span class="badge">New strategy</span>' in body
    assert "— (new strategy)" in body


def test_existing_strategy_revision_has_no_new_badge(client):
    queue_revision(client)
    body = client.get("/reviews").text
    assert "New strategy" not in body


def _queue_with_auth(client, *, base_auth=None, new_auth=None, is_new=False,
                     strategy_id="s1"):
    base = new_strategy_yaml(authorization=base_auth, status="active")
    new = new_strategy_yaml(authorization=new_auth, status="active")
    strategies_dir = client.app.state.holder.get().strategies.directory
    old_yaml = "" if is_new else base
    if not is_new:
        (strategies_dir / f"{strategy_id}.yaml").write_text(base)
    return client.app.state.holder.get().queue.add_strategy_revision(
        strategy_id=strategy_id, ticker="AAPL", old_yaml=old_yaml, new_yaml=new,
        diff="d", rationale="r", source="chat", is_new=is_new)


AUTO_WARNING = "sets authorization: auto"


def test_auto_warning_shown_when_base_not_auto_and_new_is_auto(client):
    _queue_with_auth(client, base_auth="confirm", new_auth="auto")
    body = client.get("/reviews").text
    assert AUTO_WARNING in body
    assert_english_only(body)


def test_auto_warning_absent_when_base_already_auto(client):
    _queue_with_auth(client, base_auth="auto", new_auth="auto")
    body = client.get("/reviews").text
    assert AUTO_WARNING not in body


def test_auto_warning_absent_when_new_is_not_auto(client):
    _queue_with_auth(client, base_auth="confirm", new_auth="confirm")
    body = client.get("/reviews").text
    assert AUTO_WARNING not in body


def test_auto_warning_shown_for_new_strategy_with_auto(client):
    _queue_with_auth(client, new_auth="auto", is_new=True, strategy_id="brandnew2")
    body = client.get("/reviews").text
    assert AUTO_WARNING in body


STATUS_WARNING = "takes the strategy out of active"


def _queue_with_status(client, *, base_status, new_status, strategy_id="s1"):
    base = new_strategy_yaml(status=base_status)
    new = new_strategy_yaml(status=new_status)
    strategies_dir = client.app.state.holder.get().strategies.directory
    (strategies_dir / f"{strategy_id}.yaml").write_text(base)
    return client.app.state.holder.get().queue.add_strategy_revision(
        strategy_id=strategy_id, ticker="AAPL", old_yaml=base, new_yaml=new,
        diff="d", rationale="r", source="chat")


def test_status_warning_shown_when_active_goes_to_draft(client):
    _queue_with_status(client, base_status="active", new_status="draft")
    body = client.get("/reviews").text
    assert STATUS_WARNING in body
    assert_english_only(body)


def test_status_warning_absent_when_status_unchanged(client):
    _queue_with_status(client, base_status="active", new_status="active")
    body = client.get("/reviews").text
    assert STATUS_WARNING not in body


def test_status_warning_absent_when_base_was_not_active(client):
    _queue_with_status(client, base_status="draft", new_status="draft")
    body = client.get("/reviews").text
    assert STATUS_WARNING not in body


def test_superseded_revision_renders_in_resolved_history_with_note(client):
    queue = client.app.state.holder.get().queue
    strategies_dir = client.app.state.holder.get().strategies.directory
    strategies_dir.joinpath("s1.yaml").write_text(CURRENT_S1_YAML)
    old_rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml=CURRENT_S1_YAML,
        new_yaml=VALID_REVISION_YAML, diff="d", rationale="first draft",
        source="chat")
    queue.supersede_pending_chat_revision("s1", note="replaced by a newer chat draft")
    body = client.get("/reviews").text
    assert f"#{old_rid}" in body
    assert "Superseded" in body
    assert "replaced by a newer chat draft" in body


def test_split_diff_rows_two_empty_texts_still_say_no_changes():
    # SequenceMatcher on two empty line-lists returns ZERO opcodes (not one
    # 'equal'), which used to slip past the no-change guard and render an
    # empty table body under the column headers.
    rows = split_diff_rows("", "")
    assert rows == [{"kind": "none", "text": "No changes"}]
