import threading
import time

import pytest
from fastapi.testclient import TestClient

from allpath_trade.agent.review import ReviewAnalysis
from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.config import Settings
from allpath_trade.store.reviews import ReviewError
from allpath_trade.web.app import create_app
from allpath_trade.web.routes import dashboard as dashboard_route
from allpath_trade.web.routes.reviews import split_diff_rows
from tests.helpers import CONFIGURED_KEYS, assert_english_only
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
                        memory_dir=tmp_path / "memory", web_token="secret",
                        **CONFIGURED_KEYS)
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


def test_approve_of_a_shadow_row_resolves_from_a_paper_cookie(client):
    # shadow-dual-active T5 (row-bound, CARRIED from T4's review): the
    # browser is viewing paper (the default, cookie-less account) but the
    # review id being POSTed to belongs to shadow -- the in-app approve
    # route must resolve it through SHADOW's own queue, not paper's.
    comp = client.app.state.holder.get()
    shadow_queue = comp.accounts["shadow"].queue
    rid = shadow_queue.add(
        strategy_id="s1", rule_id="r1", ticker="AAPL", rule_type="soft",
        condition="price < 100", action="sell all", snapshot={"price": "99"},
        intent=OrderIntent(ticker="AAPL", side=OrderSide.SELL, qty="1", reason="r1"))

    r = client.post(f"/reviews/{rid}/approve", follow_redirects=False)

    assert r.status_code in (200, 303)
    assert shadow_queue.get(rid)["status"] == "approved"
    # Paper's own queue never had this id at all.
    with pytest.raises(ReviewError):
        comp.queue.get(rid)


def test_reject_of_a_shadow_row_resolves_from_a_paper_cookie(client):
    comp = client.app.state.holder.get()
    shadow_queue = comp.accounts["shadow"].queue
    rid = shadow_queue.add(
        strategy_id="s1", rule_id="r1", ticker="AAPL", rule_type="soft",
        condition="price < 100", action="sell all", snapshot={"price": "99"},
        intent=OrderIntent(ticker="AAPL", side=OrderSide.SELL, qty="1", reason="r1"))

    client.post(f"/reviews/{rid}/reject", data={"note": "no thanks"})

    row = shadow_queue.get(rid)
    assert row["status"] == "rejected"
    assert row["resolution_note"] == "no thanks"


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


def test_left_column_label_reflects_the_current_file_not_the_stale_is_new_flag(client):
    # Minor 1: a proposal drafted as `is_new=True` whose file has since
    # appeared (a sibling proposal approved, or a manual write) must show
    # that real content under "Current" -- not bury it under "— (new
    # strategy)", which `is_new` alone (frozen at proposal time) would say.
    client.app.state.holder.get().queue.add_strategy_revision(
        strategy_id="brandnew", ticker="AAPL", old_yaml="",
        new_yaml=new_strategy_yaml(), diff="d", rationale="brand new idea",
        source="chat", is_new=True)
    strategies_dir = client.app.state.holder.get().strategies.directory
    strategies_dir.joinpath("brandnew.yaml").write_text(CURRENT_S1_YAML)
    body = client.get("/reviews").text
    assert "— (new strategy)" not in body
    assert "Current" in body
    # The real, now-existing content renders in the diff, not "nothing here".
    assert "target_weight: 15%" in body


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


def test_approve_button_confirms_when_proposal_flips_to_auto(client):
    # Minor 3: same interaction weight as the strategy page's Activate
    # button (strategy_detail.html) -- a confirm() dialog, not a bare
    # submit, since approving has the exact same consequence.
    rid = _queue_with_auth(client, base_auth="confirm", new_auth="auto")
    body = client.get("/reviews").text
    assert (f'action="/reviews/{rid}/approve" onsubmit="return confirm(' in body)


def test_approve_button_has_no_confirm_when_not_flipping_to_auto(client):
    rid = _queue_with_auth(client, base_auth="confirm", new_auth="confirm")
    body = client.get("/reviews").text
    assert f'action="/reviews/{rid}/approve"><button' in body


