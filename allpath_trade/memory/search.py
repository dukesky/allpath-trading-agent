from __future__ import annotations

import sqlite3

from allpath_trade.store.accounts import DEFAULT_ACCOUNT, is_valid_account


class SessionSearch:
    """FTS5 search over conversation turns and observations — history is
    searched on demand, never bulk-loaded into context.

    `account` scopes every query (shadow-dual-active T4 CRITICAL carry from
    T1's review): a shadow chat turn or observation must never surface in
    paper's search results (and vice versa) -- that would put the other
    account's data straight into an agent's context through a tool call."""

    def __init__(self, conn: sqlite3.Connection, account: str = DEFAULT_ACCOUNT) -> None:
        if not is_valid_account(account):
            raise ValueError(f"invalid account: {account!r}")
        self._conn = conn
        self.account = account

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
                " FROM search_index WHERE search_index MATCH ? AND account = ?"
                " ORDER BY rank LIMIT ?",
                (terms, self.account, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [{"kind": r["kind"], "ref_id": r["ref_id"],
                 "subject": r["subject"], "snippet": r["snip"]} for r in rows]
