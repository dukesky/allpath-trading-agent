from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from allpath_trade.broker.base import Broker
from allpath_trade.execution import refresh_pending_fills
from allpath_trade.notify import events
from allpath_trade.sentinel import Sentinel, SentinelReport
from allpath_trade.store.app_state import (
    SENTINEL_HEARTBEAT_KEY,
    SENTINEL_MARKET_OPEN_KEY,
    AppState,
)
from allpath_trade.store.journal import TradeJournal

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)


def ts_to_et_date(ts_iso: str) -> str | None:
    """Convert an ISO timestamp string to its ET calendar date (`YYYY-MM-DD`),
    or `None` if `ts_iso` doesn't parse. Naive timestamps are treated as UTC.

    Lives here (rather than a standalone timeutil module) because this file
    already owns `ET` and is already an accepted web-layer dependency
    (dashboard.py, settings.py, app.py all import from it) -- reflect.py and
    web/routes/reports.py both need the exact same ET-day cut for the
    per-row date grouping in daily reflection briefings and report pages."""
    try:
        dt = datetime.fromisoformat(ts_iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET).date().isoformat()


def today_et_date(now: datetime | None = None) -> str:
    """Today's ET calendar date (`YYYY-MM-DD`) -- the same "convert to ET,
    take the date" rule as `ts_to_et_date` above, just anchored to `now`
    (real clock by default) instead of a stored row's timestamp. Used by
    web/routes/reports.py's quick date-filter chips (Today/This
    week/This month), which need "today" in the same calendar the reports
    themselves are keyed by, not the server process's local/UTC date."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(ET).date().isoformat()

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
                       app_state: AppState | None = None,
                       journal: TradeJournal | None = None,
                       broker: Broker | None = None) -> None:
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
    sentinel_heartbeat_status uses it for exactly that).

    The pending-fill refresh (`journal`/`broker`, if both given) also runs
    unconditionally, before the market-hours gate, same as the heartbeat and
    for the same reason: a DAY order queued outside market hours fills at
    the next open, and the first pass after that open must pick it up
    without waiting on the market-hours-gated sentinel logic below (which
    only evaluates strategies, not fills) -- see execution.refresh_pending_
    fills and Order.filled_at. Deliberately does NOT call get_sentinel():
    that factory is reserved for the market-hours branch below (some
    callers assert it is never invoked while the market is closed), so the
    refresh gets its own journal/broker instead of reaching into the
    sentinel for them."""
    if app_state is not None:
        try:
            app_state.set(SENTINEL_HEARTBEAT_KEY, datetime.now(UTC).isoformat())
            app_state.set(SENTINEL_MARKET_OPEN_KEY,
                          "true" if is_market_hours() else "false")
        except Exception as exc:  # noqa: BLE001 — a failed heartbeat must not stop the pass
            print(f"[heartbeat] failed: {exc}", file=sys.stderr)
    if journal is not None and broker is not None:
        try:
            refresh_pending_fills(journal, broker)
        except Exception as exc:  # noqa: BLE001 — a dead broker must not stop the pass
            print(f"[fill-refresh] failed: {exc}", file=sys.stderr)
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
               app_state: AppState | None = None,
               journal: TradeJournal | None = None,
               broker: Broker | None = None) -> None:
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
        _run_sentinel_pass(sentinel_factory, report_progress, app_state,
                           journal=journal, broker=broker)
        _maybe_run_daily(daily_job, state)

    scheduler = scheduler_cls()
    scheduler.add_job(job, "interval", minutes=interval_minutes,
                      next_run_time=datetime.now(UTC))
    print(f"[allpath-trade] sentinel daemon: every {interval_minutes}min "
          "during US market hours (Ctrl-C to stop)")
    scheduler.start()


# app_state key for the digest's own once-per-ET-day dedup (see
# _send_daily_digest below) -- distinct from TURN_MARKER_KEY
# (memory/consolidate.py) and the reports table's per-ET-date row
# (reflect.py), which are the equivalent persisted markers for
# consolidation and reflection respectively.
DIGEST_LAST_DATE_KEY = "digest_last_date"


def _llm_cost_line(components) -> str:
    """The digest's one-line "estimated LLM cost today" mention -- `""`
    (no line at all, per `events.daily_digest`'s own contract) whenever
    there was no LLM usage today, so a day with an LLM unconfigured or
    simply unused stays exactly as silent about cost as the digest always
    was before this feature existed.

    Sums `estimate_cost` (llm/prices.py) per (tier, model) row from
    `LLMUsage.summary_for_day()` -- the UTC-calendar-day cut, matching
    `trades_today`'s and this same digest's `triggers` count's own "today"
    convention (see `_send_daily_digest`'s docstring) -- rather than
    `summary(1)`'s rolling 24h window, which would mislabel part of
    yesterday's (or a slice of tonight's not-yet-"today") usage as
    "today". Cost genuinely depends on which model each row's tokens ran
    against, so this sums per-row rather than pricing one pre-aggregated
    total.

    Never raises: reading usage is best-effort here, the same way
    recording it already is (`LLMUsage.record`'s own docstring) -- this
    runs synchronously before `notifier.send` inside `_send_daily_digest`,
    which already marks `digest_last_date` done regardless of outcome, so
    an unhandled exception here would silently cancel the ENTIRE digest for
    the day (trigger/trade/pending counts included) over what's ultimately
    just one optional line."""
    try:
        from allpath_trade.llm.prices import estimate_cost
        from allpath_trade.web.format import money

        total = Decimal(0)
        any_default_rate = False
        for row in components.llm_usage.summary_for_day():
            cost, is_default = estimate_cost(row["model"], row["input_tokens"],
                                              row["output_tokens"])
            total += cost
            any_default_rate = any_default_rate or is_default
        if total <= 0:
            return ""
        formatted = money(total)
        if formatted == "$0.00":
            # A real, nonzero cost that `money`'s 2-decimal rounding would
            # otherwise silently present as "$0.00" -- e.g. a handful of
            # haiku-tier calls costing a fraction of a cent. 4 decimals
            # keeps it honest instead of implying there was no cost at all.
            formatted = f"${total:.4f}"
        if any_default_rate:
            formatted += " (some rates estimated)"
        return formatted
    except Exception as exc:  # noqa: BLE001 — see docstring: must never cancel the digest
        print(f"[digest] llm cost line failed: {exc}", file=sys.stderr)
        return ""