def test_source_chip_renders_actual_value_for_an_unrecognized_source(client):
    # Minor 6: must render the row's real `source`, not silently relabel
    # any non-'chat' value "reflection".
    client.app.state.holder.get().queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml=CURRENT_S1_YAML,
        new_yaml=VALID_REVISION_YAML, diff="d", rationale="r", source="webhook")
    (client.app.state.holder.get().strategies.directory / "s1.yaml").write_text(CURRENT_S1_YAML)
    body = client.get("/reviews").text
    assert '<span class="chip">webhook</span>' in body
    assert '<span class="chip">reflection</span>' not in body


DRAFT_HINT = "This strategy will be draft after approval"


def test_draft_status_hint_shown_for_new_strategy_landing_as_draft(client):
    client.app.state.holder.get().queue.add_strategy_revision(
        strategy_id="brandnew5", ticker="AAPL", old_yaml="",
        new_yaml=new_strategy_yaml(status="draft"), diff="d", rationale="idea",
        source="chat", is_new=True)
    body = client.get("/reviews").text
    assert DRAFT_HINT in body


def test_draft_status_hint_shown_for_existing_strategy_proposed_into_draft(client):
    _queue_with_status(client, base_status="active", new_status="draft")
    body = client.get("/reviews").text
    assert DRAFT_HINT in body


def test_draft_status_hint_absent_when_landing_active(client):
    queue_revision(client)  # both sides status: active (CURRENT_S1_YAML)
    body = client.get("/reviews").text
    assert DRAFT_HINT not in body


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


def test_revision_card_headline_says_revise_strategy_for_an_existing_strategy(client):
    rid = queue_revision(client)
    body = client.get("/reviews").text
    assert "<strong>revise strategy · AAPL</strong>" in body
    assert f'id="review-{rid}"' in body


def test_revision_card_headline_says_new_strategy_not_revise(client):
    # Minor 2: the row's `action` column is always the fixed "revise
    # strategy" sentinel (store/reviews.py's add_strategy_revision) -- a
    # brand-new-strategy proposal must not echo it verbatim as its
    # headline.
    client.app.state.holder.get().queue.add_strategy_revision(
        strategy_id="brandnew3", ticker="AAPL", old_yaml="",
        new_yaml=new_strategy_yaml(), diff="d", rationale="brand new idea",
        source="chat", is_new=True)
    body = client.get("/reviews").text
    assert "<strong>new strategy · AAPL</strong>" in body
    assert "<strong>revise strategy · AAPL</strong>" not in body


def test_revision_card_does_not_render_a_stray_review_reason_paragraph(client):
    # Minor 2: `item['action']` on a strategy_revision row is the fixed
    # "revise strategy" sentinel, not free text -- it must not render in
    # the .review-reason block the way an order-kind chat proposal's real
    # free-text reason does.
    queue_revision(client)
    body = client.get("/reviews").text
    assert 'class="review-reason"' not in body


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


# ---------------------------------------------------------------------------
# shadow-dual-active T6: shadow_edit review cards + real end-to-end approval
# against the shadow account's actual ShadowLedger (this `client` fixture's
# accounts["shadow"] is wired through the real build_components, applier
# included -- see app.py's `_build_account_components`).
# ---------------------------------------------------------------------------

def queue_shadow_edit(client, **over):
    from allpath_trade.agent.shadow_tools import cash_snapshot

    b = client.app.state.holder.get().accounts["shadow"]
    before = cash_snapshot(b.broker)
    kwargs = {"op": "set_cash", "ticker": "", "action": "Set cash",
              "args": {"amount": "777"}, "before": before,
              "after": {"cash": "777"}}
    kwargs.update(over)
    return b.queue.add_shadow_edit(**kwargs), b


def test_shadow_edit_card_renders_title_and_before_after(client):
    rid, _b = queue_shadow_edit(client)
    # The card only shows on the CURRENT account's view -- switch first.
    client.post("/account/switch", data={"account": "shadow"})
    body = client.get("/reviews").text
    assert f"#{rid}" in body
    assert "Set cash" in body
    assert "777" in body


def test_shadow_edit_card_is_english_only(client):
    _rid, _b = queue_shadow_edit(client)
    client.post("/account/switch", data={"account": "shadow"})
    assert_english_only(client.get("/reviews").text)


