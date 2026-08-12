from __future__ import annotations

import sqlite3

SENTINEL_HEARTBEAT_KEY = "sentinel_last_pass"
# Written alongside SENTINEL_HEARTBEAT_KEY on every scheduler tick, market
# open or closed -- "true"/"false" -- so a reader of the heartbeat can tell
# a real sentinel evaluation from a tick where the daemon proved it was
# alive but skipped the actual check (market closed). Without this, the
# dashboard's "last check Nm ago" is indistinguishable from "last tick Nm
# ago, nothing evaluated since" -- misleading on a weekend or after close.
SENTINEL_MARKET_OPEN_KEY = "sentinel_last_pass_market_open"

# Telegram pairing/poll state -- runtime state discovered at pairing time and
# advanced on every poll, not user-editable configuration, so it lives here
# rather than on Settings (which is .env-backed and rewritten wholesale).
TELEGRAM_CHAT_ID_KEY = "telegram_chat_id"
# Long-poll cursor: the highest Telegram update_id already processed, so a
# restart resumes after it instead of re-delivering old updates.
TELEGRAM_OFFSET_KEY = "telegram_update_offset"


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
