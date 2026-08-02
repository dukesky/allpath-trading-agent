from __future__ import annotations

import sqlite3


class SessionSearch:
    """FTS5 search over conversation turns and observations — history is
    searched on demand, never bulk-loaded into context."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def query(self, text: str, limit: int = 8) -> list[dict]:
        # OR semantics: any term may match (FTS5 default for adjacent terms
        # is AND, which misses partially-matching rows); rank still sorts
        # multi-term hits first.
        terms = " OR ".join(
            f'"{t}"' for t in text.replace('"', " ").split() if t)
        if not terms:
            return []
        try:
            rows = self._conn.execute(
                "SELECT kind, ref_id, subject,"
                " snippet(search_index, 3, '[', ']', '…', 12) AS snip"
                " FROM search_index WHERE search_index MATCH ?"
                " ORDER BY rank LIMIT ?",
                (terms, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [{"kind": r["kind"], "ref_id": r["ref_id"],
                 "subject": r["subject"], "snippet": r["snip"]} for r in rows]