def _send_daily_digest(components) -> None:
    """Count today's activity and email a summary, at most once per ET
    calendar day.

    `trades` comes from `TradeJournal.trades_today()` — the journal's real
    accessor (there is no `journal.today()`). `triggers` counts today's
    "sentinel"-sourced rows in the observation log: `Sentinel._check_strategy`
    logs exactly one there per rule trigger regardless of disposition, so
    this is a real count, not the brief's hardcoded placeholder. Per-strategy
    failures (e.g. a bad quote) log under the distinct "sentinel_error"
    source instead, so they can never inflate this count. `since_iso`
    is a UTC calendar-day boundary, matching `TradeJournal.trades_today`'s
    own day convention; `limit` is set high because `recent()`'s 200-row
    default would silently undercount on a very active day.

    The `digest_last_date` guard exists because `_maybe_run_daily`'s
    once-per-day gate (`state["last_daily"]`) is in-memory only: a `serve`
    restart after 16:05 forgets today's digest already went out and would
    otherwise re-send it on the next tick. Reflection doesn't have this
    problem — `Reflector.run_daily` (reflect.py) checks `reports.exists
    (et_date)`, a persisted table row — and consolidation tracks its own
    turn marker the same way; this mirrors that pattern for the digest
    specifically, without touching `_maybe_run_daily` itself."""
    today = datetime.now(UTC).astimezone(ET).date().isoformat()
    if components.app_state.get(DIGEST_LAST_DATE_KEY) == today:
        return
    since_iso = datetime.now(UTC).date().isoformat()
    triggers = sum(1 for row in components.observations.recent(
        since_iso=since_iso, limit=10_000) if row["source"] == "sentinel")
    subject, body = events.daily_digest(
        triggers=triggers, trades=components.journal.trades_today(),
        pending=len(components.queue.list()), llm_cost=_llm_cost_line(components))
    components.notifier.send(subject, body)
    components.app_state.set(DIGEST_LAST_DATE_KEY, today)


def run_daily_jobs(components, verbose: bool = False) -> None:
    """The after-close daily sequence, shared by `build_jobs` (`serve`) and
    `cli.py`'s `run` daemon so the two entry points can't drift out of sync
    again (docs/TODO.md's Phase 5 leftover: `run` used to skip the digest
    entirely and never gated consolidation on `daily_consolidation`).

    Order: digest -> reflection -> consolidation (spec §①: reflection's
    memory_update conclusions need to land before consolidation runs, so
    the same night's consolidation pass can pick them up). Each step gets
    its OWN try/except so one broken task can never silently prevent the
    other two from running. The digest fires unconditionally (it doesn't
    depend on daily_consolidation or daily_reflection, though it dedupes
    itself against a same-day restart -- see _send_daily_digest); reflection
    and consolidation each stay gated by their own setting.

    Callers are expected to wrap this in their own once-per-day gate (see
    `_maybe_run_daily`) -- this function itself is state-free.

    `verbose=True` restores the success-path prints the headless `run`
    daemon had before this helper was extracted: `run` has no web UI, so
    its stdout is the operator's only window into whether the nightly
    chain did anything. `serve` keeps verbose=False -- its operator reads
    the Reports page, and the pre-refactor daily() never printed on
    success there either."""
    try:
        _send_daily_digest(components)
    except Exception as exc:  # noqa: BLE001 — a failed digest must not stop the rest
        print(f"[digest] failed: {exc}")

    reflector = components.reflector
    if reflector is not None and components.settings.daily_reflection:
        try:
            status = reflector.run_daily()
            if verbose:
                print(f"[reflection] {status}")
        except Exception as exc:  # noqa: BLE001 — must not stop consolidation
            print(f"[reflection] failed: {exc}", file=sys.stderr)

    consolidator = components.consolidator
    if consolidator is not None and components.settings.daily_consolidation:
        try:
            status = consolidator.run_daily()
            if verbose:
                print(f"[memory] {status}")
        except Exception as exc:  # noqa: BLE001 — see comment above
            print(f"[consolidation] failed: {exc}")


def build_jobs(scheduler, holder) -> None:
    """Attach the sentinel and the after-close daily jobs to a scheduler
    owned by someone else (the `serve` process).

    Same job body as `run_daemon` (see `_run_sentinel_pass` /
    `_maybe_run_daily`), minus the terminal progress lines — the server
    process has no one to print them to."""
    state = {"last_daily": None}

    def job() -> None:
        components = holder.get()
        _run_sentinel_pass(lambda: components.sentinel, app_state=components.app_state,
                           journal=components.journal, broker=components.broker)
        _maybe_run_daily(lambda: run_daily_jobs(components), state)

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
