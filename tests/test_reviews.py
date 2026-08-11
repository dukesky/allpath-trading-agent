import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from allpath_trade.broker.base import OrderIntent, OrderSide
from allpath_trade.store.db import connect
from allpath_trade.store.reviews import (
    ReviewError,
    ReviewHandle,
    ReviewQueue,
    RevisionValidationError,
)


class StubExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, intent):
        self.calls.append(intent)
        from allpath_trade.execution import ExecutionResult
        from allpath_trade.risk.gate import RiskDecision
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


def test_approve_with_corrupt_intent_raises_and_leaves_pending(queue):
    rid = add(queue)
    queue._conn.execute("UPDATE pending_reviews SET intent=? WHERE id=?",
                        ("not json", rid))
    queue._conn.commit()
    with pytest.raises(ReviewError):
        queue.approve(rid)
    row = queue.get(rid)
    assert row["status"] == "pending"
    assert queue._executor.calls == []


# --- Phase 6: strategy-revision kind ----------------------------------

def test_add_defaults_to_order_kind(queue):
    rid = add(queue)
    row = queue.get(rid)
    assert row["kind"] == "order"


def test_add_strategy_revision_row_shape(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="id: s1\n", new_yaml="id: s1\nx: 2\n",
        diff="- x: 1\n+ x: 2\n", rationale="price broke the stop-loss assumption")
    row = queue.get(rid)
    assert row["kind"] == "strategy_revision"
    assert row["status"] == "pending"
    assert row["strategy_id"] == "s1"
    assert row["ticker"] == "AAPL"
    assert row["rule_id"] == "reflection"
    assert row["rule_type"] == "revision"
    assert row["action"] == "revise strategy"
    assert row["condition"] == "price broke the stop-loss assumption"
    assert row["intent"] is None
    assert row["source"] == "reflection"
    assert row["conversation_id"] is None

    snapshot = json.loads(row["snapshot"])
    assert snapshot == {
        "old_yaml": "id: s1\n", "new_yaml": "id: s1\nx: 2\n",
        "diff": "- x: 1\n+ x: 2\n", "rationale": "price broke the stop-loss assumption"}


def test_add_strategy_revision_condition_is_truncated_rationale(queue):
    rationale = "x" * 500
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="a", new_yaml="b",
        diff="d", rationale=rationale)
    row = queue.get(rid)
    assert row["condition"] == rationale[:200]


def test_add_strategy_revision_records_conversation_id(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="a", new_yaml="b",
        diff="d", rationale="r", conversation_id=42)
    row = queue.get(rid)
    assert row["conversation_id"] == 42


def test_approve_revision_calls_applier_with_strategy_id_new_yaml_and_old_yaml(queue):
    calls = []
    queue.set_revision_applier(
        lambda strategy_id, new_yaml, old_yaml: calls.append((strategy_id, new_yaml, old_yaml)))
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r")

    result = queue.approve(rid)

    assert result is None
    # `old_yaml` (the proposal's recorded base) must be threaded through too
    # -- Finding 1: the applier needs it to detect staleness.
    assert calls == [("s1", "new", "old")]
    row = queue.get(rid)
    assert row["status"] == "approved" and row["resolved_ts"]
    # order-kind executor must never be touched by a revision approval
    assert queue._executor.calls == []


