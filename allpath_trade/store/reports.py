from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


class ReportStore:
    """Get/add access to the `reports` table -- one row per ET trading day,
    produced by the Phase 6 reflection loop.

    A separate file per table is the established pattern here (TradeJournal
    in journal.py, AppState in app_state.py, ReviewQueue in reviews.py) --
    db.py owns schema/migrations, not data access.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, date: str, body: str, summary: str, conversation_id: int | None,
            model: str, tokens_used: int, status: str = "ok") -> int:
        cur = self._conn.execute(
            "INSERT INTO reports (date, body, summary, conversation_id, model,"
            " tokens_used, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (date, body, summary, conversation_id, model, tokens_used, status,
             datetime.now(UTC).isoformat()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get(self, date: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM reports WHERE date = ?", (date,)).fetchone()

    def list(self, limit: int = 90) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM reports ORDER BY date DESC LIMIT ?", (limit,)))

    def list_between(self, start: str, end: str) -> list[sqlite3.Row]:
        """Rows with `date` inclusively between `start` and `end`
        (`YYYY-MM-DD` strings), newest first -- callers (web/routes/reports.py's
        `?from=&to=` filter) pass already-validated dates, and `YYYY-MM-DD`
        sorts identically as a string or as a real date, so no date parsing
        is needed here."""
        return list(self._conn.execute(
            "SELECT * FROM reports WHERE date >= ? AND date <= ? ORDER BY date DESC",
            (start, end)))

    def exists(self, date: str) -> bool:
        return self.get(date) is not None
