from __future__ import annotations

import sqlite3


class AppState:
    """Get/set access to the `app_state` key-value table.

    A separate file per table is the established pattern here (TradeJournal
    in journal.py, ReviewQueue in reviews.py, ConversationStore in
    conversations.py) -- db.py owns schema/migrations, not data access.
    Following that keeps this store discoverable the same way as its
    siblings instead of being a special case hiding on the connection
    module.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))
        self._conn.commit()
