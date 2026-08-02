from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime


class ConversationStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def start(self) -> int:
        cur = self._conn.execute(
            "INSERT INTO conversations (started_ts) VALUES (?)",
            (datetime.now(UTC).isoformat(),))
        self._conn.commit()
        return cur.lastrowid

    def latest(self) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM conversations ORDER BY id DESC LIMIT 1").fetchone()
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
