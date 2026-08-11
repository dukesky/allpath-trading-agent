from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import ValidationError

from allpath_trade.broker.base import OrderIntent
from allpath_trade.execution import ExecutionError, ExecutionResult, Executor


class ReviewError(Exception):
    pass


class RevisionValidationError(ReviewError):
    """Raised by the revision applier (allpath_trade/agent/reflection_tools.py
    -- see `apply_revision_factory`) when an approved strategy_revision row
    fails one of its pre-write gates: an invalid strategy id, a
    since-deleted strategy file, the file's CURRENT text no longer matching
    the proposal's recorded base (spec §④: a second proposal drafted from
    the same base, approved after a sibling proposal already changed the
    file -- "校验不过则批准动作报错并保持 pending(用户可拒绝)"), or the
    proposed yaml itself failing strategy validation. The applier MUST
    raise this (and only this) before it writes anything to disk; see the
    loud comment in `_approve_revision` for why that ordering matters."""


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)}")


class ReviewQueue:
    """Service API for pending trigger reviews. The CLI today, the Web UI
    (Phase 5) and the agent (Phase 3) all operate this same interface."""

    def __init__(self, conn: sqlite3.Connection, executor: Executor | None) -> None:
        # executor may be None for read-only usage (list/reject);
        # approve() requires one for order-kind rows.
        self._conn = conn
        self._executor = executor
        # Phase 6: injected the same way as `executor` -- construction-time
        # for the common case, but strategy-revision approval is wired up
        # by Task 3's reflection machinery after the queue already exists
        # (mirrors how `_executor` itself is threaded through app.py), so a
        # setter is needed too. None until set: revision approvals fail
        # loudly (see `approve`) rather than silently no-op-ing.
        self._revision_applier: Callable[[str, str, str], None] | None = None

    def set_revision_applier(self, fn: Callable[[str, str, str], None]) -> None:
        """Wire up the function that actually applies an approved strategy
        revision: `fn(strategy_id, new_yaml, expected_base_yaml)` --
        `expected_base_yaml` is the file text the proposal was drafted
        against (snapshot["old_yaml"]), which the applier compares to the
        file's current-on-disk text to catch staleness (see
        `apply_revision_factory`). Task 3 calls this once at startup with
        the real strategy-file writer."""
        self._revision_applier = fn

    def add(self, *, strategy_id: str, rule_id: str, ticker: str, rule_type: str,
            condition: str, action: str, snapshot: dict,
            intent: OrderIntent | None, source: str = "sentinel",
            conversation_id: int | None = None,
            risk_preview: str | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO pending_reviews (ts, strategy_id, rule_id, ticker,"
            " rule_type, condition, action, snapshot, intent, source,"
            " conversation_id, risk_preview)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(UTC).isoformat(), strategy_id, rule_id, ticker,
             rule_type, condition, action,
             json.dumps(snapshot, default=_json_default),
             intent.model_dump_json() if intent else None, source,
             conversation_id, risk_preview))
        self._conn.commit()
        return cur.lastrowid

    def add_strategy_revision(self, *, strategy_id: str, ticker: str, old_yaml: str,
                              new_yaml: str, diff: str, rationale: str,
                              conversation_id: int | None = None) -> int:
        """Queue a reflection-proposed strategy revision for human approval.
        Shares `pending_reviews` with order reviews (a unified inbox, see
        Phase 6 design §④) rather than a separate table -- so it has to
        fill the legacy order-shaped NOT NULL columns honestly rather than
        leaving them blank: `rule_id`/`rule_type`/`action` are fixed
        sentinel values identifying "this is a revision, not a rule
        trigger", `condition` carries a truncated rationale (there's no
        other free-text summary column to put it in), and `intent` is None
        since there is no order to execute."""
        cur = self._conn.execute(
            "INSERT INTO pending_reviews (ts, strategy_id, rule_id, ticker,"
            " rule_type, condition, action, snapshot, intent, source,"
            " conversation_id, kind)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(UTC).isoformat(), strategy_id, "reflection", ticker,
             "revision", rationale[:200], "revise strategy",
             json.dumps({"old_yaml": old_yaml, "new_yaml": new_yaml,
                        "diff": diff, "rationale": rationale}),
             None, "reflection", conversation_id, "strategy_revision"))
        self._conn.commit()
        return cur.lastrowid

    def list(self, status: str | None = "pending") -> list[sqlite3.Row]:
        if status is None:
            return list(self._conn.execute(
                "SELECT * FROM pending_reviews ORDER BY id DESC"))
        return list(self._conn.execute(
            "SELECT * FROM pending_reviews WHERE status = ? ORDER BY id DESC",
            (status,)))

    def get(self, review_id: int) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM pending_reviews WHERE id = ?", (review_id,)).fetchone()
        if row is None:
            raise ReviewError(f"review {review_id} not found")
        return row

    def approve(self, review_id: int) -> ExecutionResult | None:
        # kind decides which of two claim-then-act paths runs; route reads
        # kind first (Phase 6) rather than duplicating the branch into every
        # caller (routes/reviews.py, sentinel.py, the review-agent tool).
        # Explicit allow-list (not an if/else fallthrough): an unrecognized
        # kind must fail closed rather than silently being treated as an
        # order and routed through the executor.
        row = self.get(review_id)
        if row["kind"] == "strategy_revision":
            return self._approve_revision(review_id)
        if row["kind"] == "order":
            return self._approve_order(review_id)
        raise ReviewError(f"unknown review kind: {row['kind']!r}")

    def _approve_order(self, review_id: int) -> ExecutionResult:
        if self._executor is None:
            raise ReviewError("approve requires broker credentials (no executor)")
        # Fetch row first to check existence and intent before claiming
        row = self.get(review_id)
        if row["status"] != "pending":
            raise ReviewError(f"review {review_id} is {row['status']}, not pending")
        if not row["intent"]:
            raise ReviewError(f"review {review_id} has no executable intent")

        # Parse the intent BEFORE claiming: a corrupt intent must leave the
        # review pending (not stuck "approved" with nothing executed).
        try:
            intent = OrderIntent.model_validate_json(row["intent"])
        except (ValidationError, ValueError) as exc:
            raise ReviewError(
                f"review {review_id} has corrupt intent: {exc}") from exc

        # Atomically claim the review to prevent concurrent execution
        resolved_ts = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            "UPDATE pending_reviews SET status=?, resolved_ts=? "
            "WHERE id=? AND status=?",
            ("approved", resolved_ts, review_id, "pending"))
        self._conn.commit()
        if cur.rowcount == 0:
            # Someone else already claimed this review
            row = self.get(review_id)
            raise ReviewError(f"review {review_id} is {row['status']}, not pending")

        # Now execute and set execution_result
        try:
            result = self._executor.execute(intent)
        except ExecutionError as exc:
            self._conn.execute(
                "UPDATE pending_reviews SET execution_result=? WHERE id=?",
                (json.dumps({"error": str(exc)}), review_id))
            self._conn.commit()
            raise
        self._conn.execute(
            "UPDATE pending_reviews SET execution_result=? WHERE id=?",
            (result.model_dump_json(), review_id))
        self._conn.commit()
        return result

    def _approve_revision(self, review_id: int) -> None:
        # Mirrors `_approve_order`'s shape: reject up front (before touching
        # the row) if approval can't possibly succeed, fetch + status-check,
        # atomically claim, then act and record the outcome on the claimed
        # row.
        if self._revision_applier is None:
            raise ReviewError(
                "approve requires a revision applier (none configured)")
        row = self.get(review_id)
        if row["status"] != "pending":
            raise ReviewError(f"review {review_id} is {row['status']}, not pending")

        # Parse the snapshot BEFORE claiming: mirrors `_approve_order`'s
        # pre-claim intent parse (see the comment there). A malformed
        # snapshot must leave the review pending -- not stuck "approved"
        # with a NULL execution_result and no audit trail of what went
        # wrong. `old_yaml` is the base this proposal was drafted against --
        # threaded through to the applier so it can compare it to the
        # file's CURRENT text (Finding 1: the applier's own re-validation
        # is a pure function of already-validated args and can never
        # observe a file that changed underneath a stale sibling proposal).
        try:
            snapshot = json.loads(row["snapshot"])
            new_yaml = snapshot["new_yaml"]
            old_yaml = snapshot["old_yaml"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ReviewError(
                f"review {review_id} has corrupt snapshot: {exc}") from exc

        # Atomically claim the review, same pattern as the order path.
        resolved_ts = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            "UPDATE pending_reviews SET status=?, resolved_ts=? "
            "WHERE id=? AND status=?",
            ("approved", resolved_ts, review_id, "pending"))
        self._conn.commit()
        if cur.rowcount == 0:
            row = self.get(review_id)
            raise ReviewError(f"review {review_id} is {row['status']}, not pending")

        try:
            self._revision_applier(row["strategy_id"], new_yaml, old_yaml)
        except RevisionValidationError:
            # Spec §④: a same-strategy proposal approved after an earlier
            # one already changed the file must fail revalidation without
            # getting stuck "approved" -- the row goes back to "pending" so
            # the user can still reject it.
            #
            # LOUD INVARIANT (Task 3's applier depends on this): validation
            # ALWAYS runs before any file write. RevisionValidationError
            # therefore only ever fires pre-write, so rolling this claim
            # back to "pending" is safe -- there is nothing on disk to
            # undo. If the applier is ever changed to validate *after*
            # writing, this rollback becomes unsafe and must be revisited.
            self._conn.execute(
                "UPDATE pending_reviews SET status=?, resolved_ts=? WHERE id=?",
                ("pending", None, review_id))
            self._conn.commit()
            raise
        except Exception as exc:
            # Any other applier failure (e.g. a disk write / os.replace
            # failing AFTER validation already passed) is NOT safely
            # retryable -- the file may already be half-written -- so
            # unlike RevisionValidationError above, the claim stays
            # "approved" (already committed) with the failure recorded for
            # an auditable trail, mirroring the order path's
            # ExecutionError handling.
            self._conn.execute(
                "UPDATE pending_reviews SET execution_result=? WHERE id=?",
                (json.dumps({"error": str(exc)}), review_id))
            self._conn.commit()
            raise
        # Success: a small non-null marker, not the order path's full
        # ExecutionResult JSON (there's no execution result for a file
        # write) -- keeps `execution_result` a reliable "has this row been
        # acted on" signal rather than leaving it ambiguously NULL.
        self._conn.execute(
            "UPDATE pending_reviews SET execution_result=? WHERE id=?",
            (json.dumps({"applied": True}), review_id))
        self._conn.commit()

    def attach_analysis(self, review_id: int, analysis_json: str) -> None:
        self._conn.execute(
            "UPDATE pending_reviews SET agent_analysis = ? WHERE id = ?",
            (analysis_json, review_id))
        self._conn.commit()

    def reject(self, review_id: int, note: str = "") -> None:
        # Fetch row first to check existence
        row = self.get(review_id)
        if row["status"] != "pending":
            raise ReviewError(f"review {review_id} is {row['status']}, not pending")

        # Atomically claim the review to prevent concurrent execution
        resolved_ts = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            "UPDATE pending_reviews SET status=?, resolved_ts=?, resolution_note=? "
            "WHERE id=? AND status=?",
            ("rejected", resolved_ts, note, review_id, "pending"))
        self._conn.commit()
        if cur.rowcount == 0:
            # Someone else already claimed this review
            row = self.get(review_id)
            raise ReviewError(f"review {review_id} is {row['status']}, not pending")
