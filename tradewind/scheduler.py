from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from tradewind.sentinel import Sentinel

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)


def is_market_hours(now: datetime | None = None) -> bool:
    """US regular session, no holiday calendar yet (see docs/TODO.md)."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    et = now.astimezone(ET)
    return et.weekday() < 5 and OPEN <= et.time() < CLOSE


def run_daemon(sentinel_factory: Callable[[], Sentinel], interval_minutes: int,
               scheduler_cls: type = BlockingScheduler) -> None:
    def job() -> None:
        if not is_market_hours():
            print("[sentinel] market closed, skipping")
            return
        report = sentinel_factory().run_once()
        print(f"[sentinel] checked={report.strategies_checked} "
              f"triggers={len(report.outcomes)} errors={len(report.errors)}")
        for o in report.outcomes:
            print(f"  {o.strategy_id}/{o.rule_id}: {o.disposition} {o.detail}")
        for e in report.errors:
            print(f"  error: {e}")

    scheduler = scheduler_cls()
    scheduler.add_job(job, "interval", minutes=interval_minutes,
                      next_run_time=datetime.now(UTC))
    print(f"[tradewind] sentinel daemon: every {interval_minutes}min "
          "during US market hours (Ctrl-C to stop)")
    scheduler.start()
