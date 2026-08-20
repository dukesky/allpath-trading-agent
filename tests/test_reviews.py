import hashlib
import json
import sqlite3
import threading
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
        "diff": "- x: 1\n+ x: 2\n", "rationale": "price broke the stop-loss assumption",
        "is_new": False}


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


def test_add_strategy_revision_defaults_source_to_reflection(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="a", new_yaml="b",
        diff="d", rationale="r")
    row = queue.get(rid)
    assert row["source"] == "reflection"


def test_add_strategy_revision_persists_chat_source(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="a", new_yaml="b",
        diff="d", rationale="r", source="chat")
    row = queue.get(rid)
    assert row["source"] == "chat"


def test_add_strategy_revision_records_is_new_flag(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s2", ticker="AAPL", old_yaml="", new_yaml="id: s2\n",
        diff="d", rationale="r", source="chat", is_new=True)
    row = queue.get(rid)
    snapshot = json.loads(row["snapshot"])
    assert snapshot["old_yaml"] == ""
    assert snapshot["is_new"] is True


def test_add_strategy_revision_defaults_is_new_to_false(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="a", new_yaml="b",
        diff="d", rationale="r")
    row = queue.get(rid)
    assert json.loads(row["snapshot"])["is_new"] is False


def test_add_strategy_revision_empty_old_yaml_with_is_new_false_is_a_repair_not_new(queue):
    # Important 2: a 0-byte (or otherwise unparseable) *existing* strategy
    # file also legitimately reads back as old_yaml=="" -- that's a repair
    # proposal, not a new-strategy one. is_new is the explicit signal now,
    # independent of whether old_yaml happens to be empty.
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="", new_yaml="id: s1\n",
        diff="d", rationale="r", is_new=False)
    row = queue.get(rid)
    snapshot = json.loads(row["snapshot"])
    assert snapshot["old_yaml"] == ""
    assert snapshot["is_new"] is False


def test_approve_revision_threads_is_new_to_applier(queue):
    calls = []
    queue.set_revision_applier(
        lambda strategy_id, new_yaml, old_yaml, source, is_new: calls.append(is_new))
    rid = queue.add_strategy_revision(
        strategy_id="s2", ticker="AAPL", old_yaml="", new_yaml="id: s2\n",
        diff="d", rationale="r", source="chat", is_new=True)

    queue.approve(rid)

    assert calls == [True]


def test_approve_revision_is_new_falls_back_to_old_sentinel_for_legacy_rows(queue):
    # A row inserted before the `is_new` flag existed on the snapshot has no
    # "is_new" key at all -- must still resolve using the retired
    # old_yaml=="" convention rather than raising on a missing key.
    calls = []
    queue.set_revision_applier(
        lambda strategy_id, new_yaml, old_yaml, source, is_new: calls.append(is_new))
    rid = queue.add_strategy_revision(
        strategy_id="s2", ticker="AAPL", old_yaml="", new_yaml="id: s2\n",
        diff="d", rationale="r", source="chat", is_new=True)
    # Simulate a pre-fix row: snapshot has old_yaml=="" but no is_new key.
    row = queue.get(rid)
    snapshot = json.loads(row["snapshot"])
    del snapshot["is_new"]
    queue._conn.execute("UPDATE pending_reviews SET snapshot=? WHERE id=?",
                        (json.dumps(snapshot), rid))
    queue._conn.commit()

    queue.approve(rid)

    assert calls == [True]


def test_approve_revision_calls_applier_with_strategy_id_new_yaml_and_old_yaml(queue):
    calls = []
    queue.set_revision_applier(
        lambda strategy_id, new_yaml, old_yaml, source, is_new: calls.append(
            (strategy_id, new_yaml, old_yaml, source, is_new)))
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r")

    result = queue.approve(rid)

    assert result is None
    # `old_yaml` (the proposal's recorded base) must be threaded through too
    # -- Finding 1: the applier needs it to detect staleness. `source`
    # (defaulting to "reflection") is threaded through as the 4th arg so
    # the applier can branch its guards per proposer (Task 2). `is_new`
    # (defaulting to False -- this proposal wasn't recorded with is_new=True)
    # is the 5th arg (Important 2 fix): the applier's own base check, not
    # this store layer, decides what it means.
    assert calls == [("s1", "new", "old", "reflection", False)]
    row = queue.get(rid)
    assert row["status"] == "approved" and row["resolved_ts"]
    # order-kind executor must never be touched by a revision approval
    assert queue._executor.calls == []


