from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import ValidationError

from allpath_trade.broker.base import OrderIntent
from allpath_trade.execution import ExecutionError, ExecutionResult, Executor


class ReviewError(Exception):
    pass


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)}")


class ReviewQueue:
    """Service API for pending trigger reviews. The CLI today, the Web UI
    (Phase 5) and the agent (Phase 3) all operate this same interface."""

    def __init__(self, conn: sqlite3.Connection, executor: Executor | None) -> None:
        # executor may be None for read-only usage (list/reject);
        # approve() requires one.
        self._conn = conn
        self._executor = executor

    def add(self, *, strategy_id: str, rule_id: str, ticker: str, rule_type: str,
            condition: str, action: str, snapshot: dict,
            intent: OrderIntent | None) -> int:
        cur = self._conn.execute(
            "INSERT INTO pending_reviews (ts, strategy_id, rule_id, ticker,"
            " rule_type, condition, action, snapshot, intent)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(UTC).isoformat(), strategy_id, rule_id, ticker,
             rule_type, condition, action,
             json.dumps(snapshot, default=_json_default),
             intent.model_dump_json() if intent else None))
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

    def approve(self, review_id: int) -> ExecutionResult:
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
