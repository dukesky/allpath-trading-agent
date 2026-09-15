from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)


def is_us_market_open(now: datetime | None = None) -> bool:
    """US regular session: Mon-Fri 09:30-16:00 America/New_York.

    No holiday calendar yet (docs/TODO.md) -- a US market holiday reads as
    open. This is the one shared implementation: `scheduler.is_market_hours`,
    `sentinel._us_market_open_now` and `ReviewQueue` (option approvals) all
    call it, so the store layer never has to import `sentinel`."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    et = now.astimezone(ET)
    return et.weekday() < 5 and OPEN <= et.time() < CLOSE


def next_us_market_open(now: datetime | None = None) -> datetime:
    """The next US market open (09:30 America/New_York on a weekday).

    If the market is currently closed, this is the next 09:30 ET strictly
    after `now` that falls on a weekday. If the market is currently open,
    this is the CURRENT session's own open (today's 09:30 ET), not the
    next one -- the approve-link market-closed page (web/routes/approve.py)
    uses this to compare against a pending option approval's token expiry,
    and "the session that's open right now started at ..." is the useful
    answer there, not "tomorrow's open".

    Same naive-as-UTC handling as `is_us_market_open`, and the same no-
    holiday-calendar caveat: a US market holiday reads as an ordinary
    session that opens on schedule."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    et = now.astimezone(ET)
    if is_us_market_open(now):
        return et.replace(hour=OPEN.hour, minute=OPEN.minute, second=0, microsecond=0)
    candidate_date = et.date()
    if not (et.weekday() < 5 and et.time() < OPEN):
        # Today's own open (if any) either doesn't exist (weekend) or has
        # already passed -- the next candidate is tomorrow at the earliest.
        candidate_date += timedelta(days=1)
    while candidate_date.weekday() >= 5:
        candidate_date += timedelta(days=1)
    return datetime.combine(candidate_date, OPEN, tzinfo=ET)
