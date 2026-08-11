from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime


class ConversationStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def start(self, kind: str = "chat") -> int:
        cur = self._conn.execute(
            "INSERT INTO conversations (started_ts, kind) VALUES (?, ?)",
            (datetime.now(UTC).isoformat(), kind))
        self._conn.commit()
        return cur.lastrowid

    def latest(self, kind: str = "chat") -> int | None:
        # Filtered by kind so the web chat's "resume the latest conversation"
        # call (chat_service.py) never resumes a reflection transcript
        # (Phase 6) -- reflection sessions get their own `kind="reflection"`
        # conversations, kept out of the user-facing chat's history.
        row = self._conn.execute(
            "SELECT id FROM conversations WHERE kind = ? ORDER BY id DESC LIMIT 1",
            (kind,)).fetchone()
        return row["id"] if row else None

    def append(self, conversation_id: int, message: dict) -> None:
        # The turn and its FTS index entry must land as a unit; see
        # LockedConnection.transaction for why.
        with self._conn.transaction() as conn:
            conn.execute(
                "INSERT INTO conversation_turns (conversation_id, ts, message)"
                " VALUES (?, ?, ?)",
                (conversation_id, datetime.now(UTC).isoformat(), json.dumps(message)))
            # `display`, when present, is the human-readable text (e.g. a
            # system_note's unfenced summary -- see ChatService.note_
            # resolution); `content` for that same message is the
            # fence_external-wrapped version sent to the model. Indexing
            # `content` would surface the FENCE_NOTICE wrapper boilerplate
            # in session search instead of "You resolved #12. Result: ...".
            indexed = message.get("display", message.get("content"))
            if isinstance(indexed, str) and indexed.strip():
                conn.execute(
                    "INSERT INTO search_index (kind, ref_id, subject, content)"
                    " VALUES ('turn', ?, ?, ?)",
                    (str(conversation_id), message.get("role", ""), indexed))

    def history(self, conversation_id: int, after_turn_id: int = 0) -> list[dict]:
        rows = self._conn.execute(
            "SELECT message FROM conversation_turns"
            " WHERE conversation_id = ? AND id > ? ORDER BY id",
            (conversation_id, after_turn_id))
        return [json.loads(r["message"]) for r in rows]

    def history_with_ids(self, conversation_id: int,
                         after_turn_id: int = 0) -> list[tuple[int, dict]]:
        rows = self._conn.execute(
            "SELECT id, message FROM conversation_turns"
            " WHERE conversation_id = ? AND id > ? ORDER BY id",
            (conversation_id, after_turn_id))
        return [(r["id"], json.loads(r["message"])) for r in rows]

    def turns_since(self, after_turn_id: int = 0,
                    limit: int | None = None) -> list[tuple[int, str, dict]]:
        """All turns across every conversation with id > after_turn_id,
        oldest first. Unlike `history`/`history_with_ids`, which are scoped
        to one conversation (a single chat session), this spans the whole
        table -- the daily consolidator aggregates a day's worth of web and
        terminal chats together, not one session at a time, so it needs a
        global watermark rather than a per-conversation one.

        `limit`, when given, adds a SQL-level `LIMIT` (still oldest-first,
        so it bounds the OLDEST unconsumed turns, never skips ahead to the
        newest). Without it, a first run after upgrade -- or any run after
        a long gap -- has `after_turn_id` far behind and this query would
        load and JSON-parse the ENTIRE turn history in one shot while
        holding the connection, stalling the live chat thread behind it,
        not just doing extra work. Defaults to `None` (unbounded) to keep
        existing direct callers/tests working; production consolidation
        always passes an explicit bound (see `TURN_FETCH_LIMIT` in
        `memory/consolidate.py`).

        Finding F3: each row now carries its owning conversation's `kind`
        (JOINed from `conversations` -- "chat" for web/terminal,
        "reflection" for the daily reflection pass, see
        `ConversationStore.start`) alongside the turn id and message,
        instead of just `(id, message)`. Without it, the consolidator's
        `_turn_lines` had no way to tell a reflection session's turns apart
        from the user's own conversation, and prefixed every one of them
        `[chat]` -- misattributing a reflection hypothesis to the user
        while also telling the summarizing LLM (CONSOLIDATE_PROMPT) it was
        reading actual user/assistant conversation."""
        query = ("SELECT t.id AS id, c.kind AS kind, t.message AS message"
                 " FROM conversation_turns t"
                 " JOIN conversations c ON c.id = t.conversation_id"
                 " WHERE t.id > ? ORDER BY t.id")
        params: tuple = (after_turn_id,)
        if limit is not None:
            query += " LIMIT ?"
            params = (after_turn_id, limit)
        rows = self._conn.execute(query, params)
        return [(r["id"], r["kind"], json.loads(r["message"])) for r in rows]

    def summary(self, conversation_id: int) -> tuple[str, int]:
        row = self._conn.execute(
            "SELECT summary, summarized_through FROM conversations WHERE id = ?",
            (conversation_id,)).fetchone()
        if row is None:
            return "", 0
        return row["summary"], row["summarized_through"]

    def set_summary(self, conversation_id: int, text: str,
                    through_turn_id: int) -> None:
        self._conn.execute(
            "UPDATE conversations SET summary = ?, summarized_through = ?"
            " WHERE id = ?", (text, through_turn_id, conversation_id))
        self._conn.commit()