def test_approve_shadow_edit_applies_to_the_real_ledger(client):
    rid, b = queue_shadow_edit(client)

    r = client.post(f"/reviews/{rid}/approve", follow_redirects=False)

    assert r.status_code in (200, 303)
    assert b.broker.get_account().cash == 777
    row = b.queue.get(rid)
    assert row["status"] == "approved"
    assert "applied" in row["execution_result"]


def test_reject_shadow_edit_leaves_ledger_untouched(client):
    rid, b = queue_shadow_edit(client)
    before_cash = b.broker.get_account().cash

    client.post(f"/reviews/{rid}/reject", data={"note": "no"})

    assert b.broker.get_account().cash == before_cash
    assert b.queue.get(rid)["status"] == "rejected"


def test_approve_shadow_edit_stale_before_state_stays_pending(client):
    from decimal import Decimal

    rid, b = queue_shadow_edit(
        client, before={"cash": "999999"})  # never matched reality

    r = client.post(f"/reviews/{rid}/approve", follow_redirects=False)

    assert r.status_code in (200, 303)
    row = b.queue.get(rid)
    assert row["status"] == "pending"
    assert b.broker.get_account().cash != Decimal(777)


def test_approve_stale_shadow_edit_message_says_ledger_change_not_revision(client):
    # M3: apply_shadow_edit_factory raises the exact same
    # RevisionValidationError strategy_revision's applier does -- the
    # in-app approve route must not echo "Revision failed re-validation"
    # for a row that never was a revision.
    rid, _b = queue_shadow_edit(client, before={"cash": "999999"})

    r = client.post(f"/reviews/{rid}/approve", follow_redirects=True)

    assert "Ledger change failed re-validation" in r.text
    assert "Revision failed re-validation" not in r.text


def test_shadow_edit_card_does_not_duplicate_the_action_as_a_reason(client):
    # M1: shadow_edit's own `action` ("Set cash") is already the card's
    # headline (the <strong> block) -- the .review-reason echo further
    # down used to repeat it a second time.
    queue_shadow_edit(client, action="Set cash to 777")
    client.post("/account/switch", data={"account": "shadow"})
    body = client.get("/reviews").text
    assert body.count("Set cash to 777") == 1


def test_shadow_edit_card_does_not_show_a_from_chat_triggered_on_line(client):
    # M1: "From chat — triggered on <code>set_cash</code>" duplicates the
    # card's own "Shadow ledger edit from your chat." line while also
    # confusingly echoing the raw op name as if it were a rule condition.
    queue_shadow_edit(client)
    client.post("/account/switch", data={"account": "shadow"})
    body = client.get("/reviews").text
    assert "triggered on" not in body
    assert "Shadow ledger edit from your chat." in body


def test_import_card_lists_the_tickers_being_set(client):
    from allpath_trade.agent.shadow_tools import full_snapshot, preview_import

    b = client.app.state.holder.get().accounts["shadow"]
    before = full_snapshot(b.broker)
    rows = [{"ticker": "AAPL", "qty": "10", "avg_cost": "150"},
            {"ticker": "MSFT", "qty": "5", "avg_cost": "300"}]
    after = preview_import(before, rows, None)
    b.queue.add_shadow_edit(
        op="import", ticker="", action="Import 2 position(s) from CSV",
        args={"rows": rows, "cash": None}, before=before, after=after)

    client.post("/account/switch", data={"account": "shadow"})
    body = client.get("/reviews").text

    assert "AAPL" in body and "MSFT" in body


def test_import_card_lists_first_10_tickers_and_a_count_of_the_rest(client):
    from allpath_trade.agent.shadow_tools import full_snapshot, preview_import

    b = client.app.state.holder.get().accounts["shadow"]
    before = full_snapshot(b.broker)
    rows = [{"ticker": f"T{i}", "qty": "1", "avg_cost": "1"} for i in range(12)]
    after = preview_import(before, rows, None)
    b.queue.add_shadow_edit(
        op="import", ticker="", action="Import 12 position(s) from CSV",
        args={"rows": rows, "cash": None}, before=before, after=after)

    client.post("/account/switch", data={"account": "shadow"})
    body = client.get("/reviews").text

    assert "and 2 more" in body