def test_approve_revision_without_applier_raises_and_leaves_pending(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r")
    with pytest.raises(ReviewError):
        queue.approve(rid)
    row = queue.get(rid)
    assert row["status"] == "pending"


def test_approve_revision_applier_exception_recorded_and_reraised(queue):
    def boom(strategy_id, new_yaml, old_yaml):
        raise ValueError("bad yaml")

    queue.set_revision_applier(boom)
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r")

    with pytest.raises(ValueError, match="bad yaml"):
        queue.approve(rid)

    row = queue.get(rid)
    # The claim is atomic and happens before the applier call (mirrors the
    # order path's ExecutionError handling): the row is left "approved"
    # with the failure recorded, not rolled back to pending.
    assert row["status"] == "approved"
    assert json.loads(row["execution_result"]) == {"error": "bad yaml"}


def test_approve_revision_twice_raises(queue):
    calls = []
    queue.set_revision_applier(lambda strategy_id, new_yaml, old_yaml: calls.append(1))
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r")
    queue.approve(rid)
    with pytest.raises(ReviewError):
        queue.approve(rid)
    # the second approve must be rejected by the atomic claim before ever
    # reaching the applier again
    assert calls == [1]


def test_reject_unchanged_for_revision_kind(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r")
    queue.reject(rid, note="not now")
    row = queue.get(rid)
    assert row["status"] == "rejected" and row["resolution_note"] == "not now"


def test_reject_unchanged_for_order_kind(queue):
    # Locks in that touching the revision branch didn't disturb the
    # pre-existing order-kind reject path.
    rid = add(queue)
    queue.reject(rid, note="not now")
    row = queue.get(rid)
    assert row["status"] == "rejected" and row["resolution_note"] == "not now"


def test_legacy_pending_reviews_row_defaults_kind_order_after_migration(tmp_path):
    # Simulate a pre-Phase-6 database: pending_reviews exists without a
    # `kind` column. CREATE TABLE IF NOT EXISTS won't touch it, so the
    # ALTER TABLE migration must add + backfill the column.
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE pending_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts TEXT NOT NULL, strategy_id TEXT NOT NULL, rule_id TEXT NOT NULL,"
        " ticker TEXT NOT NULL, rule_type TEXT NOT NULL, condition TEXT NOT NULL,"
        " action TEXT NOT NULL, snapshot TEXT NOT NULL, intent TEXT,"
        " status TEXT NOT NULL DEFAULT 'pending', resolved_ts TEXT,"
        " resolution_note TEXT, execution_result TEXT)")
    raw.execute(
        "INSERT INTO pending_reviews (ts, strategy_id, rule_id, ticker, rule_type,"
        " condition, action, snapshot) VALUES ('t', 's1', 'r1', 'AAPL', 'soft',"
        " 'c', 'a', '{}')")
    raw.commit()
    raw.close()

    conn = connect(path)
    row = conn.execute("SELECT kind FROM pending_reviews").fetchone()
    assert row["kind"] == "order"


def test_legacy_row_with_no_token_hash_has_no_link_ever(tmp_path):
    # Same pre-migration simulation as above, but the point here is Part A's
    # invariant: a row that predates the approve-token migration has NULL
    # approval_token_hash/token_expires_ts and must never validate a link,
    # since no plaintext token was ever issued for it to check against.
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE pending_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts TEXT NOT NULL, strategy_id TEXT NOT NULL, rule_id TEXT NOT NULL,"
        " ticker TEXT NOT NULL, rule_type TEXT NOT NULL, condition TEXT NOT NULL,"
        " action TEXT NOT NULL, snapshot TEXT NOT NULL, intent TEXT,"
        " status TEXT NOT NULL DEFAULT 'pending', resolved_ts TEXT,"
        " resolution_note TEXT, execution_result TEXT)")
    raw.execute(
        "INSERT INTO pending_reviews (ts, strategy_id, rule_id, ticker, rule_type,"
        " condition, action, snapshot) VALUES ('t', 's1', 'r1', 'AAPL', 'soft',"
        " 'c', 'a', '{}')")
    raw.commit()
    raw.close()

    conn = connect(path)
    queue = ReviewQueue(conn, executor=None)
    row = queue.get(1)
    assert row["approval_token_hash"] is None
    assert row["token_expires_ts"] is None
    assert queue.validate_token(1, "anything") is None


# --- Review findings fixes ---------------------------------------------

def test_approve_revision_validation_error_leaves_row_pending_and_rejectable(queue):
    # Spec §④: a same-strategy proposal approved after an earlier one
    # already changed the file must fail revalidation without getting
    # stuck "approved" -- the row must go back to "pending" so the user
    # can still reject it.
    def boom(strategy_id, new_yaml, old_yaml):
        raise RevisionValidationError("file changed underneath this proposal")

    queue.set_revision_applier(boom)
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r")

    with pytest.raises(RevisionValidationError):
        queue.approve(rid)

    row = queue.get(rid)
    assert row["status"] == "pending"
    assert row["resolved_ts"] is None

    # still rejectable -- the whole point of leaving it pending
    queue.reject(rid, note="stale, will re-propose")
    row = queue.get(rid)
    assert row["status"] == "rejected" and row["resolution_note"] == "stale, will re-propose"


def test_approve_revision_runtime_error_recorded_and_reraised(queue):
    # Non-validation applier failures (e.g. a failed disk write AFTER
    # validation already passed) are NOT safely retryable, so the existing
    # approved+error behavior is pinned here.
    def boom(strategy_id, new_yaml, old_yaml):
        raise RuntimeError("os.replace failed")

    queue.set_revision_applier(boom)
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r")

    with pytest.raises(RuntimeError, match="os.replace failed"):
        queue.approve(rid)

    row = queue.get(rid)
    assert row["status"] == "approved"
    assert json.loads(row["execution_result"]) == {"error": "os.replace failed"}


