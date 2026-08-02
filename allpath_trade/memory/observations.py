from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


class ObservationLog:
    """Raw, append-only event journal — the consolidator's input."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, source: str, text: str, subject: str | None = None) -> int:
        # The record and its FTS index entry must land as a unit; see
        # LockedConnection.transaction for why.
        with self._conn.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO observations (ts, source, subject, text)"
                " VALUES (?, ?, ?, ?)",
                (datetime.now(UTC).isoformat(), source, subject, text))
            content = f"{subject}: {text}" if subject else text
            conn.execute(
                "INSERT INTO search_index (kind, ref_id, subject, content)"
                " VALUES ('observation', ?, ?, ?)",
                (str(cur.lastrowid), source, content))
        return cur.lastrowid

    def recent(self, since_iso: str | None = None,
               limit: int = 200) -> list[sqlite3.Row]:
        if since_iso:
            return list(self._conn.execute(
                "SELECT * FROM observations WHERE ts > ? ORDER BY id LIMIT ?",
                (since_iso, limit)))
        return list(self._conn.execute(
            "SELECT * FROM observations ORDER BY id LIMIT ?", (limit,)))