def test_reset_card_does_not_list_tickers(client):
    # M7 is scoped to import specifically -- reset unconditionally wipes
    # everything, so a "tickers being set" list would be misleading (it
    # doesn't "set" anything, it removes it all).
    from decimal import Decimal

    from allpath_trade.agent.shadow_tools import full_snapshot

    b = client.app.state.holder.get().accounts["shadow"]
    b.broker.set_position("AAPL", Decimal(10), Decimal(100))
    before = full_snapshot(b.broker)
    rid = b.queue.add_shadow_edit(
        op="reset", ticker="", action="Reset ledger", args={},
        before=before, after={"positions": {}, "cash": "0"})

    client.post("/account/switch", data={"account": "shadow"})
    body = client.get("/reviews").text

    assert f"#{rid}" in body
    assert "Tickers" not in body


# ---------------------------------------------------------------------------
# C3: the chat receipt for an approved SHADOW order must not say "submitted"
# -- the shadow executor only wrote a ledger row, and this line is what the
# agent (and the user) reads back later as the record of what happened.
# ---------------------------------------------------------------------------

class _RecordingChat:
    def __init__(self) -> None:
        self.notes: list[str] = []

    def note_resolution(self, text: str, source: str = "web") -> None:
        self.notes.append(text)


def _queue_chat_order(queue):
    return queue.add(
        strategy_id="", rule_id="", ticker="AAPL", rule_type="soft",
        condition="chat", action="sell all", snapshot={"price": "99"},
        intent=OrderIntent(ticker="AAPL", side=OrderSide.SELL, qty="1",
                           reason="chat"),
        source="chat")


def _wire_chats(client):
    chats = {"paper": _RecordingChat(), "shadow": _RecordingChat()}
    client.app.state.chat_services = chats
    return chats


def _stub_executor(monkeypatch, bundle):
    # A submitted-and-clean ExecutionResult, so the echo under test is the
    # success line rather than a broker/quote failure -- and so the shadow
    # case never reaches the real (network-backed) quote lookup its own
    # ledger write would otherwise make.
    from allpath_trade.execution import ExecutionResult
    from allpath_trade.risk import RiskDecision
    monkeypatch.setattr(
        bundle.executor, "execute",
        lambda intent: ExecutionResult(submitted=True, order=None,
                                       decision=RiskDecision(approved=True)))


def test_shadow_order_approval_echoes_recorded_not_submitted(client, monkeypatch):
    comp = client.app.state.holder.get()
    shadow = comp.accounts["shadow"]
    _stub_executor(monkeypatch, shadow)
    rid = _queue_chat_order(shadow.queue)
    chats = _wire_chats(client)

    client.post(f"/reviews/{rid}/approve", follow_redirects=False)

    expected = (f"You resolved #{rid}. Result: order recorded in your shadow "
                f"ledger — place it in your brokerage now")
    assert chats["shadow"].notes == [expected]
    assert chats["paper"].notes == []


def test_paper_order_approval_still_echoes_order_submitted(client, monkeypatch):
    comp = client.app.state.holder.get()
    _stub_executor(monkeypatch, comp)
    rid = _queue_chat_order(comp.queue)
    chats = _wire_chats(client)

    client.post(f"/reviews/{rid}/approve", follow_redirects=False)

    assert chats["paper"].notes == [f"You resolved #{rid}. Result: order submitted"]


def test_resolved_shadow_order_card_says_recorded_not_submitted(client, monkeypatch):
    # C3: the resolved card is the durable record on the reviews page --
    # "Order submitted" there outlives the one-off approval message and is
    # the wording a user checking back a day later reads.
    comp = client.app.state.holder.get()
    shadow = comp.accounts["shadow"]
    _stub_executor(monkeypatch, shadow)
    rid = _queue_chat_order(shadow.queue)
    client.post(f"/reviews/{rid}/approve", follow_redirects=False)

    client.post("/account/switch", data={"account": "shadow"})
    body = client.get("/reviews").text

    assert "Order recorded (shadow)" in body
    assert "Order submitted" not in body


def test_resolved_paper_order_card_still_says_submitted(client, monkeypatch):
    comp = client.app.state.holder.get()
    _stub_executor(monkeypatch, comp)
    rid = _queue_chat_order(comp.queue)
    client.post(f"/reviews/{rid}/approve", follow_redirects=False)

    body = client.get("/reviews").text

    assert "Order submitted" in body
    assert "Order recorded (shadow)" not in body