def test_approve_revision_passes_chat_source_to_applier(queue):
    calls = []
    queue.set_revision_applier(
        lambda strategy_id, new_yaml, old_yaml, source, is_new: calls.append(source))
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r", source="chat")

    queue.approve(rid)

    assert calls == ["chat"]


def test_approve_revision_without_applier_raises_and_leaves_pending(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r")
    with pytest.raises(ReviewError):
        queue.approve(rid)
    row = queue.get(rid)
    assert row["status"] == "pending"


def test_approve_revision_applier_exception_recorded_and_reraised(queue):
    def boom(strategy_id, new_yaml, old_yaml, source, is_new):
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
    queue.set_revision_applier(
        lambda strategy_id, new_yaml, old_yaml, source, is_new: calls.append(1))
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


# --- Task 1: supersede_pending_chat_revision ----------------------------

def test_supersede_marks_pending_chat_row_superseded_with_note(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r", source="chat")

    superseded_id = queue.supersede_pending_chat_revision("s1", "replaced by #99")

    assert superseded_id == rid
    row = queue.get(rid)
    assert row["status"] == "superseded"
    assert row["resolution_note"] == "replaced by #99"
    assert row["resolved_ts"]


def test_supersede_returns_none_when_no_pending_chat_row(queue):
    assert queue.supersede_pending_chat_revision("s1", "note") is None


def test_supersede_ignores_reflection_rows_for_same_strategy(queue):
    reflection_id = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r", source="reflection")

    result = queue.supersede_pending_chat_revision("s1", "note")

    assert result is None
    row = queue.get(reflection_id)
    assert row["status"] == "pending"


def test_supersede_ignores_chat_rows_for_a_different_strategy_id(queue):
    other_id = queue.add_strategy_revision(
        strategy_id="s2", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r", source="chat")

    result = queue.supersede_pending_chat_revision("s1", "note")

    assert result is None
    row = queue.get(other_id)
    assert row["status"] == "pending"


def test_supersede_ignores_already_resolved_chat_rows(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r", source="chat")
    queue.reject(rid, note="no thanks")

    assert queue.supersede_pending_chat_revision("s1", "note") is None
    row = queue.get(rid)
    # untouched by supersede -- still shows the original rejection, not
    # overwritten with the supersede note
    assert row["status"] == "rejected" and row["resolution_note"] == "no thanks"


# --- Important 1: no SELECT-then-UPDATE race against a concurrent approve --

def test_supersede_leaves_an_already_approved_row_untouched(queue):
    # A row that resolved to "approved" (file written to disk, applier ran)
    # before supersede runs must never be relabeled "superseded" -- that
    # would make the audit trail lie about a live strategy change having
    # been silently discarded.
    apply_calls = []
    queue.set_revision_applier(
        lambda strategy_id, new_yaml, old_yaml, source, is_new:
            apply_calls.append(strategy_id))
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r", source="chat")
    queue.approve(rid)
    assert apply_calls == ["s1"]

    result = queue.supersede_pending_chat_revision("s1", "replaced by #99")

    assert result is None
    row = queue.get(rid)
    assert row["status"] == "approved"
    assert row["resolution_note"] is None


def test_supersede_issues_a_single_atomic_update_gated_on_pending_status(queue, monkeypatch):
    # Structural regression pin for the fix itself: the old implementation
    # first SELECTed the candidate pending ids, then UPDATEd them by id in a
    # second step -- leaving a window between the two statements where a
    # concurrent approve() on the same row could claim it before the UPDATE
    # ran, which would then blindly overwrite it back to "superseded"
    # anyway (WHERE id=? alone, no status re-check). The fix folds
    # selection and write into one UPDATE whose own WHERE clause re-checks
    # status='pending', so there is no separate SELECT for a concurrent
    # approve() to slip in behind. Pin that shape directly: exactly one
    # UPDATE statement is issued (no preceding SELECT), and it carries both
    # `status='pending'` in its WHERE clause and `status='superseded'` in
    # its SET clause together.
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r", source="chat")

    statements = []
    real_execute = queue._conn.execute

    def spy(sql, parameters=()):
        statements.append(sql)
        return real_execute(sql, parameters)

    monkeypatch.setattr(queue._conn, "execute", spy)

    result = queue.supersede_pending_chat_revision("s1", "replaced by #99")

    assert result == rid
    updates = [sql for sql in statements if sql.strip().upper().startswith("UPDATE")]
    selects = [sql for sql in statements if sql.strip().upper().startswith("SELECT")
               and "MAX(id)" not in sql]
    assert selects == []  # no SELECT ever gathers candidate ids up front
    assert len(updates) == 1
    assert "status='superseded'" in updates[0]
    assert "status='pending'" in updates[0]


def _race_supersede_against_approve(queue, strategy_id):
    """One trial of the approve()-vs-supersede() race for `strategy_id`.
    Returns (row, supersede_result, approve_errors, applied) so the caller
    can check the cross-outcome invariant. Defined at module level (not
    inside the loop below) so nothing here closes over a loop variable."""
    apply_calls = []
    queue.set_revision_applier(
        lambda strategy_id, new_yaml, old_yaml, source, is_new:
            apply_calls.append(strategy_id))
    rid = queue.add_strategy_revision(
        strategy_id=strategy_id, ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r", source="chat")

    barrier = threading.Barrier(2)
    approve_errors = []

    def do_approve():
        barrier.wait()
        try:
            queue.approve(rid)
        except ReviewError as exc:
            approve_errors.append(exc)

    thread = threading.Thread(target=do_approve)
    thread.start()
    barrier.wait()
    result = queue.supersede_pending_chat_revision(strategy_id, "replaced by #99")
    thread.join()

    return queue.get(rid), result, approve_errors, bool(apply_calls)


def test_supersede_and_concurrent_approve_are_mutually_exclusive(queue):
    # Behavioral version of the same regression, under real thread
    # scheduling rather than a hand-simulated interleaving: whichever of
    # approve()/supersede() actually wins the race for a given row, the
    # outcome must stay internally consistent -- a row the applier actually
    # wrote to disk must end up "approved", never "superseded" out from
    # under it, and vice versa. Looped to raise the odds of exercising both
    # orderings; every iteration's invariant holds regardless of which one
    # wins, so this is not a flaky/order-dependent assertion.
    for i in range(20):
        row, result, approve_errors, applied = _race_supersede_against_approve(
            queue, f"race-{i}")
        if applied:
            # approve() won: the file-write outcome must stick.
            assert row["status"] == "approved", i
            assert result is None, i
            assert approve_errors == [], i
        else:
            # supersede() won: approve() must have failed cleanly (the row
            # was no longer pending by the time its own atomic claim ran).
            assert row["status"] == "superseded", i
            assert result == row["id"], i
            assert len(approve_errors) == 1, i


def test_supersede_marks_only_pending_chat_rows_and_returns_most_recent_id(queue):
    first = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="v1",
        diff="d", rationale="r", source="chat")
    second = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="v2",
        diff="d", rationale="r", source="chat")

    result = queue.supersede_pending_chat_revision("s1", "replaced")

    assert result == second
    assert queue.get(first)["status"] == "superseded"
    assert queue.get(second)["status"] == "superseded"


def test_superseded_rows_excluded_from_pending_list_and_nav_count(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r", source="chat")
    queue.supersede_pending_chat_revision("s1", "note")

    assert queue.list("pending") == []
    assert [r["id"] for r in queue.list()] == []  # default status="pending"
    assert rid not in [r["id"] for r in queue.list("pending")]


def test_superseded_rows_appear_in_resolved_history_with_note(queue):
    rid = queue.add_strategy_revision(
        strategy_id="s1", ticker="AAPL", old_yaml="old", new_yaml="new",
        diff="d", rationale="r", source="chat")
    queue.supersede_pending_chat_revision("s1", "replaced by #99")

    all_rows = {r["id"]: r for r in queue.list(None)}
    resolved = [r for r in all_rows.values() if r["status"] != "pending"]
    assert len(resolved) == 1
    assert resolved[0]["id"] == rid
    assert resolved[0]["status"] == "superseded"
    assert resolved[0]["resolution_note"] == "replaced by #99"


# --- Review findings fixes ---------------------------------------------

def test_approve_revision_validation_error_leaves_row_pending_and_rejectable(queue):
    # Spec §④: a same-strategy proposal approved after an earlier one
    # already changed the file must fail revalidation without getting
    # stuck "approved" -- the row must go back to "pending" so the user
    # can still reject it.
    def boom(strategy_id, new_yaml, old_yaml, source, is_new):
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
    def boom(strategy_id, new_yaml, old_yaml, source, is_new):
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
    queue.set_revision_applier(
        lambda strategy_id, new_yaml, old_yaml, source, is_new: calls.append(1))
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


# --- shadow-dual-active T1: account scoping ---------------------------------

def test_two_account_interleave_isolated(tmp_path):
    conn = connect(tmp_path / "t.db")
    paper = ReviewQueue(conn, StubExecutor())
    shadow = ReviewQueue(conn, StubExecutor(), account="shadow")

    prid = add(paper)
    srid = add(shadow)

    [prow] = paper.list()
    assert prow["id"] == prid and prow["account"] == "paper"
    [srow] = shadow.list()
    assert srow["id"] == srid and srow["account"] == "shadow"

    # Cross-account get() must read back as "not found", not the other
    # account's row.
    with pytest.raises(ReviewError):
        paper.get(srid)
    with pytest.raises(ReviewError):
        shadow.get(prid)


def test_approve_does_not_cross_accounts(tmp_path):
    conn = connect(tmp_path / "t.db")
    paper = ReviewQueue(conn, StubExecutor())
    shadow = ReviewQueue(conn, StubExecutor(), account="shadow")
    srid = add(shadow)

    # paper's instance must not be able to approve shadow's row via its id.
    with pytest.raises(ReviewError):
        paper.approve(srid)
    assert shadow.get(srid)["status"] == "pending"

    shadow.approve(srid)
    assert shadow.get(srid)["status"] == "approved"


def test_supersede_pending_chat_revision_scoped_to_account(tmp_path):
    conn = connect(tmp_path / "t.db")
    paper = ReviewQueue(conn, None)
    shadow = ReviewQueue(conn, None, account="shadow")

    # Same strategy_id in both accounts (legitimate: Task 2 gives each
    # account its own strategy directory, so ids can collide).
    prid = paper.add_strategy_revision(strategy_id="s1", ticker="AAPL",
                                       old_yaml="old", new_yaml="new-p",
                                       diff="d", rationale="r", source="chat")
    srid = shadow.add_strategy_revision(strategy_id="s1", ticker="AAPL",
                                        old_yaml="old", new_yaml="new-s",
                                        diff="d", rationale="r", source="chat")

    # A second paper proposal for the same strategy_id must supersede only
    # the paper row, never shadow's.
    prid2 = paper.add_strategy_revision(strategy_id="s1", ticker="AAPL",
                                        old_yaml="old", new_yaml="new-p2",
                                        diff="d", rationale="r", source="chat")
    superseded = paper.supersede_pending_chat_revision(
        "s1", f"replaced by #{prid2}", exclude_id=prid2)
    assert superseded == prid
    assert paper.get(prid)["status"] == "superseded"
    assert shadow.get(srid)["status"] == "pending"


def test_legacy_pending_reviews_row_defaults_account_paper_after_migration(tmp_path):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE pending_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts TEXT NOT NULL, strategy_id TEXT NOT NULL, rule_id TEXT NOT NULL,"
        " ticker TEXT NOT NULL, rule_type TEXT NOT NULL, condition TEXT NOT NULL,"
        " action TEXT NOT NULL, snapshot TEXT NOT NULL, intent TEXT,"
        " status TEXT NOT NULL DEFAULT 'pending', resolved_ts TEXT,"
        " resolution_note TEXT, execution_result TEXT,"
        " kind TEXT NOT NULL DEFAULT 'order')")
    raw.execute(
        "INSERT INTO pending_reviews (ts, strategy_id, rule_id, ticker, rule_type,"
        " condition, action, snapshot) VALUES ('t', 's1', 'r1', 'AAPL', 'soft',"
        " 'c', 'a', '{}')")
    raw.commit()
    raw.close()

    conn = connect(path)
    row = conn.execute("SELECT account FROM pending_reviews").fetchone()
    assert row["account"] == "paper"

    paper = ReviewQueue(conn, None)
    [prow] = paper.list(status=None)
    assert prow["account"] == "paper"
