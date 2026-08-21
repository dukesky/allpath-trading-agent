from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from allpath_trade.execution import refresh_pending_fills
from allpath_trade.notify import events
from allpath_trade.sentinel import SentinelReport
from allpath_trade.store.accounts import DEFAULT_ACCOUNT
from allpath_trade.store.app_state import (
    SENTINEL_HEARTBEAT_KEY,
    SENTINEL_MARKET_OPEN_KEY,
    AppState,
)

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


# shadow-dual-active T4: `accounts` is a `dict[str, app.AccountComponents]`
# -- duck-typed here (only `.journal .broker .sentinel` are read) rather
# than importing `AccountComponents` from app.py, to keep this module free
# of an app.py dependency (cli.py/web/app.py already import both; this
# module itself doesn't need to).
def _run_sentinel_pass(accounts: dict,
                       app_state: AppState | None = None,
                       on_report: Callable[[str, SentinelReport | None], None]
                       | None = None) -> None:
    """Run one sentinel pass PER ACCOUNT (shadow-dual-active T4, spec §③),
    each only actually evaluating strategies during market hours.

    Each account's fill-refresh + sentinel run is wrapped in its OWN
    try/except: paper's sentinel raising must never stop shadow's pass from
    running (or the reverse) -- see the dual-sentinel-isolation tests in
    tests/test_scheduler.py. `on_report` (if given) is called once per
    account with `(account, report)` -- `report` is `None` when that
    account's pass was skipped (market closed) or itself failed (already
    printed to stderr below).

    Heartbeats: every account gets its OWN `sentinel_last_pass:{account}`
    key, written unconditionally (market open or closed -- it proves the
    *scheduler* is alive for that account, not the market) before that
    account's fill-refresh/sentinel logic runs. The LEGACY (un-suffixed)
    `SENTINEL_HEARTBEAT_KEY` is ALSO still written, mirroring paper's own
    timestamp -- the dashboard (web/routes/dashboard.py) reads exactly that
    key today, and Task 7 is what teaches it to read the per-account keys
    instead; keeping this write means paper's existing "last check Nm ago"
    tile stays accurate without waiting on Task 7 to land first.

    The market-open flag (`SENTINEL_MARKET_OPEN_KEY`) is written ONCE,
    before the per-account loop, not per account -- the market is the
    market regardless of which account's sentinel is evaluating it; a
    second per-account copy of the same boolean would just be duplication,
    not a real second fact.

    The pending-fill refresh (`acc.journal`/`acc.broker`) also runs
    unconditionally per account, before that account's market-hours gate,
    same reasoning as the heartbeat: a DAY order queued outside market
    hours fills at the next open, and the first pass after that open must
    pick it up without waiting on the market-hours-gated sentinel logic
    below (which only evaluates strategies, not fills) -- see
    execution.refresh_pending_fills and Order.filled_at."""
    if app_state is not None:
        try:
            app_state.set(SENTINEL_MARKET_OPEN_KEY,
                          "true" if is_market_hours() else "false")
        except Exception as exc:  # noqa: BLE001 — a failed flag write must not stop the pass
            print(f"[heartbeat] market-open flag failed: {exc}", file=sys.stderr)

    for account, acc in accounts.items():
        if app_state is not None:
            try:
                now_iso = datetime.now(UTC).isoformat()
                app_state.set(f"{SENTINEL_HEARTBEAT_KEY}:{account}", now_iso)
                if account == DEFAULT_ACCOUNT:
                    # Legacy compat write -- see docstring above.
                    app_state.set(SENTINEL_HEARTBEAT_KEY, now_iso)
            except Exception as exc:  # noqa: BLE001 — a failed heartbeat must not stop the pass
                print(f"[heartbeat] {account} failed: {exc}", file=sys.stderr)
        try:
            refresh_pending_fills(acc.journal, acc.broker)
        except Exception as exc:  # noqa: BLE001 — a dead broker must not stop the pass
            print(f"[fill-refresh] {account} failed: {exc}", file=sys.stderr)

        report: SentinelReport | None = None
        if is_market_hours():
            try:
                report = acc.sentinel.run_once()
            except Exception as exc:  # noqa: BLE001 — one account's sentinel
                # failing must never take the other account's pass down too.
                print(f"[sentinel] {account} failed: {exc}", file=sys.stderr)
        if on_report is not None:
            on_report(account, report)


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


def run_daemon(get_accounts: Callable[[], dict], interval_minutes: int,
               scheduler_cls: type = BlockingScheduler,
               daily_job: Callable[[], None] | None = None,
               app_state: AppState | None = None) -> None:
    """The headless `allpath-trade run` daemon -- shadow-dual-active T4:
    `get_accounts` returns the current `dict[str, app.AccountComponents]`
    (a callable, like `build_jobs`'s `holder.get()`, so a future live
    rebuild is possible even though `cli.py`'s own caller today just
    returns the same dict every time) rather than a single sentinel/
    journal/broker triple -- `_run_sentinel_pass` iterates it, one pass per
    account, each isolated in its own try/except."""
    state = {"last_daily": None}

    def report_progress(account: str, report: SentinelReport | None) -> None:
        if report is None:
            # Market closed, or that account's own pass already failed and
            # printed to stderr -- nothing new to say here.
            return
        print(f"[sentinel:{account}] checked={report.strategies_checked} "
              f"triggers={len(report.outcomes)} errors={len(report.errors)}")
        for o in report.outcomes:
            print(f"  {o.strategy_id}/{o.rule_id}: {o.disposition} {o.detail}")
        for e in report.errors:
            print(f"  error: {e}")

    def job() -> None:
        _run_sentinel_pass(get_accounts(), app_state=app_state,
                           on_report=report_progress)
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


