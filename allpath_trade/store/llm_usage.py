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

    def summary_for_day(self, date_utc: str | None = None) -> list[sqlite3.Row]:
        """One row per (tier, model) combination used on `date_utc`
        (`YYYY-MM-DD`, UTC calendar day; defaults to today), with summed
        input/output tokens and a call count.

        This is the daily digest's "LLM cost today" source, and deliberately
        NOT `summary(1)` (a rolling 24h window from the instant it's called)
        -- `TradeJournal.trades_today` and the digest's own `triggers` count
        both use a UTC-calendar-day cut (see `_send_daily_digest`'s
        docstring), and a "today" label on a rolling-24h number would show
        yesterday evening's usage as part of "today" for anyone running the
        digest job before midnight UTC, or double up across midnight for
        anyone running it after. `substr(ts, 1, 10)` reads the `YYYY-MM-DD`
        prefix off the ISO-8601 timestamp `record` writes, same convention
        `daily` above already uses."""
        day = date_utc or datetime.now(UTC).date().isoformat()
        return list(self._conn.execute(
            "SELECT tier, model, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, COUNT(*) AS calls"
            " FROM llm_usage WHERE substr(ts, 1, 10) = ? GROUP BY tier, model"
            " ORDER BY tier, model", (day,)))

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

