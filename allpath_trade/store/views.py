from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from allpath_trade.store.accounts import is_valid_account

# A view the owner never revisits should stop steering reflection on its own;
# 30 days unless the agent files it with a different horizon, never more than
# half a year.
DEFAULT_VIEW_DAYS = 30
MAX_VIEW_DAYS = 180
MARKET_SUBJECT = "MARKET"
_MAX_FIELD_CHARS = 500


def _clean(name: str, value: str) -> str:
    value = (value or "").strip()[:_MAX_FIELD_CHARS]
    if not value:
        raise ValueError(f"{name} is required")
    return value


class ViewStore:
    """The owner's dated views (shadow check-in pilot, spec
    2026-11-06-shadow-checkin-design.md): one row per stated position on a
    ticker, a theme, or the market as a whole (`MARKET`). Rows are never
    edited; a changed view is a new row, and old ones age out at
    `expires_ts`."""

    def __init__(self, conn: sqlite3.Connection, account: str) -> None:
        if not is_valid_account(account):
            raise ValueError(f"invalid account: {account!r}")
        self._conn = conn
        self._account = account

    def add(self, *, subject: str, stance: str, horizon: str, conviction: str,
            invalidation: str, expires_days: int | None = None,
            source: str = "checkin", now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        days = DEFAULT_VIEW_DAYS if expires_days is None else expires_days
        days = max(1, min(int(days), MAX_VIEW_DAYS))
        cur = self._conn.execute(
            "INSERT INTO views (account, ts, subject, stance, horizon, conviction,"
            " invalidation, source, expires_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self._account, now.isoformat(), _clean("subject", subject).upper(),
             _clean("stance", stance), _clean("horizon", horizon),
             _clean("conviction", conviction), _clean("invalidation", invalidation),
             source, (now + timedelta(days=days)).isoformat()))
        self._conn.commit()
        return int(cur.lastrowid)

    def active(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(UTC)
        rows = self._conn.execute(
            "SELECT * FROM views WHERE account = ? AND expires_ts > ?"
            " ORDER BY id", (self._account, now.isoformat())).fetchall()
        return [dict(r) for r in rows]

    def relevant(self, ticker: str, now: datetime | None = None) -> list[dict]:
        """Active views a revision of `ticker`'s strategy has to answer to:
        that ticker's own, plus market-wide ones. Theme views are shown to
        reflection in the briefing but never force an alignment -- matching
        a theme to a ticker would be a guess."""
        wanted = {ticker.strip().upper(), MARKET_SUBJECT}
        return [v for v in self.active(now) if v["subject"] in wanted]


class CheckinStore:
    """One row per (account, ET date) the daily check-in actually sent --
    the idempotency marker, and how the Telegram poller recognises a reply
    to a check-in (by the sent message's id)."""

    def __init__(self, conn: sqlite3.Connection, account: str) -> None:
        if not is_valid_account(account):
            raise ValueError(f"invalid account: {account!r}")
        self._conn = conn
        self._account = account

    def exists_for(self, et_date: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM checkins WHERE account = ? AND et_date = ?",
            (self._account, et_date)).fetchone() is not None

    def add(self, *, et_date: str, questions: list[str],
            telegram_message_id: int | None, now: datetime | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO checkins (account, et_date, ts, questions,"
            " telegram_message_id) VALUES (?, ?, ?, ?, ?)",
            (self._account, et_date, (now or datetime.now(UTC)).isoformat(),
             json.dumps(questions, ensure_ascii=False), telegram_message_id))
        self._conn.commit()
        return int(cur.lastrowid)

    def by_message_id(self, message_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM checkins WHERE account = ? AND telegram_message_id = ?",
            (self._account, message_id)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["questions"] = json.loads(out["questions"])
        return out