def _account_has_active_strategy(strategies) -> bool:
    """The reflection cost gate (spec §③: "反思跳过(无 active 策略)") -- an
    account with zero active strategies (a fresh `shadow` ledger the user
    hasn't written anything for yet is the common case) must not burn a
    nightly LLM call reviewing nothing. `load_all`'s default `status=
    StrategyStatus.ACTIVE` filter already does the filtering; this just
    asks "is that list non-empty" without needing the caller to import
    StrategyStatus. Errors degrade to "no active strategies" (skip) rather
    than raising -- a broken strategy YAML must never be what decides
    tonight's LLM spend, and `Reflector.run_daily` would hit the same
    broken file itself if this let it through anyway."""
    try:
        return bool(strategies.load_all())
    except Exception:  # noqa: BLE001
        return False


def _run_account_daily(account: str, acc, settings, *, verbose: bool) -> None:
    """One account's reflection -> consolidation pair (shadow-dual-active
    T4, spec §③). Order matches `run_daily_jobs`'s own docstring: reflection
    before consolidation, so the same night's consolidation pass can pick up
    reflection's memory_update conclusions. Each step keeps its own
    try/except, same as the pre-dual-active single-account version, so one
    broken task never silently prevents the other from running -- and,
    since this whole function is called once per account from a loop, a
    failure inside it (there shouldn't be one left uncaught, but belt and
    suspenders) must never be allowed to stop the OTHER account's nightly
    chain either; see run_daily_jobs's own per-account try/except."""
    reflector = acc.reflector
    if reflector is not None and settings.daily_reflection:
        if _account_has_active_strategy(acc.strategies):
            try:
                status = reflector.run_daily()
                if verbose:
                    print(f"[reflection:{account}] {status}")
            except Exception as exc:  # noqa: BLE001 — must not stop consolidation
                print(f"[reflection:{account}] failed: {exc}", file=sys.stderr)
        elif verbose:
            print(f"[reflection:{account}] skipped (no active strategies)")

    consolidator = acc.consolidator
    if consolidator is not None and settings.daily_consolidation:
        try:
            status = consolidator.run_daily()
            if verbose:
                print(f"[memory:{account}] {status}")
        except Exception as exc:  # noqa: BLE001 — see comment above
            print(f"[consolidation:{account}] failed: {exc}")


def run_daily_jobs(components, verbose: bool = False) -> None:
    """The after-close daily sequence, shared by `build_jobs` (`serve`) and
    `cli.py`'s `run` daemon so the two entry points can't drift out of sync
    again (docs/TODO.md's Phase 5 leftover: `run` used to skip the digest
    entirely and never gated consolidation on `daily_consolidation`).

    Order: digest -> {reflection -> consolidation} PER ACCOUNT
    (shadow-dual-active T4, spec §③) -- within one account, reflection
    still runs before consolidation (spec §①: reflection's memory_update
    conclusions need to land before consolidation runs, so the same
    night's pass can pick them up); each account's whole
    reflection+consolidation pair is further isolated in its own
    try/except in the loop below, so paper's nightly chain failing outright
    can never silently prevent shadow's from running, or the reverse.

    The digest fires unconditionally, once, BEFORE the per-account loop --
    shadow-dual-active T4 deliberately does NOT explode it into one digest
    per account (that -- and the `[Paper]`/`[Shadow]` subject prefixes
    every other notification eventually gets -- is Task 7's job per the
    plan; see docs/TODO.md). It keeps reading through the legacy
    `components.observations`/`.journal`/`.queue` attribute alias, i.e.
    paper's own, same single combined summary as before this task.

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

    for account, acc in components.accounts.items():
        try:
            _run_account_daily(account, acc, components.settings, verbose=verbose)
        except Exception as exc:  # noqa: BLE001 — one account's nightly chain
            # must never take the other account's down (belt and
            # suspenders: _run_account_daily already catches its own two
            # steps individually, so this is not expected to be reachable).
            print(f"[daily:{account}] failed: {exc}", file=sys.stderr)


def build_jobs(scheduler, holder) -> None:
    """Attach the sentinel and the after-close daily jobs to a scheduler
    owned by someone else (the `serve` process).

    Same job body as `run_daemon` (see `_run_sentinel_pass` /
    `_maybe_run_daily`), minus the terminal progress lines — the server
    process has no one to print them to."""
    state = {"last_daily": None}

    def job() -> None:
        components = holder.get()
        _run_sentinel_pass(components.accounts, app_state=components.app_state)
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
