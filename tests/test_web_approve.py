"""Approve-by-link surface (web/routes/approve.py) -- I2: this whole file
didn't exist before this round; not one test previously hit `/a/`."""

from __future__ import annotations

import difflib

import pytest
from fastapi.testclient import TestClient

from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.config import Settings
from allpath_trade.execution import ExecutionError, ExecutionResult
from allpath_trade.risk.gate import RiskDecision
from allpath_trade.web.app import create_app
from allpath_trade.web.routes import dashboard as dashboard_route
from tests.helpers import assert_english_only
from tests.test_sentinel import FakeBroker
from tests.test_web_dashboard import FakeDataSource

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


@pytest.fixture(autouse=True)
def _clear_quote_cache():
    # Same reasoning as test_web_reviews.py's fixture of the same name: the
    # cache is module-level (survives across requests/tests on purpose), so
    # it must be cleared between tests sharing a ticker (AAPL) or one
    # test's cached quote leaks into the next's assertions.
    dashboard_route._quote_cache.clear()
    yield
    dashboard_route._quote_cache.clear()


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    # No login -- this is the fixture the "works with no auth cookie" test
    # needs. Every other test below logs in via the `client` fixture, since
    # the point of the /a/ exemption is that it works *either* way, not
    # that a session breaks it.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        monkeypatch.setattr(c.app.state.holder.get(), "data", FakeDataSource())
        yield c


@pytest.fixture
def client(app_client):
    app_client.post("/login", data={"token": "secret"})
    return app_client


def queue_one(client, **over):
    q = client.app.state.holder.get().queue
    kwargs = {"strategy_id": "s1", "rule_id": "r1", "ticker": "AAPL",
              "rule_type": "soft", "condition": "price < 100",
              "action": "sell all", "snapshot": {"price": "99"},
              "intent": OrderIntent(ticker="AAPL", side=OrderSide.SELL,
                                    qty="1", reason="rule r1")}
    kwargs.update(over)
    return q.add(**kwargs)


def queue_revision(client, strategy_id="s1", old_yaml=CURRENT_S1_YAML,
                   new_yaml=VALID_REVISION_YAML, rationale="reflection rationale"):
    strategies_dir = client.app.state.holder.get().strategies.directory
    path = strategies_dir / f"{strategy_id}.yaml"
    if not path.exists():
        path.write_text(old_yaml)
    # A real unified diff (not a placeholder) -- I3's confirm page renders
    # the *stored* diff verbatim, unlike the in-app card's live regenerate,
    # so a test asserting on diff-add/diff-del content needs the real thing.
    diff_text = "\n".join(difflib.unified_diff(
        old_yaml.splitlines(), new_yaml.splitlines(),
        fromfile=f"{strategy_id} (current)", tofile=f"{strategy_id} (proposed)",
        lineterm=""))
    q = client.app.state.holder.get().queue
    return q.add_strategy_revision(
        strategy_id=strategy_id, ticker="AAPL", old_yaml=old_yaml, new_yaml=new_yaml,
        diff=diff_text, rationale=rationale)


def approve_url(rid) -> str:
    return f"/a/{rid}"


# --- the /a/ exemption from the session-cookie gate -------------------------

def test_get_valid_link_works_with_no_auth_cookie(app_client):
    rid = queue_one(app_client)
    r = app_client.get(approve_url(rid), params={"k": rid.token})
    assert r.status_code == 200
    assert "AAPL" in r.text
    assert "login" not in r.text.lower()


# --- uniform invalid page: no oracle for *why* a link doesn't work ---------

