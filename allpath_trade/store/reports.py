from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from allpath_trade.store.accounts import DEFAULT_ACCOUNT, is_valid_account


class ReportStore:
    """Get/add access to the `reports` table -- one row per (account, ET
    trading day), produced by the Phase 6 reflection loop.

    A separate file per table is the established pattern here (TradeJournal
    in journal.py, AppState in app_state.py, ReviewQueue in reviews.py) --
    db.py owns schema/migrations, not data access.
    """

    def __init__(self, conn: sqlite3.Connection, account: str = DEFAULT_ACCOUNT) -> None:
        # shadow-dual-active T5 carry: see TradeJournal's identical gate.
        if not is_valid_account(account):
            raise ValueError(f"invalid account: {account!r}")
        self._conn = conn
        self._account = account

    def add(self, date: str, body: str, summary: str, conversation_id: int | None,
            model: str, tokens_used: int, status: str = "ok") -> int:
        """Write this (account, date)'s report, REPLACING any row already
        there (I9).

        The table's UNIQUE (account, date) constraint means one ET day has
        exactly one slot per account, and `Reflector._fail` occupies it with
        a `status="failed"` row whenever a night's LLM was down or its
        output was unparseable twice. A plain INSERT made that failure
        permanent for the day: the retry could not store its result
        anywhere, so it died on an IntegrityError instead. Upserting lets a
        later successful pass take the slot over -- the failed row was a
        record of an attempt, not of the day, and nothing downstream reads
        the superseded attempt (the Reports page shows the day's one row).

        Returns the row id, re-read via `get` rather than taken from
        `cur.lastrowid`: on the DO UPDATE path SQLite performs no insert, so
        `lastrowid` there reports whatever the connection last inserted, not
        this row. Re-reading also keeps the id STABLE across a replace,
        which is what a caller storing "report #N" wants."""
        self._conn.execute(
            "INSERT INTO reports (account, date, body, summary, conversation_id,"
            " model, tokens_used, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(account, date) DO UPDATE SET"
            " body = excluded.body, summary = excluded.summary,"
            " conversation_id = excluded.conversation_id, model = excluded.model,"
            " tokens_used = excluded.tokens_used, status = excluded.status,"
            " created_at = excluded.created_at",
            (self._account, date, body, summary, conversation_id, model, tokens_used,
             status, datetime.now(UTC).isoformat()),
        )
        self._conn.commit()
        row = self.get(date)
        return row["id"] if row else 0

    def get(self, date: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM reports WHERE date = ? AND account = ?",
            (date, self._account)).fetchone()

    def list(self, limit: int = 90) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM reports WHERE account = ? ORDER BY date DESC LIMIT ?",
            (self._account, limit)))

    def list_between(self, start: str, end: str) -> list[sqlite3.Row]:
        """Rows with `date` inclusively between `start` and `end`
        (`YYYY-MM-DD` strings), newest first -- callers (web/routes/reports.py's
        `?from=&to=` filter) pass already-validated dates, and `YYYY-MM-DD`
        sorts identically as a string or as a real date, so no date parsing
        is needed here."""
        return list(self._conn.execute(
            "SELECT * FROM reports WHERE date >= ? AND date <= ? AND account = ?"
            " ORDER BY date DESC",
            (start, end, self._account)))

    def exists(self, date: str) -> bool:
        return self.get(date) is not None

    def exists_ok(self, date: str) -> bool:
        """Is there a SUCCESSFUL report for this (account, date)?

        I9: `exists` answers "is there any row", which a `status="failed"`
        row satisfies -- so using it as the reflection idempotency guard
        (Reflector.run_daily) meant one failed night, e.g. a five-minute LLM
        outage at 16:05, locked the account out of reflection for the rest
        of the calendar day. This is the question that guard actually wants:
        a failed row is an attempt, not a result, and must not stop a retry.
        `add`'s UPSERT (see above) is what lets that retry land."""
        row = self.get(date)
        return row is not None and row["status"] == "ok"
