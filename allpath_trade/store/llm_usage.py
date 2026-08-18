from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta


class LLMUsage:
    """Records every LLM call's token usage and reads it back for the
    Settings -> Usage panel (see llm/prices.py for the cost-estimate math
    built on top of these numbers).

    Written from exactly ONE choke point -- the `_RecordingClient` wrapper
    `llm/factory.py`'s `build_llm` returns when a caller passes it a
    `usage_store` -- rather than at each of this app's many call sites
    (chat turns, sentinel's review-tier analysis, memory consolidation,
    after-close reflection), so a future new caller of `build_llm` is
    covered automatically instead of needing its own recording code."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, *, tier: str, model: str, input_tokens: int,
              output_tokens: int, purpose: str) -> None:
        # Never raises -- a broken usage table must not break the LLM call
        # that triggered it (see `_RecordingClient.complete`'s own
        # try/except, which is the only caller of this method in production
        # code and already treats this as best-effort).
        self._conn.execute(
            "INSERT INTO llm_usage (ts, tier, model, input_tokens, output_tokens, purpose)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(UTC).isoformat(), tier, model,
             int(input_tokens), int(output_tokens), purpose))
        self._conn.commit()

    def _since(self, days: int) -> str:
        return (datetime.now(UTC) - timedelta(days=days)).isoformat()

    def summary(self, days: int) -> list[sqlite3.Row]:
        """One row per (tier, model) combination used in the last `days`
        days, with summed input/output tokens and a call count -- the
        Settings -> Usage panel's per-tier breakdown table."""
        return list(self._conn.execute(
            "SELECT tier, model, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, COUNT(*) AS calls"
            " FROM llm_usage WHERE ts >= ? GROUP BY tier, model"
            " ORDER BY tier, model", (self._since(days),)))

    def daily(self, days: int) -> list[sqlite3.Row]:
        """One row per UTC calendar day with any usage in the last `days`
        days, with summed input/output tokens across every tier/model --
        the Usage panel's per-day mini table. `substr(ts, 1, 10)` reads the
        `YYYY-MM-DD` prefix off the ISO-8601 timestamp `record` writes,
        matching the same UTC-calendar-day convention `TradeJournal.
        trades_today` and the daily digest already use elsewhere in this
        codebase."""
        return list(self._conn.execute(
            "SELECT substr(ts, 1, 10) AS day, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens FROM llm_usage WHERE ts >= ?"
            " GROUP BY day ORDER BY day DESC", (self._since(days),)))

    def total_tokens_since(self, days: int) -> int:
        """Total input+output tokens across every tier/model in the last
        `days` days -- used by the daily digest's one-line cost mention to
        decide whether there's anything to say at all (`> 0`)."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)"
            " AS total FROM llm_usage WHERE ts >= ?", (self._since(days),)).fetchone()
        return int(row["total"]) if row else 0