def test_invalid_page_is_byte_identical_across_every_failure_mode(client):
    rid = queue_one(client)
    good_token = rid.token

    # baseline: a review id that doesn't exist at all
    baseline = client.get(approve_url(999999), params={"k": "whatever"})
    assert baseline.status_code == 200

    # wrong token on a real, still-pending review
    wrong_token = client.get(approve_url(rid), params={"k": "not-the-real-token"})
    assert wrong_token.status_code == 200
    assert wrong_token.text == baseline.text

    # expired token
    rid_expired = queue_one(client)
    conn = client.app.state.holder.get().conn
    conn.execute("UPDATE pending_reviews SET token_expires_ts='2000-01-01T00:00:00+00:00'"
                " WHERE id=?", (rid_expired,))
    conn.commit()
    expired = client.get(approve_url(rid_expired), params={"k": rid_expired.token})
    assert expired.status_code == 200
    assert expired.text == baseline.text

    # already-used (burned) token
    rid_used = queue_one(client)
    client.post(f"{approve_url(rid_used)}/reject", data={"k": rid_used.token})
    used = client.get(approve_url(rid_used), params={"k": rid_used.token})
    assert used.status_code == 200
    assert used.text == baseline.text

    # resolved in-app -- the link's own review is no longer pending, even
    # though nothing about the token itself changed the request
    rid_resolved = queue_one(client)
    token_resolved = rid_resolved.token
    client.post(f"/reviews/{rid_resolved}/reject")
    resolved = client.get(approve_url(rid_resolved), params={"k": token_resolved})
    assert resolved.status_code == 200
    assert resolved.text == baseline.text

    # the original good link is untouched by any of the above
    still_good = client.get(approve_url(rid), params={"k": good_token})
    assert still_good.status_code == 200
    assert still_good.text != baseline.text


# --- M1: a non-numeric review id must not 422 its way onto a different page -

def test_non_numeric_review_id_hits_the_same_uniform_invalid_page(client):
    baseline = client.get(approve_url(999999), params={"k": "whatever"})

    get_bad = client.get("/a/abc", params={"k": "whatever"})
    assert get_bad.status_code == 200
    assert get_bad.text == baseline.text

    post_bad = client.post("/a/abc/approve", data={"k": "whatever"})
    assert post_bad.status_code == 200
    assert post_bad.text == baseline.text


# --- POST approve: executes through the queue, burns the token -------------

def test_post_approve_with_token_in_body_executes_and_burns_the_token(client, monkeypatch):
    executed = []

    def spy_execute(intent):
        executed.append(intent)
        return ExecutionResult(submitted=True, order=None,
                               decision=RiskDecision(approved=True))

    monkeypatch.setattr(client.app.state.holder.get().executor, "execute", spy_execute)
    rid = queue_one(client)
    token = rid.token

    r = client.post(f"{approve_url(rid)}/approve", data={"k": token})
    assert r.status_code == 200
    assert "Order submitted" in r.text
    assert len(executed) == 1
    assert executed[0].ticker == "AAPL"

    row = client.app.state.holder.get().queue.get(rid)
    assert row["status"] == "approved"

    # single-use: the same link no longer works
    second = client.post(f"{approve_url(rid)}/approve", data={"k": token})
    assert second.status_code == 200
    assert "no longer valid" in second.text.lower()
    assert len(executed) == 1  # not executed again


# --- token must come from the form body, not the query string --------------

def test_token_only_in_query_string_is_rejected_and_the_real_token_still_works(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get().executor, "execute",
                        lambda intent: ExecutionResult(
                            submitted=True, order=None,
                            decision=RiskDecision(approved=True)))
    rid = queue_one(client)
    token = rid.token

    # `k` on the query string, none in the POST body -- FastAPI's `Form("")`
    # param never looks at query params, so this must fail exactly like a
    # missing token would.
    r = client.post(f"{approve_url(rid)}/approve?k={token}")
    assert r.status_code == 200
    assert "no longer valid" in r.text.lower()

    row = client.app.state.holder.get().queue.get(rid)
    assert row["status"] == "pending"  # nothing was claimed

    # the real token, submitted correctly, still works afterward
    confirm = client.get(approve_url(rid), params={"k": token})
    assert confirm.status_code == 200
    assert "no longer valid" not in confirm.text.lower()

    ok = client.post(f"{approve_url(rid)}/approve", data={"k": token})
    assert "Order submitted" in ok.text
    assert client.app.state.holder.get().queue.get(rid)["status"] == "approved"


# --- a token is scoped to the review row it was minted for -----------------

