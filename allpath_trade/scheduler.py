from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from allpath_trade.sentinel import Sentinel

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


def _is_after_close(now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    et = now.astimezone(ET)
    return et.weekday() < 5 and et.time() >= time(16, 5)


def run_daemon(sentinel_factory: Callable[[], Sentinel], interval_minutes: int,
               scheduler_cls: type = BlockingScheduler,
               daily_job: Callable[[], None] | None = None) -> None:
    state = {"last_daily": None}

    def job() -> None:
        if not is_market_hours():
            print("[sentinel] market closed, skipping")
        else:
            report = sentinel_factory().run_once()
            print(f"[sentinel] checked={report.strategies_checked} "
                  f"triggers={len(report.outcomes)} errors={len(report.errors)}")
            for o in report.outcomes:
                print(f"  {o.strategy_id}/{o.rule_id}: {o.disposition} {o.detail}")
            for e in report.errors:
                print(f"  error: {e}")

        if daily_job is not None and _is_after_close():
            today = datetime.now(UTC).astimezone(ET).date().isoformat()
            if state["last_daily"] != today:
                state["last_daily"] = today
                try:
                    daily_job()
                except Exception as exc:  # noqa: BLE001
                    print(f"[daily] failed: {exc}")

    scheduler = scheduler_cls()
    scheduler.add_job(job, "interval", minutes=interval_minutes,
                      next_run_time=datetime.now(UTC))
    print(f"[allpath-trade] sentinel daemon: every {interval_minutes}min "
          "during US market hours (Ctrl-C to stop)")
    scheduler.start()
