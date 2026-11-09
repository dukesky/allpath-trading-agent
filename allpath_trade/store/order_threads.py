"""Telegram order threads (allpath_trade/order_threads.py): the card ->
review mapping and the ledger of human messages on a thread."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from allpath_trade.store.accounts import is_valid_account


def find_card(conn: sqlite3.Connection, chat_id: str, message_id: int) -> dict | None:
    """The card a Telegram message id in `chat_id` belongs to, any account
    (the poller learns the account from the card), or None."""
    row = conn.execute(
        "SELECT * FROM telegram_cards WHERE chat_id = ? AND telegram_message_id = ?",
        (str(chat_id), message_id)).fetchone()
    return dict(row) if row is not None else None


class OrderThreadStore:
    def __init__(self, conn: sqlite3.Connection, account: str) -> None:
        if not is_valid_account(account):
            raise ValueError(f"invalid account: {account!r}")
        self._conn = conn
        self._account = account

    def add_card(self, review_id: int, chat_id: str, message_id: int) -> None:
        """Remember that Telegram message `message_id` in `chat_id` is the
        card for `review_id`. Wired as the review queue's card recorder
        (ReviewQueue.set_telegram_card_recorder) when the flag is on."""
        self._conn.execute(
            "INSERT OR REPLACE INTO telegram_cards (account, review_id, chat_id,"
            " telegram_message_id, ts) VALUES (?, ?, ?, ?, ?)",
            (self._account, int(review_id), str(chat_id), int(message_id),
             datetime.now(UTC).isoformat()))
        self._conn.commit()

    def log_message(self, *, review_id: int, card_message_id: int,
                    telegram_message_id: int | None, chat_id: str, user_id: str,
                    text: str, forwarded: bool, now: datetime | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO order_thread_messages (account, ts, review_id,"
            " card_message_id, telegram_message_id, chat_id, user_id, text, forwarded)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self._account, (now or datetime.now(UTC)).isoformat(), int(review_id),
             int(card_message_id), telegram_message_id, str(chat_id), str(user_id),
             text, int(forwarded)))
        self._conn.commit()
        return int(cur.lastrowid)

    def messages(self, review_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM order_thread_messages WHERE account = ?"
        args: list = [self._account]
        if review_id is not None:
            sql += " AND review_id = ?"
            args.append(int(review_id))
        return [dict(r) for r in self._conn.execute(sql + " ORDER BY id", args)]