def test_approve_revision_with_corrupt_snapshot_raises_and_leaves_pending(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r")
    queue._conn.execute("UPDATE pending_reviews SET snapshot=? WHERE id=?",
                        ("not json", rid))
    queue._conn.commit()

    with pytest.raises(ReviewError):
        queue.approve(rid)

    row = queue.get(rid)
    assert row["status"] == "pending"
    assert row["execution_result"] is None


def test_approve_unknown_kind_raises(queue):
    rid = add(queue)
    queue._conn.execute("UPDATE pending_reviews SET kind=? WHERE id=?",
                        ("mystery", rid))
    queue._conn.commit()

    with pytest.raises(ReviewError, match="unknown review kind"):
        queue.approve(rid)

    row = queue.get(rid)
    assert row["status"] == "pending"
    assert queue._executor.calls == []


# --- Approve-by-link tokens ---------------------------------------------

def test_add_returns_a_review_handle_carrying_a_plaintext_token(queue):
    rid = add(queue)
    assert isinstance(rid, ReviewHandle)
    assert isinstance(rid, int)
    assert rid.token and len(rid.token) > 20
    row = queue.get(rid)
    assert row["id"] == rid  # still a plain int for every existing comparison
    # only the hash is ever persisted, never the plaintext
    assert row["approval_token_hash"] == hashlib.sha256(rid.token.encode()).hexdigest()
    assert row["approval_token_hash"] != rid.token


def test_add_strategy_revision_also_returns_a_token(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r")
    assert isinstance(rid, ReviewHandle)
    assert rid.token
    row = queue.get(rid)
    assert row["approval_token_hash"] == hashlib.sha256(rid.token.encode()).hexdigest()


def test_validate_token_accepts_the_right_token(queue):
    rid = add(queue)
    row = queue.validate_token(rid, rid.token)
    assert row is not None and row["id"] == rid


def test_validate_token_rejects_the_wrong_token(queue):
    rid = add(queue)
    assert queue.validate_token(rid, "not-the-real-token") is None


def test_validate_token_rejects_empty_token(queue):
    rid = add(queue)
    assert queue.validate_token(rid, "") is None


def test_validate_token_rejects_missing_review(queue):
    assert queue.validate_token(999, "whatever") is None


def test_validate_token_rejects_already_resolved_review(queue):
    rid = add(queue)
    queue.reject(rid)
    assert queue.validate_token(rid, rid.token) is None


def test_validate_token_rejects_expired_token(queue):
    rid = add(queue)
    queue._conn.execute(
        "UPDATE pending_reviews SET token_expires_ts=? WHERE id=?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), rid))
    queue._conn.commit()
    assert queue.validate_token(rid, rid.token) is None


def test_validate_token_rejects_a_legacy_row_with_no_hash(queue):
    rid = add(queue)
    queue._conn.execute(
        "UPDATE pending_reviews SET approval_token_hash=NULL WHERE id=?", (rid,))
    queue._conn.commit()
    assert queue.validate_token(rid, rid.token) is None


def test_consume_token_burns_it_and_a_second_use_fails(queue):
    rid = add(queue)
    row = queue.consume_token(rid, rid.token)
    assert row is not None and row["id"] == rid
    # single-use: the same token no longer validates
    assert queue.validate_token(rid, rid.token) is None
    assert queue.consume_token(rid, rid.token) is None


def test_consume_token_rejects_wrong_token_without_burning_the_real_one(queue):
    rid = add(queue)
    assert queue.consume_token(rid, "wrong") is None
    # the real token must still work -- a failed guess must not burn it
    assert queue.validate_token(rid, rid.token) is not None


def test_approve_order_does_not_call_revision_applier(queue):
    # Converse of the contamination test above `test_approve_revision_...`:
    # an order-kind row must never reach a configured revision applier.
    calls = []
    queue.set_revision_applier(lambda strategy_id, new_yaml, old_yaml: calls.append(1))
    rid = add(queue)

    result = queue.approve(rid)

    assert result.submitted
    assert calls == []


# -- M2: `token` defaulting to None on ReviewHandle.__new__ ------------------


def test_review_handle_token_defaults_to_none():
    # Makes the sentinel's own `getattr(review_id, "token", None)` comment
    # (allpath_trade/sentinel.py) literally true for a handle built with no
    # token arg at all, not just for a bare `int`.
    handle = ReviewHandle(7)
    assert handle == 7
    assert handle.token is None


def test_review_handle_survives_deepcopy(queue):
    # `int.__new__`-based subclasses round-trip through pickle/deepcopy by
    # calling `__new__` again -- without a default, `token` (a
    # keyword-less positional) being required broke that round-trip with a
    # bare TypeError. A required `copy.deepcopy` use (e.g. handing a review
    # handle to another thread/context) must not blow up on this detail.
    import copy

    rid = add(queue)
    cloned = copy.deepcopy(rid)
    assert cloned == rid
    assert isinstance(cloned, ReviewHandle)
