from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Self

from pydantic import ValidationError

from allpath_trade.broker.base import OrderIntent
from allpath_trade.execution import ExecutionError, ExecutionResult, Executor
from allpath_trade.store.accounts import DEFAULT_ACCOUNT

# Approve-by-link token lifetime (Part A): 24h from issue, per row.
TOKEN_TTL_SECONDS = 24 * 3600


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ReviewHandle(int):
    """The review id `add()`/`add_strategy_revision()` hand back, extended
    with the plaintext single-use approval-link token minted for this row
    (`.token`) -- the ONLY place that plaintext ever exists; only its
    sha256 hash is persisted (see `_hash_token`/`consume_token`).

    Subclasses `int` rather than becoming a dataclass/tuple so this is a
    drop-in replacement for the bare `int` review id every existing caller
    (sentinel.py, order_sink.py, the whole test suite) already treats the
    return value as -- equality, dict/row `id` comparisons, sqlite3
    parameter binding, f-string interpolation, all still just work. Code
    that wants the token opts in explicitly via `.token`; everything else
    is unaffected by this class existing at all."""

    def __new__(cls, review_id: int, token: str | None = None) -> Self:
        obj = super().__new__(cls, review_id)
        obj.token = token
        return obj


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

    def __init__(self, conn: sqlite3.Connection, executor: Executor | None,
                account: str = DEFAULT_ACCOUNT) -> None:
        # executor may be None for read-only usage (list/reject);
        # approve() requires one for order-kind rows.
        self._conn = conn
        self._executor = executor
        self._account = account
        # Phase 6: injected the same way as `executor` -- construction-time
        # for the common case, but strategy-revision approval is wired up
        # by Task 3's reflection machinery after the queue already exists
        # (mirrors how `_executor` itself is threaded through app.py), so a
        # setter is needed too. None until set: revision approvals fail
        # loudly (see `approve`) rather than silently no-op-ing.
        self._revision_applier: Callable[[str, str, str, str, bool], None] | None = None

    def set_revision_applier(self, fn: Callable[[str, str, str, str, bool], None]) -> None:
        """Wire up the function that actually applies an approved strategy
        revision: `fn(strategy_id, new_yaml, expected_base_yaml, source,
        is_new)` -- `expected_base_yaml` is the file text the proposal was
        drafted against (snapshot["old_yaml"]), which the applier compares
        to the file's current-on-disk text to catch staleness (see
        `apply_revision_factory`). `source` is the row's own `source`
        column ("reflection" or "chat", see `add_strategy_revision`) --
        threaded through so the applier can branch its guards by proposer
        (spec §①: reflection keeps its authorization/status freeze; a
        user-initiated chat proposal doesn't). `is_new` is the row's own
        recorded `is_new` flag (see `add_strategy_revision`) -- threaded
        through so the applier's base check knows whether "the file must
        not exist" (a brand-new strategy) or "the file must exist and
        match byte-exact" (a revision, including a repair of a 0-byte/
        unparseable existing file, where `expected_base_yaml` is also `""`
        but the file is very much supposed to already be there). Task 3
        calls this once at startup with the real strategy-file writer."""
        self._revision_applier = fn

    def _issue_token(self) -> tuple[str, str, str]:
        """Mint a fresh single-use approval-link token for a row about to
        be inserted. Returns (plaintext, hash, expires_iso) -- the caller
        persists only the hash and expiry; the plaintext is handed back to
        `add`/`add_strategy_revision`'s own caller (via `ReviewHandle`) and
        never stored anywhere."""
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(UTC) + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat()
        return token, _hash_token(token), expires

    def add(self, *, strategy_id: str, rule_id: str, ticker: str, rule_type: str,
            condition: str, action: str, snapshot: dict,
            intent: OrderIntent | None, source: str = "sentinel",
            conversation_id: int | None = None,
            risk_preview: str | None = None) -> ReviewHandle:
        token, token_hash, expires = self._issue_token()
        cur = self._conn.execute(
            "INSERT INTO pending_reviews (account, ts, strategy_id, rule_id, ticker,"
            " rule_type, condition, action, snapshot, intent, source,"
            " conversation_id, risk_preview, approval_token_hash, token_expires_ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self._account, datetime.now(UTC).isoformat(), strategy_id, rule_id, ticker,
             rule_type, condition, action,
             json.dumps(snapshot, default=_json_default),
             intent.model_dump_json() if intent else None, source,
             conversation_id, risk_preview, token_hash, expires))
        self._conn.commit()
        return ReviewHandle(cur.lastrowid, token)

    def add_strategy_revision(self, *, strategy_id: str, ticker: str, old_yaml: str,
                              new_yaml: str, diff: str, rationale: str,
                              conversation_id: int | None = None,
                              source: str = "reflection",
                              is_new: bool = False) -> ReviewHandle:
        """Queue a proposed strategy revision for human approval. Shares
        `pending_reviews` with order reviews (a unified inbox, see Phase 6
        design §④) rather than a separate table -- so it has to fill the
        legacy order-shaped NOT NULL columns honestly rather than leaving
        them blank: `rule_id`/`rule_type`/`action` are fixed sentinel
        values identifying "this is a revision, not a rule trigger",
        `condition` carries a truncated rationale (there's no other
        free-text summary column to put it in), and `intent` is None since
        there is no order to execute.

        `source` distinguishes the proposer: `"reflection"` (default -- the
        automated agent) or `"chat"` (the user drafting via web/Telegram
        chat, spec §①). Persisted on the `source` column that already
        exists on `pending_reviews`; `_approve_revision` threads it through
        to the revision applier so it can branch its guards accordingly.

        `is_new` (default `False`) is an explicit flag for "this proposes a
        brand-new strategy, not a revision of an existing one" (spec §②):
        the applier's base check becomes "the file must not already exist"
        rather than a byte-for-byte match against a non-empty base.
        Deliberately its own flag rather than the earlier `old_yaml == ""`
        sentinel convention: a 0-byte (or otherwise unparseable) *existing*
        strategy file also legitimately reads back as `old_yaml == ""` --
        that's a repair proposal against a real file the applier must still
        require to be present, not a new-strategy proposal. Collapsing both
        into one sentinel meant a repair proposal against an empty file
        could never apply (the applier demanded the file NOT exist, forever
        wrong). Persisted on the snapshot (there's no dedicated column,
        same as every other revision field) so `_approve_revision` can read
        it back and thread it to the applier alongside `old_yaml` itself."""
        token, token_hash, expires = self._issue_token()
        cur = self._conn.execute(
            "INSERT INTO pending_reviews (account, ts, strategy_id, rule_id, ticker,"
            " rule_type, condition, action, snapshot, intent, source,"
            " conversation_id, kind, approval_token_hash, token_expires_ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self._account, datetime.now(UTC).isoformat(), strategy_id, "reflection", ticker,
             "revision", rationale[:200], "revise strategy",
             json.dumps({"old_yaml": old_yaml, "new_yaml": new_yaml,
                        "diff": diff, "rationale": rationale, "is_new": is_new}),
             None, source, conversation_id, "strategy_revision",
             token_hash, expires))
        self._conn.commit()
        return ReviewHandle(cur.lastrowid, token)

    def supersede_pending_chat_revision(self, strategy_id: str, note: str, *,
                                        exclude_id: int | None = None) -> int | None:
        """Spec §③: a same-`strategy_id` chat proposal replaces (rather
        than piling up alongside) any earlier chat proposal that's still
        pending -- "改两轮很常见" (revising twice in one conversation is
        common). Marks every currently-pending `strategy_revision` row with
        `source='chat'` for this `strategy_id` as `status='superseded'`
        (a resolved status -- it sorts into "recent"/history the same way
        `rejected`/`approved` do, just with its own label), stamping
        `resolved_ts` and `resolution_note` (expected to name the
        replacing row, e.g. "replaced by #99") so the audit trail explains
        why it's gone from Pending without the user having acted on it.

        Deliberately scoped to `source='chat'`: a reflection proposal for
        the same strategy is a different proposer's independent judgment
        call (spec §③: "反思提案不受聊天提案影响、也不替换聊天提案") and
        is left untouched, as is any already-resolved chat row (only
        `status='pending'` rows are touched -- a rejected/approved/already-
        superseded row keeps its own resolution).

        Also clears the approval-link token (mirrors the M5 pattern in
        `_approve_order`/`reject`), so a stale approve-by-link email/
        Telegram notification for a superseded row can't be used even by
        accident -- belt-and-suspenders on top of `_token_ok` already
        gating on `status == "pending"`.

        Returns the id of the most recently created row that was
        superseded (there should ordinarily be at most one pending chat
        row per strategy, since each new one supersedes the last), or
        `None` if there was nothing pending to supersede.

        `exclude_id` (Important 2 fix, action_tools.py's `draft_strategy`):
        the caller now inserts the replacing row via `add_strategy_revision`
        BEFORE calling this (add-before-supersede, so a failed add never
        leaves a superseded orphan with nothing new in Pending -- see the
        comment at the call site). That just-inserted row is itself
        `status='pending'`, `source='chat'`, same `strategy_id` -- it would
        otherwise match this method's own WHERE clause and supersede
        itself. Pass its id here to exclude it from the UPDATE.

        Single UPDATE, `status='pending'` in the WHERE clause, no
        SELECT-then-UPDATE: an earlier version selected the candidate ids
        first and updated them by id in a second step, which left a window
        between the two where a concurrent `approve()` on one of those same
        ids could claim it (flip it to `approved`, run the applier, write
        the file) before this method's UPDATE ran -- which would then still
        relabel that row `superseded`, an audit trail that lies about a
        live strategy change having been silently discarded. Gating the
        UPDATE itself on `status='pending'` closes that window: rows an
        `approve()` claimed first simply no longer match and are left
        alone. The whole thing runs under `transaction()` so nothing else
        sharing this connection can interleave between the UPDATE and the
        follow-up SELECT that recovers the max id it actually touched."""
        # `strategy_id` is scoped per-account (Task 2: strategies/{account}/
        # directories), so the same id can legitimately exist in both
        # accounts -- the WHERE clauses below filter on `account` as well so
        # a chat proposal in one account never supersedes a same-named
        # strategy's pending proposal in the other.
        resolved_ts = datetime.now(UTC).isoformat()
        exclude_clause = " AND id != ?" if exclude_id is not None else ""
        params = ((resolved_ts, note, self._account, strategy_id)
                 + ((exclude_id,) if exclude_id is not None else ()))
        with self._conn.transaction():
            cur = self._conn.execute(
                "UPDATE pending_reviews SET status='superseded', resolved_ts=?,"
                " resolution_note=?, approval_token_hash=NULL, token_expires_ts=NULL"
                " WHERE kind='strategy_revision' AND source='chat'"
                " AND account=? AND strategy_id=? AND status='pending'" + exclude_clause,
                params)
            if cur.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT MAX(id) AS max_id FROM pending_reviews"
                " WHERE kind='strategy_revision' AND source='chat'"
                " AND account=? AND strategy_id=? AND status='superseded'"
                " AND resolved_ts=? AND resolution_note=?",
                (self._account, strategy_id, resolved_ts, note)).fetchone()
        return row["max_id"]

    def list(self, status: str | None = "pending") -> list[sqlite3.Row]:
        if status is None:
            return list(self._conn.execute(
                "SELECT * FROM pending_reviews WHERE account = ? ORDER BY id DESC",
                (self._account,)))
        return list(self._conn.execute(
            "SELECT * FROM pending_reviews WHERE status = ? AND account = ?"
            " ORDER BY id DESC",
            (status, self._account)))

    def get(self, review_id: int) -> sqlite3.Row:
        # Filtered by account: Task 1 keeps every store instance scoped to
        # its own account, so a foreign-account id reads back as "not
        # found" here -- same as it not existing at all. Row-bound approval
        # across the CURRENT view (spec: "批准永远作用于该行的账户") is
        # Task 5's job, via a lookup that isn't scoped to one instance.
        row = self._conn.execute(
            "SELECT * FROM pending_reviews WHERE id = ? AND account = ?",
            (review_id, self._account)).fetchone()
        if row is None:
            raise ReviewError(f"review {review_id} not found")
        return row

    def _token_ok(self, row: sqlite3.Row, token: str) -> bool:
        """No-oracle check: every failure mode (missing row handled by the
        caller, wrong status, no hash on the row -- legacy pre-migration
        rows, a wrong/garbled token, an expired one) returns the same
        `False` with no distinguishing signal. `hmac.compare_digest`
        against the hex digests (not the raw tokens) keeps the comparison
        constant-time without needing the attacker-controlled `token` to be
        a fixed length first."""
        if row["status"] != "pending":
            return False
        stored_hash = row["approval_token_hash"]
        if not stored_hash:
            return False
        if not hmac.compare_digest(stored_hash, _hash_token(token)):
            return False
        expires_raw = row["token_expires_ts"]
        if not expires_raw:
            return False
        try:
            expires = datetime.fromisoformat(expires_raw)
        except (TypeError, ValueError):
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return datetime.now(UTC) < expires

    def validate_token(self, review_id: int, token: str) -> sqlite3.Row | None:
        """Read-only check for the GET confirmation page: does this
        (review_id, token) pair currently resolve to a live, pending,
        unexpired review? Returns the row when it does, else None -- never
        raises, so a missing/garbled review_id is indistinguishable from a
        wrong token to the caller (see `_token_ok`'s docstring)."""
        if not token:
            return None
        try:
            row = self.get(review_id)
        except ReviewError:
            return None
        return row if self._token_ok(row, token) else None

    def consume_token(self, review_id: int, token: str) -> sqlite3.Row | None:
        """Validate-and-burn for the POST approve/reject routes: the token
        is atomically cleared (single-use) in the same statement that
        re-checks it is still the row's live token and the row is still
        pending, so two concurrent submits of the same link can't both
        pass. Returns the pre-consumption row on success, else None.

        Clearing happens here -- before `approve()`/`reject()` ever runs --
        specifically so a `RevisionValidationError` (which rolls the row's
        `status` back to "pending", see `_approve_revision`) can't leave the
        link usable again: by the time that rollback happens, the hash is
        already gone, so the link is dead regardless of what `status`
        recovers to."""
        row = self.validate_token(review_id, token)
        if row is None:
            return None
        cur = self._conn.execute(
            "UPDATE pending_reviews SET approval_token_hash=NULL"
            " WHERE id=? AND approval_token_hash=? AND status='pending' AND account=?",
            (review_id, row["approval_token_hash"], self._account))
        self._conn.commit()
        if cur.rowcount == 0:
            return None
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

        # Atomically claim the review to prevent concurrent execution.
        # M5: also clears the approval-link token here -- this is the
        # in-app approve path (web/routes/reviews.py), which never goes
        # through consume_token, so without this the row's link would stay
        # live (and usable) after an in-app approval already resolved it.
        # Two independent kill switches (status != pending, and no token)
        # instead of relying on status alone.
        resolved_ts = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            "UPDATE pending_reviews SET status=?, resolved_ts=?,"
            " approval_token_hash=NULL, token_expires_ts=NULL "
            "WHERE id=? AND status=? AND account=?",
            ("approved", resolved_ts, review_id, "pending", self._account))
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
                "UPDATE pending_reviews SET execution_result=? WHERE id=? AND account=?",
                (json.dumps({"error": str(exc)}), review_id, self._account))
            self._conn.commit()
            raise
        self._conn.execute(
            "UPDATE pending_reviews SET execution_result=? WHERE id=? AND account=?",
            (result.model_dump_json(), review_id, self._account))
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
        # `.get(..., old_yaml == "")` rather than a bare `["is_new"]`: rows
        # inserted before `is_new` existed on the snapshot (pre-fix chat/
        # reflection rows already sitting in `pending_reviews`) fall back to
        # the old sentinel convention so they keep resolving exactly as they
        # did before this fix, instead of raising on a missing key.
        is_new = snapshot.get("is_new", old_yaml == "")

        # Atomically claim the review, same pattern as the order path
        # (including the M5 token-clearing -- see the comment there).
        resolved_ts = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            "UPDATE pending_reviews SET status=?, resolved_ts=?,"
            " approval_token_hash=NULL, token_expires_ts=NULL "
            "WHERE id=? AND status=? AND account=?",
            ("approved", resolved_ts, review_id, "pending", self._account))
        self._conn.commit()
        if cur.rowcount == 0:
            row = self.get(review_id)
            raise ReviewError(f"review {review_id} is {row['status']}, not pending")

        try:
            self._revision_applier(
                row["strategy_id"], new_yaml, old_yaml, row["source"], is_new)
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
                "UPDATE pending_reviews SET status=?, resolved_ts=?"
                " WHERE id=? AND account=?",
                ("pending", None, review_id, self._account))
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
                "UPDATE pending_reviews SET execution_result=? WHERE id=? AND account=?",
                (json.dumps({"error": str(exc)}), review_id, self._account))
            self._conn.commit()
            raise
        # Success: a small non-null marker, not the order path's full
        # ExecutionResult JSON (there's no execution result for a file
        # write) -- keeps `execution_result` a reliable "has this row been
        # acted on" signal rather than leaving it ambiguously NULL.
        self._conn.execute(
            "UPDATE pending_reviews SET execution_result=? WHERE id=? AND account=?",
            (json.dumps({"applied": True}), review_id, self._account))
        self._conn.commit()

    def attach_analysis(self, review_id: int, analysis_json: str) -> None:
        self._conn.execute(
            "UPDATE pending_reviews SET agent_analysis = ? WHERE id = ? AND account = ?",
            (analysis_json, review_id, self._account))
        self._conn.commit()

    def reject(self, review_id: int, note: str = "") -> None:
        # Fetch row first to check existence
        row = self.get(review_id)
        if row["status"] != "pending":
            raise ReviewError(f"review {review_id} is {row['status']}, not pending")

        # Atomically claim the review to prevent concurrent execution (M5:
        # clears the approval-link token too -- see _approve_order's comment).
        resolved_ts = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            "UPDATE pending_reviews SET status=?, resolved_ts=?, resolution_note=?,"
            " approval_token_hash=NULL, token_expires_ts=NULL "
            "WHERE id=? AND status=? AND account=?",
            ("rejected", resolved_ts, note, review_id, "pending", self._account))
        self._conn.commit()
        if cur.rowcount == 0:
            # Someone else already claimed this review
            row = self.get(review_id)
            raise ReviewError(f"review {review_id} is {row['status']}, not pending")
