from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from allpath_trade.store.accounts import DEFAULT_ACCOUNT


class ConversationStore:
    def __init__(self, conn: sqlite3.Connection, account: str = DEFAULT_ACCOUNT) -> None:
        self._conn = conn
        self._account = account

    @property
    def account(self) -> str:
        # Read-only: shadow-dual-active T4 review Minor 8 needs a public
        # way for `Consolidator.__init__` to assert this store's account
        # matches its own, without reaching into the `_account` internal
        # (every write path here still goes through `self._account`
        # directly -- this property exists for that one external
        # structural check, not as a second name for internal use).
        return self._account

    def start(self, kind: str = "chat") -> int:
        cur = self._conn.execute(
            "INSERT INTO conversations (account, started_ts, kind) VALUES (?, ?, ?)",
            (self._account, datetime.now(UTC).isoformat(), kind))
        self._conn.commit()
        return cur.lastrowid

    def latest(self, kind: str = "chat") -> int | None:
        # Filtered by kind so the web chat's "resume the latest conversation"
        # call (chat_service.py) never resumes a reflection transcript
        # (Phase 6) -- reflection sessions get their own `kind="reflection"`
        # conversations, kept out of the user-facing chat's history. Filtered
        # by account (shadow-dual-active T1) so each account's ChatService
        # instance resumes only its own conversation history.
        row = self._conn.execute(
            "SELECT id FROM conversations WHERE kind = ? AND account = ?"
            " ORDER BY id DESC LIMIT 1",
            (kind, self._account)).fetchone()
        return row["id"] if row else None

    def append(self, conversation_id: int, message: dict) -> None:
        # `conversation_turns` has no `account` column of its own (only its
        # parent `conversations` row does) -- ownership is checked here,
        # inside the same transaction as the write, rather than trusting the
        # caller to only ever pass a conversation_id it got from this same
        # account's `start()`/`latest()`. Turn and its FTS index entry must
        # land as a unit; see LockedConnection.transaction for why.
        with self._conn.transaction() as conn:
            owner = conn.execute(
                "SELECT account FROM conversations WHERE id = ?",
                (conversation_id,)).fetchone()
            if owner is None or owner["account"] != self._account:
                raise ValueError(
                    f"conversation {conversation_id} does not belong to"
                    f" account {self._account!r}")
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
                # shadow-dual-active T4 CRITICAL carry (from T1's review):
                # tag this row with the OWNING conversation's account (this
                # store's own `self._account` -- `append` already refuses a
                # foreign conversation_id above), not left blank/default --
                # SessionSearch scopes every query by account, so an
                # untagged row would either vanish from search entirely or
                # (worse, pre-this-fix) default to 'paper' regardless of
                # which account the turn actually belongs to.
                conn.execute(
                    "INSERT INTO search_index"
                    " (kind, ref_id, subject, content, account)"
                    " VALUES ('turn', ?, ?, ?, ?)",
                    (str(conversation_id), message.get("role", ""), indexed,
                     self._account))

    def history(self, conversation_id: int, after_turn_id: int = 0) -> list[dict]:
        # Joined to `conversations` and filtered by account (shadow-dual-
        # active T1): `conversation_turns` carries no account column of its
        # own, so ownership is enforced through its parent row -- a
        # conversation_id belonging to the other account reads back empty
        # rather than leaking that account's turns.
        rows = self._conn.execute(
            "SELECT t.message AS message FROM conversation_turns t"
            " JOIN conversations c ON c.id = t.conversation_id"
            " WHERE t.conversation_id = ? AND t.id > ? AND c.account = ?"
            " ORDER BY t.id",
            (conversation_id, after_turn_id, self._account))
        return [json.loads(r["message"]) for r in rows]

    def history_with_ids(self, conversation_id: int,
                         after_turn_id: int = 0) -> list[tuple[int, dict]]:
        rows = self._conn.execute(
            "SELECT t.id AS id, t.message AS message FROM conversation_turns t"
            " JOIN conversations c ON c.id = t.conversation_id"
            " WHERE t.conversation_id = ? AND t.id > ? AND c.account = ?"
            " ORDER BY t.id",
            (conversation_id, after_turn_id, self._account))
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
        # Scoped to this instance's account (shadow-dual-active T1): each
        # account gets its own nightly consolidator pass (Task 4), reading
        # only its own day's turns.
        query = ("SELECT t.id AS id, c.kind AS kind, t.message AS message"
                 " FROM conversation_turns t"
                 " JOIN conversations c ON c.id = t.conversation_id"
                 " WHERE t.id > ? AND c.account = ? ORDER BY t.id")
        params: tuple = (after_turn_id, self._account)
        if limit is not None:
            query += " LIMIT ?"
            params = (after_turn_id, self._account, limit)
        rows = self._conn.execute(query, params)
        return [(r["id"], r["kind"], json.loads(r["message"])) for r in rows]

    def summary(self, conversation_id: int) -> tuple[str, int]:
        row = self._conn.execute(
            "SELECT summary, summarized_through FROM conversations"
            " WHERE id = ? AND account = ?",
            (conversation_id, self._account)).fetchone()
        if row is None:
            return "", 0
        return row["summary"], row["summarized_through"]

    def set_summary(self, conversation_id: int, text: str,
                    through_turn_id: int) -> None:
        self._conn.execute(
            "UPDATE conversations SET summary = ?, summarized_through = ?"
            " WHERE id = ? AND account = ?",
            (text, through_turn_id, conversation_id, self._account))
        self._conn.commit()
