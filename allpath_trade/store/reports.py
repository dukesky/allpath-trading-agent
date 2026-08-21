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
        cur = self._conn.execute(
            "INSERT INTO reports (account, date, body, summary, conversation_id,"
            " model, tokens_used, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self._account, date, body, summary, conversation_id, model, tokens_used,
             status, datetime.now(UTC).isoformat()),
        )
        self._conn.commit()
        return cur.lastrowid

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