def test_token_for_review_a_is_rejected_on_review_b(client):
    rid_a = queue_one(client)
    rid_b = queue_one(client)
    token_a = rid_a.token

    r = client.post(f"{approve_url(rid_b)}/approve", data={"k": token_a})
    assert r.status_code == 200
    assert "no longer valid" in r.text.lower()

    assert client.app.state.holder.get().queue.get(rid_b)["status"] == "pending"
    # rejected on the wrong row, but not burned -- it must still work on its
    # own review
    assert client.app.state.holder.get().queue.get(rid_a)["status"] == "pending"
    confirm_a = client.get(approve_url(rid_a), params={"k": token_a})
    assert confirm_a.status_code == 200
    assert "AAPL" in confirm_a.text


# --- reject burns the token too, same as approve ----------------------------

def test_reject_path_burns_the_token(client):
    rid = queue_one(client)
    token = rid.token

    r = client.post(f"{approve_url(rid)}/reject", data={"k": token})
    assert r.status_code == 200
    assert "Rejected" in r.text
    assert client.app.state.holder.get().queue.get(rid)["status"] == "rejected"

    again = client.get(approve_url(rid), params={"k": token})
    assert "no longer valid" in again.text.lower()


# --- a failed execution is reported honestly, and still burns the token ----

def test_failed_execution_shows_an_honest_result_and_still_burns_the_token(client, monkeypatch):
    def boom(intent):
        raise ExecutionError("broker unreachable")

    monkeypatch.setattr(client.app.state.holder.get().executor, "execute", boom)
    rid = queue_one(client)
    token = rid.token

    r = client.post(f"{approve_url(rid)}/approve", data={"k": token})
    assert r.status_code == 200
    assert "execution failed" in r.text.lower()
    assert "broker unreachable" in r.text

    # the review was claimed (status flips before the broker call, same as
    # the in-app path) -- the link must not offer a retry either way.
    assert client.app.state.holder.get().queue.get(rid)["status"] == "approved"
    again = client.get(approve_url(rid), params={"k": token})
    assert "no longer valid" in again.text.lower()


# --- I3: revision confirm page must not render the order-shaped price block
# and must show the actual diff/rationale instead ---------------------------

def test_order_confirm_page_shows_the_price_block(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get(), "data",
                        FakeDataSource(price="105.00", previous_close="100.00"))
    rid = queue_one(client)
    body = client.get(approve_url(rid), params={"k": rid.token}).text
    assert "review-price-context" in body
    assert "Rationale:" not in body


def test_revision_confirm_page_shows_diff_and_rationale_not_a_price_block(client):
    rid = queue_revision(client, rationale="guidance updated after earnings")
    body = client.get(approve_url(rid), params={"k": rid.token}).text

    assert "review-price-context" not in body
    assert "Market open" not in body
    assert "Market closed" not in body
    assert "Rationale:" in body
    assert "guidance updated after earnings" in body
    assert 'class="diff-add"' in body
    assert 'class="diff-del"' in body
    assert "target_weight: 10%" in body  # proposed
    assert "target_weight: 15%" in body  # current


# --- assert_english_only across all three /a/ templates --------------------

def test_confirm_page_is_english_only(client):
    rid = queue_one(client)
    assert_english_only(client.get(approve_url(rid), params={"k": rid.token}).text)


def test_revision_confirm_page_is_english_only(client):
    rid = queue_revision(client)
    assert_english_only(client.get(approve_url(rid), params={"k": rid.token}).text)


def test_invalid_page_is_english_only(client):
    assert_english_only(client.get(approve_url(999999), params={"k": "x"}).text)


def test_result_page_is_english_only(client):
    rid = queue_one(client)
    body = client.post(f"{approve_url(rid)}/reject", data={"k": rid.token}).text
    assert_english_only(body)


# --- cross-origin POST is expected to succeed: the token IS the auth -------

