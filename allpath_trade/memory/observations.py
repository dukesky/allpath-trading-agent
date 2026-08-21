from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from allpath_trade.store.accounts import DEFAULT_ACCOUNT, is_valid_account


class ObservationLog:
    """Raw, append-only event journal — the consolidator's input.

    `account` scopes every read and write (shadow-dual-active T4 CRITICAL
    carry from T1's review): without it, a shadow-account observation would
    land in `search_index` with no account tag and surface in paper's
    session_search results — straight into the paper agent's context. Every
    construction site (app.py's per-account bundle) passes its own account;
    `recent`/`window` only ever return this instance's own rows."""

    def __init__(self, conn: sqlite3.Connection, account: str = DEFAULT_ACCOUNT) -> None:
        if not is_valid_account(account):
            raise ValueError(f"invalid account: {account!r}")
        self._conn = conn
        self.account = account

    def add(self, source: str, text: str, subject: str | None = None) -> int:
        # The record and its FTS index entry must land as a unit; see
        # LockedConnection.transaction for why.
        with self._conn.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO observations (account, ts, source, subject, text)"
                " VALUES (?, ?, ?, ?, ?)",
                (self.account, datetime.now(UTC).isoformat(), source, subject, text))
            content = f"{subject}: {text}" if subject else text
            conn.execute(
                "INSERT INTO search_index (kind, ref_id, subject, content, account)"
                " VALUES ('observation', ?, ?, ?, ?)",
                (str(cur.lastrowid), source, content, self.account))
        return cur.lastrowid

    def recent(self, since_iso: str | None = None,
               limit: int = 200) -> list[sqlite3.Row]:
        if since_iso:
            return list(self._conn.execute(
                "SELECT * FROM observations WHERE ts > ? AND account = ?"
                " ORDER BY id LIMIT ?",
                (since_iso, self.account, limit)))
        return list(self._conn.execute(
            "SELECT * FROM observations WHERE account = ? ORDER BY id LIMIT ?",
            (self.account, limit)))

    def window(self, since_iso: str, until_iso: str,
               limit: int = 5000) -> list[sqlite3.Row]:
        """Rows in `(since_iso, until_iso]`, NEWEST-first. Unlike `recent`
        (oldest-first `ORDER BY id ASC`, used for the append-only "what's
        happened since last time" reads), a caller that windows a bounded
        span and applies a `limit` needs the newest rows to survive an
        overflow, not the oldest -- the daily reflection briefing is exactly
        this case: on a day with more than `limit` observations, an
        oldest-first fetch would silently drop the afternoon and close, the
        exact window the reflection exists to review."""
        return list(self._conn.execute(
            "SELECT * FROM observations WHERE ts > ? AND ts <= ? AND account = ?"
            " ORDER BY id DESC LIMIT ?",
            (since_iso, until_iso, self.account, limit)))

    def last_marker_ts(self, source: str) -> str | None:
        """The `ts` of this account's own most recent `source`-tagged row --
        the consolidator's watermark for its "events since last run" read
        (memory/consolidate.py's `_last_marker_ts`). Scoped by account like
        every other method here (shadow-dual-active T4 CRITICAL carry):
        without this filter, whichever account's consolidator ran most
        recently would advance the OTHER account's watermark too, since a
        bare `MAX(id)` over an unscoped table can't tell whose marker row is
        whose."""
        row = self._conn.execute(
            "SELECT ts FROM observations WHERE source = ? AND account = ?"
            " ORDER BY id DESC LIMIT 1", (source, self.account)).fetchone()
        return row["ts"] if row else None
