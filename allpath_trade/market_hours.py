from __future__ import annotations

from datetime import UTC, datetime, time
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