def test_cross_origin_post_with_a_valid_token_succeeds_by_design(client, monkeypatch):
    # web/auth.py's Origin check (defense against a LAN cookie-bearing
    # cross-origin post) never runs for /a/ at all -- the whole route
    # prefix is exempted from the session-cookie middleware entirely (see
    # web/auth.py's PUBLIC_PATHS/`/a/` comment). A tap-from-notification
    # link has no session and no Origin to compare against; the single-use
    # token is the only thing standing between a visitor and this action,
    # by design, not by omission.
    monkeypatch.setattr(client.app.state.holder.get().executor, "execute",
                        lambda intent: ExecutionResult(
                            submitted=True, order=None,
                            decision=RiskDecision(approved=True)))
    rid = queue_one(client)
    r = client.post(f"{approve_url(rid)}/approve", data={"k": rid.token},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
    assert "cross-origin" not in r.text.lower()
    assert client.app.state.holder.get().queue.get(rid)["status"] == "approved"


# --- M3: no-store / no-referrer headers on every /a/ response --------------

def test_security_headers_present_on_all_three_a_responses(client):
    rid = queue_one(client)
    confirm = client.get(approve_url(rid), params={"k": rid.token})
    assert confirm.headers["cache-control"] == "no-store"
    assert confirm.headers["referrer-policy"] == "no-referrer"

    invalid = client.get(approve_url(999999), params={"k": "x"})
    assert invalid.headers["cache-control"] == "no-store"
    assert invalid.headers["referrer-policy"] == "no-referrer"

    result = client.post(f"{approve_url(rid)}/reject", data={"k": rid.token})
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["referrer-policy"] == "no-referrer"


# --- M5: an in-app resolution kills the standalone link too ----------------

def test_in_app_approve_kills_the_approval_link(client, monkeypatch):
    monkeypatch.setattr(client.app.state.holder.get().executor, "execute",
                        lambda intent: ExecutionResult(
                            submitted=True, order=None,
                            decision=RiskDecision(approved=True)))
    rid = queue_one(client)
    token = rid.token

    client.post(f"/reviews/{rid}/approve")  # in-app, no token involved

    r = client.get(approve_url(rid), params={"k": token})
    assert r.status_code == 200
    assert "no longer valid" in r.text.lower()


def test_in_app_reject_kills_the_approval_link(client):
    rid = queue_one(client)
    token = rid.token

    client.post(f"/reviews/{rid}/reject")  # in-app

    r = client.get(approve_url(rid), params={"k": token})
    assert r.status_code == 200
    assert "no longer valid" in r.text.lower()


def test_in_app_revision_approve_kills_the_approval_link(client):
    rid = queue_revision(client)
    token = rid.token

    client.post(f"/reviews/{rid}/approve")  # in-app

    r = client.get(approve_url(rid), params={"k": token})
    assert r.status_code == 200
    assert "no longer valid" in r.text.lower()


# --- M6: an order row with no executable intent is an honest error, but
# must not burn the link ------------------------------------------------

def test_null_intent_order_row_does_not_burn_the_link(client):
    rid = queue_one(client)
    token = rid.token
    conn = client.app.state.holder.get().conn
    conn.execute("UPDATE pending_reviews SET intent=NULL WHERE id=?", (rid,))
    conn.commit()

    r = client.post(f"{approve_url(rid)}/approve", data={"k": token})
    assert r.status_code == 200
    assert "no executable order" in r.text.lower()
    # the honest-error copy says the link is still live, not spent
    assert "still active" in r.text.lower()

    # nothing was claimed
    row = client.app.state.holder.get().queue.get(rid)
    assert row["status"] == "pending"

    # the link itself was not burned -- it still resolves to the confirm page
    confirm = client.get(approve_url(rid), params={"k": token})
    assert confirm.status_code == 200
    assert "no longer valid" not in confirm.text.lower()


def test_null_intent_order_row_reject_is_unaffected(client):
    # The M6 guard only gates approve -- reject never touches intent and
    # must keep working normally even on a malformed row.
    rid = queue_one(client)
    token = rid.token
    conn = client.app.state.holder.get().conn
    conn.execute("UPDATE pending_reviews SET intent=NULL WHERE id=?", (rid,))
    conn.commit()

    r = client.post(f"{approve_url(rid)}/reject", data={"k": token})
    assert r.status_code == 200
    assert "Rejected" in r.text
    assert client.app.state.holder.get().queue.get(rid)["status"] == "rejected"
