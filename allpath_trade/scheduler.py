from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from allpath_trade.notify import events
from allpath_trade.sentinel import Sentinel, SentinelReport
from allpath_trade.store.app_state import (
    SENTINEL_HEARTBEAT_KEY,
    SENTINEL_MARKET_OPEN_KEY,
    AppState,
)

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)

# Stable id for the interval job build_jobs registers, so a later settings
# save can find and reschedule the same job instead of only being able to
# add a second one alongside it (see reschedule_sentinel_job below).
SENTINEL_JOB_ID = "sentinel_pass"


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
                       on_report: Callable[[SentinelReport | None], None] | None = None,
                       app_state: AppState | None = None) -> None:
    """Run one sentinel pass, but only during market hours.

    `on_report` (if given) is called with the resulting `SentinelReport`, or
    with `None` when the pass was skipped because the market is closed — the
    only place that wants to know is `run_daemon`'s terminal output.

    The heartbeat write (`app_state`, if given) happens unconditionally,
    market open or closed: it proves the *scheduler* is alive, not the
    market, and this is the one job body both `run_daemon` (the headless
    daemon) and `build_jobs` (the `serve` process) call through, so writing
    it here covers both without duplicating the call site.

    The market-open flag is written in the same try block, right alongside
    the timestamp -- a reader needs both together to tell "the scheduler
    ticked, and the sentinel actually ran" from "the scheduler ticked, but
    the market was closed so nothing was evaluated" (dashboard.py's
    sentinel_heartbeat_status uses it for exactly that)."""
    if app_state is not None:
        try:
            app_state.set(SENTINEL_HEARTBEAT_KEY, datetime.now(UTC).isoformat())
            app_state.set(SENTINEL_MARKET_OPEN_KEY,
                          "true" if is_market_hours() else "false")
        except Exception as exc:  # noqa: BLE001 — a failed heartbeat must not stop the pass
            print(f"[heartbeat] failed: {exc}", file=sys.stderr)
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
               daily_job: Callable[[], None] | None = None,
               app_state: AppState | None = None) -> None:
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
        _run_sentinel_pass(sentinel_factory, report_progress, app_state)
        _maybe_run_daily(daily_job, state)

    scheduler = scheduler_cls()
    scheduler.add_job(job, "interval", minutes=interval_minutes,
                      next_run_time=datetime.now(UTC))
    print(f"[allpath-trade] sentinel daemon: every {interval_minutes}min "
          "during US market hours (Ctrl-C to stop)")
    scheduler.start()


def _send_daily_digest(components) -> None:
    """Count today's activity and email a summary.

    `trades` comes from `TradeJournal.trades_today()` — the journal's real
    accessor (there is no `journal.today()`). `triggers` counts today's
    "sentinel"-sourced rows in the observation log: `Sentinel._check_strategy`
    logs exactly one there per rule trigger regardless of disposition, so
    this is a real count, not the brief's hardcoded placeholder. Per-strategy
    failures (e.g. a bad quote) log under the distinct "sentinel_error"
    source instead, so they can never inflate this count. `since_iso`
    is a UTC calendar-day boundary, matching `TradeJournal.trades_today`'s
    own day convention; `limit` is set high because `recent()`'s 200-row
    default would silently undercount on a very active day."""
    since_iso = datetime.now(UTC).date().isoformat()
    triggers = sum(1 for row in components.observations.recent(
        since_iso=since_iso, limit=10_000) if row["source"] == "sentinel")
    subject, body = events.daily_digest(
        triggers=triggers, trades=components.journal.trades_today(),
        pending=len(components.queue.list()))
    components.notifier.send(subject, body)


def build_jobs(scheduler, holder) -> None:
    """Attach the sentinel and the after-close daily jobs to a scheduler
    owned by someone else (the `serve` process).

    Same job body as `run_daemon` (see `_run_sentinel_pass` /
    `_maybe_run_daily`), minus the terminal progress lines — the server
    process has no one to print them to."""
    state = {"last_daily": None}

    def job() -> None:
        components = holder.get()
        _run_sentinel_pass(lambda: components.sentinel, app_state=components.app_state)

        def daily() -> None:
            # Consolidation stays gated by its own setting; the digest email
            # fires unconditionally (it doesn't depend on daily_consolidation
            # being on). Both run under the same _maybe_run_daily call so
            # they share one once-per-day gate. Consolidation failure is
            # caught right here, separately from _maybe_run_daily's own
            # handler, so it costs only itself: the digest is the user's
            # daily signal that the system is alive, and a broken
            # consolidator must not also silence that signal — it should be
            # visible in the digest's own trigger/trade counts instead.
            consolidator = components.consolidator
            if consolidator is not None and components.settings.daily_consolidation:
                try:
                    consolidator.run_daily()
                except Exception as exc:  # noqa: BLE001 — see comment above
                    print(f"[consolidation] failed: {exc}")
            _send_daily_digest(components)

        _maybe_run_daily(daily, state)

    scheduler.add_job(job, "interval",
                      minutes=holder.settings().sentinel_interval_minutes,
                      next_run_time=datetime.now(UTC), id=SENTINEL_JOB_ID)


def reschedule_sentinel_job(scheduler, minutes: int) -> None:
    """Apply a new sentinel_interval_minutes to the job build_jobs already
    registered, without a process restart.

    Settings edits go through ComponentHolder.rebuild(), which swaps the
    component graph the *next* request reads -- but the APScheduler
    instance already running the interval job lives on `app.state.scheduler`
    (the `serve` process), a layer rebuild() never touches. Without this,
    `context_budget_tokens` right next to this field on the settings page
    takes effect immediately while the sentinel cadence silently doesn't
    move until the process restarts (Finding 5). The caller is expected to
    guard this against a scheduler that isn't running (a test build,
    `allpath-trade run`'s own daemon) -- see routes/settings.py."""
    scheduler.reschedule_job(SENTINEL_JOB_ID, trigger="interval", minutes=minutes)
