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
        self._conn.execute(
            "INSERT INTO conversation_turns (conversation_id, ts, message)"
            " VALUES (?, ?, ?)",
            (conversation_id, datetime.now(UTC).isoformat(), json.dumps(message)))
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            self._conn.execute(
                "INSERT INTO search_index (kind, ref_id, subject, content)"
                " VALUES ('turn', ?, ?, ?)",
                (str(conversation_id), message.get("role", ""), content))
        self._conn.commit()

    def history(self, conversation_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT message FROM conversation_turns WHERE conversation_id = ?"
            " ORDER BY id", (conversation_id,))
        return [json.loads(r["message"]) for r in rows]
