from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from allpath_trade.sentinel import Sentinel, SentinelReport

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


def _run_sentinel_pass(get_sentinel: Callable[[], Sentinel],
                       on_report: Callable[[SentinelReport | None], None] | None = None) -> None:
    """Run one sentinel pass, but only during market hours.

    `on_report` (if given) is called with the resulting `SentinelReport`, or
    with `None` when the pass was skipped because the market is closed — the
    only place that wants to know is `run_daemon`'s terminal output."""
    if is_market_hours():
        report = get_sentinel().run_once()
    else:
        report = None
    if on_report is not None:
        on_report(report)


def _maybe_run_daily(daily_job: Callable[[], None] | None, state: dict) -> None:
    """Run `daily_job` at most once per ET calendar day, after close.

    `state` is a `{"last_daily": ...}` dict owned by the caller, so both
    `run_daemon` and `build_jobs` can each keep their own once-per-day
    tracking across repeated calls."""
    if daily_job is None or not _is_after_close():
        return
    today = datetime.now(UTC).astimezone(ET).date().isoformat()
    if state["last_daily"] == today:
        return
    state["last_daily"] = today
    try:
        daily_job()
    except Exception as exc:  # noqa: BLE001 — a failed digest must not stop the loop
        print(f"[daily] failed: {exc}")


def run_daemon(sentinel_factory: Callable[[], Sentinel], interval_minutes: int,
               scheduler_cls: type = BlockingScheduler,
               daily_job: Callable[[], None] | None = None) -> None:
    state = {"last_daily": None}

    def report_progress(report: SentinelReport | None) -> None:
        if report is None:
            print("[sentinel] market closed, skipping")
            return
        print(f"[sentinel] checked={report.strategies_checked} "
              f"triggers={len(report.outcomes)} errors={len(report.errors)}")
        for o in report.outcomes:
            print(f"  {o.strategy_id}/{o.rule_id}: {o.disposition} {o.detail}")
        for e in report.errors:
            print(f"  error: {e}")

    def job() -> None:
        _run_sentinel_pass(sentinel_factory, report_progress)
        _maybe_run_daily(daily_job, state)

    scheduler = scheduler_cls()
    scheduler.add_job(job, "interval", minutes=interval_minutes,
                      next_run_time=datetime.now(UTC))
    print(f"[allpath-trade] sentinel daemon: every {interval_minutes}min "
          "during US market hours (Ctrl-C to stop)")
    scheduler.start()


def build_jobs(scheduler, holder) -> None:
    """Attach the sentinel and the after-close consolidation to a scheduler
    owned by someone else (the `serve` process).

    Same job body as `run_daemon` (see `_run_sentinel_pass` /
    `_maybe_run_daily`), minus the terminal progress lines — the server
    process has no one to print them to."""
    state = {"last_daily": None}

    def job() -> None:
        components = holder.get()
        _run_sentinel_pass(lambda: components.sentinel)
        consolidator = components.consolidator
        daily_job = None
        if consolidator is not None and components.settings.daily_consolidation:
            daily_job = consolidator.run_daily
        _maybe_run_daily(daily_job, state)

    scheduler.add_job(job, "interval",
                      minutes=holder.settings().sentinel_interval_minutes,
                      next_run_time=datetime.now(UTC))
