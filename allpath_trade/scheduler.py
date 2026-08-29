from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from allpath_trade.broker.unconfigured import UnconfiguredBroker
from allpath_trade.execution import refresh_pending_fills
from allpath_trade.notify import events
from allpath_trade.sentinel import SentinelReport
from allpath_trade.store.accounts import DEFAULT_ACCOUNT
from allpath_trade.store.app_state import (
    SENTINEL_HEARTBEAT_KEY,
    SENTINEL_LAST_OK_KEY,
    SENTINEL_MARKET_OPEN_KEY,
    AppState,
)

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)


def _is_unconfigured(acc) -> bool:
    """setup-wizard T1: does this account's bundle still carry the
    placeholder broker (no Alpaca keys yet)?

    One shared predicate for the three scheduled surfaces that must skip
    such an account -- the sentinel pass, the nightly digest, and the
    nightly reflection/consolidation chain -- so they can never disagree
    about what "not configured" means. An isinstance check rather than
    `getattr(acc.broker, "name", "") == "unconfigured"`: the class is the
    fact, and a string compare would silently start passing for any future
    broker that happened to reuse the name. `getattr` on the bundle itself
    keeps this safe for the lightweight namespaces tests build."""
    return isinstance(getattr(acc, "broker", None), UnconfiguredBroker)


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

# I8: the after-close chain (digest -> reflection -> consolidation) is its
# own interval job, separate from the sentinel tick -- see build_jobs. Its
# cadence is NOT the user-configurable sentinel interval: this job is a
# cheap "is it after close, and is today still unfinished?" poll
# (_maybe_run_daily returns immediately in almost every call), so it wants a
# fixed, frequent cadence -- 5 minutes bounds how long after 16:05 the
# night's chain starts, and how long after a failed attempt the retry lands,
# regardless of whether the operator set the sentinel to 5 minutes or 60.
DAILY_CHECK_INTERVAL_MINUTES = 5
DAILY_JOB_ID = "daily_chain"


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


def _drop_legacy_heartbeat(app_state: AppState) -> None:
    """Delete the orphan pre-dual-active `sentinel_last_pass` row.

    T7 stopped writing the bare (un-suffixed) key and the dashboard stopped
    reading it, but an install that ran before T7 still has the row sitting
    in `app_state`, frozen at whatever timestamp the last pre-T7 tick wrote.
    Anyone reading the table (a support session, a future feature keying off
    "is the daemon alive") sees what looks like live state and is years out
    of date. Reads first and deletes only when it's actually there, so the
    steady state -- every install, every tick after the first -- is one
    cheap SELECT and no write at all, rather than a DELETE+commit per tick.

    Never raises: this is housekeeping, and a locked/broken store must not
    be what stops a monitoring pass."""
    try:
        if app_state.get(SENTINEL_HEARTBEAT_KEY) is not None:
            app_state.delete(SENTINEL_HEARTBEAT_KEY)
    except Exception as exc:  # noqa: BLE001 — housekeeping must not stop the pass
        print(f"[heartbeat] legacy row cleanup failed: {exc}", file=sys.stderr)


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
    account's fill-refresh/sentinel logic runs.

    shadow-dual-active T7 (carried from T4): the LEGACY (un-suffixed)
    `SENTINEL_HEARTBEAT_KEY` used to also be written, mirroring paper's own
    timestamp, because the dashboard read exactly that bare key. The
    dashboard (web/routes/dashboard.py, T5) now reads the per-account
    `sentinel_last_pass:{account}` key for whichever account is currently
    being viewed -- nothing left reads the bare key -- so this task drops
    the legacy write entirely rather than carrying dead compat code
    forward. The bare `SENTINEL_HEARTBEAT_KEY` constant itself stays
    (still imported by the dashboard for the per-account key's prefix, and
    still exercised by `run_daemon`'s own single-account tests), it just
    never gets its own un-suffixed row written any more.

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
        _drop_legacy_heartbeat(app_state)

    for account, acc in accounts.items():
        if app_state is not None:
            try:
                now_iso = datetime.now(UTC).isoformat()
                # shadow-dual-active T7: per-account key only -- see the
                # docstring above for why the legacy un-suffixed write was
                # dropped (nothing reads it any more).
                app_state.set(f"{SENTINEL_HEARTBEAT_KEY}:{account}", now_iso)
            except Exception as exc:  # noqa: BLE001 — a failed heartbeat must not stop the pass
                print(f"[heartbeat] {account} failed: {exc}", file=sys.stderr)

        # setup-wizard T1: an account with no credentials yet is SKIPPED,
        # not run-and-caught. Letting it fall through would work (every
        # broker call raises BrokerNotConfigured and the generic handlers
        # below would catch it), but it would spend a fill-refresh and a
        # full sentinel pass per tick to reach a foregone conclusion and
        # print a stack-flavoured "failed:" line for what is really just an
        # unfinished setup. The heartbeat above IS still written -- the
        # daemon genuinely is alive and ticking for this account; only the
        # `sentinel_last_ok` success key below is withheld, because nothing
        # was actually checked (that key is exactly what stops the
        # dashboard claiming a healthy monitor here). `on_report(.., None)`
        # matches the market-closed skip: no report to show.
        if _is_unconfigured(acc):
            print(f"[sentinel] {account}: Alpaca keys not set — skipping",
                  file=sys.stderr)
            if on_report is not None:
                on_report(account, None)
            continue

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
            else:
                # I7: the SUCCESS heartbeat, written only once run_once has
                # returned -- see SENTINEL_LAST_OK_KEY. Deliberately inside
                # the market-hours branch and in the `else` of the
                # try/except: a tick that evaluated nothing (market closed)
                # or whose evaluation raised has not successfully checked
                # anything, and must not be able to claim it did.
                if app_state is not None:
                    try:
                        app_state.set(f"{SENTINEL_LAST_OK_KEY}:{account}",
                                      datetime.now(UTC).isoformat())
                    except Exception as exc:  # noqa: BLE001 — see above
                        print(f"[heartbeat] {account} last-ok failed: {exc}",
                              file=sys.stderr)
        if on_report is not None:
            on_report(account, report)


# I4(b): how many times one ET day's nightly chain may be attempted before
# the in-memory guard stops retrying it. Retrying at all is the point (a
# failed or partial night must not be unreachable until tomorrow), but an
# unbounded retry would re-run the chain on EVERY tick of a day whose
# failure is permanent -- a dead LLM provider, a full disk -- and each
# attempt costs real work (and, once past the digest, real LLM calls). Three
# attempts is enough to ride out a transient outage without turning a
# genuinely broken night into an all-night loop; the operator's signal is
# the "giving up" stderr line the third failure prints.
MAX_DAILY_ATTEMPTS = 3


def new_daily_state() -> dict:
    """The `state` dict `_maybe_run_daily` owns across ticks -- one per
    caller (`run_daemon`, `build_jobs`), so each entry point keeps its own
    once-per-day tracking. Constructed here rather than inline at each call
    site so the three keys can never drift apart between them."""
    return {"last_daily": None, "attempt_date": None, "attempts": 0}


def _maybe_run_daily(daily_job: Callable[[], object] | None, state: dict) -> None:
    """Run `daily_job` at most once SUCCESSFULLY per ET calendar day, after
    close, and at most `MAX_DAILY_ATTEMPTS` times in total.

    I4(b): `state["last_daily"]` used to be stamped BEFORE the call, so it
    meant "attempted today". Any failure -- a raised exception, or a partial
    night where one account's chain blew up (`run_daily_jobs` returns False
    for that) -- was therefore permanent: the day was marked done and no
    later tick would ever try again, even though the failure was often a
    transient outage minutes long. It now means "completed today", stamped
    only after a clean return.

    Retrying is safe precisely because every sub-step carries its OWN
    persisted idempotency: the digest's `digest_last_date:{account}`
    watermark (now written only on a real send, see `_send_one_digest`),
    reflection's `reports.exists_ok(et_date)` row, and the consolidator's
    turn marker. A retry re-walks the chain and no-ops through everything
    that already succeeded, so the only work it repeats is the work that
    failed.

    `daily_job` may report a partial failure by returning False (that's
    `run_daily_jobs`'s contract); any other return value -- including the
    `None` a caller's own callable returns by default -- counts as success,
    so a caller that doesn't participate in this protocol behaves exactly as
    it did before."""
    if daily_job is None or not _is_after_close():
        return
    today = datetime.now(UTC).astimezone(ET).date().isoformat()
    if state["last_daily"] == today:
        return
    if state.get("attempt_date") != today:
        # A new ET day: the attempt budget is per-day, so a night that
        # burned all three attempts must not disable tomorrow's run too.
        state["attempt_date"] = today
        state["attempts"] = 0
    if state["attempts"] >= MAX_DAILY_ATTEMPTS:
        return
    state["attempts"] += 1
    try:
        ok = daily_job() is not False
    except Exception as exc:  # noqa: BLE001 — a failed digest must not stop the loop
        print(f"[daily] failed: {exc}")
        ok = False
    if ok:
        state["last_daily"] = today
    elif state["attempts"] >= MAX_DAILY_ATTEMPTS:
        print(f"[daily] giving up on {today} after {state['attempts']} attempts",
              file=sys.stderr)


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
    state = new_daily_state()

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

    def daily() -> None:
        _maybe_run_daily(daily_job, state)

    scheduler = scheduler_cls()
    scheduler.add_job(job, "interval", minutes=interval_minutes,
                      next_run_time=datetime.now(UTC), id=SENTINEL_JOB_ID)
    # I8: the nightly chain is its own job here too, not a tail call on the
    # sentinel tick -- `run` and `serve` must not drift apart on this (see
    # build_jobs's own registration for the full reasoning).
    scheduler.add_job(daily, "interval", minutes=DAILY_CHECK_INTERVAL_MINUTES,
                      next_run_time=datetime.now(UTC), id=DAILY_JOB_ID,
                      max_instances=1, coalesce=True)
    print(f"[allpath-trade] sentinel daemon: every {interval_minutes}min "
          "during US market hours (Ctrl-C to stop)")
    scheduler.start()


# app_state key for the digest's own once-per-ET-day dedup (see
# _send_daily_digest below) -- distinct from TURN_MARKER_KEY
# (memory/consolidate.py) and the reports table's per-ET-date row
# (reflect.py), which are the equivalent persisted markers for
# consolidation and reflection respectively.
#
# shadow-dual-active T7 (carried from T4's review, which deliberately left
# this single global while the digest itself was still one email covering
# both accounts): now that the digest is one send PER ACCOUNT, this bare
# name is kept only as the pre-dual-active LEGACY key -- `_digest_date_key`
# below suffixes it with the account, exactly the same
# `TURN_MARKER_KEY`/`_turn_marker_key` shape memory/consolidate.py already
# established, including the same one-time legacy-seed-for-paper fallback.
DIGEST_LAST_DATE_KEY = "digest_last_date"


def _digest_date_key(account: str) -> str:
    return f"{DIGEST_LAST_DATE_KEY}:{account}"


def _last_digest_date(app_state: AppState, account: str) -> str | None:
    """This account's digest watermark, seeding paper's per-account key from
    the pre-dual-active global key exactly once.

    Minor (review): the seed now MIGRATES rather than merely reads -- it
    copies the legacy value into the per-account key and deletes the legacy
    row in the same breath. Leaving the legacy row behind meant the fallback
    stayed live forever: any later deletion of `digest_last_date:paper` (an
    operator resetting the watermark to force a re-send, a future
    account-reset feature) would silently resurrect a stale date from the
    legacy row and suppress the digest instead. `shadow` has no legacy
    history to seed from and simply starts unsent, same as a brand-new
    account should.

    Never raises on the migration write: a failed seed must degrade to
    "read the legacy value and carry on", not cancel the digest."""
    value = app_state.get(_digest_date_key(account))
    if value is None and account == DEFAULT_ACCOUNT:
        value = app_state.get(DIGEST_LAST_DATE_KEY)
        if value is not None:
            try:
                app_state.set(_digest_date_key(account), value)
                app_state.delete(DIGEST_LAST_DATE_KEY)
            except Exception as exc:  # noqa: BLE001 — see docstring
                print(f"[digest] legacy watermark migration failed: {exc}",
                      file=sys.stderr)
    return value


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
    so an unhandled exception here would cancel the ENTIRE digest for every
    account (trigger/trade/pending counts included) over what's ultimately
    just one optional line. (Since I4 an undelivered digest is at least
    retried on the next tick rather than being marked done -- but a retry
    that re-raises here would just fail again, three times, and then be
    given up on.)"""
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


def _send_one_digest(components, account: str, acc, today: str,
                     llm_cost: str) -> bool:
    """One account's digest -- `trades` comes from `TradeJournal.
    trades_today()` — the journal's real accessor (there is no `journal.
    today()`). `triggers` counts today's "sentinel"-sourced rows in THIS
    account's own observation log: `Sentinel._check_strategy` logs exactly
    one there per rule trigger regardless of disposition, so this is a
    real count, not the brief's hardcoded placeholder. Per-strategy
    failures (e.g. a bad quote) log under the distinct "sentinel_error"
    source instead, so they can never inflate this count. `since_iso`
    is a UTC calendar-day boundary, matching `TradeJournal.trades_today`'s
    own day convention; `limit` is set high because `recent()`'s 200-row
    default would silently undercount on a very active day.

    `acc` is this account's own `app.AccountComponents` bundle
    (`.observations`/`.journal`/`.queue`) -- shadow-dual-active T7: reading
    `acc`'s own stores rather than the legacy `components.observations`/
    `.journal`/`.queue` aliases (which only ever point at paper's) is what
    makes shadow's digest count shadow's own activity instead of silently
    repeating paper's."""
    since_iso = datetime.now(UTC).date().isoformat()
    triggers = sum(1 for row in acc.observations.recent(
        since_iso=since_iso, limit=10_000) if row["source"] == "sentinel")
    subject, body = events.daily_digest(
        account=account, triggers=triggers, trades=acc.journal.trades_today(),
        pending=len(acc.queue.list()), llm_cost=llm_cost)
    # I4: `Notifier.send` is non-raising by contract (notify/base.py) and
    # reports failure by RETURNING False -- a dead SMTP server, an ntfy
    # topic that 404s. Stamping the watermark regardless made that failure
    # permanent: the day was marked sent, and no later tick could reach the
    # digest again. Write it only on a real delivery; an undelivered digest
    # stays pending and the next tick retries it (bounded, in practice, by
    # the day itself -- the watermark is per ET day).
    if not components.notifier.send(subject, body):
        print(f"[digest:{account}] not sent (notifier reported failure); "
              "will retry on the next tick", file=sys.stderr)
        return False
    components.app_state.set(_digest_date_key(account), today)
    return True


def _send_daily_digest(components) -> bool:
    """One digest per account (shadow-dual-active T7, spec §⑤: every event
    prefixed `[Paper]`/`[Shadow]` -- the digest used to be one email
    combining both accounts, before this task split it), each sent at most
    once per ET calendar day.

    The `digest_last_date:{account}` guard (see `_digest_date_key`) exists
    because `_maybe_run_daily`'s once-per-day gate (`state["last_daily"]`)
    is in-memory only: a `serve` restart after 16:05 forgets today's
    digest already went out and would otherwise re-send it on the next
    tick. Reflection doesn't have this problem — `Reflector.run_daily`
    (reflect.py) checks `reports.exists_ok(et_date)`, a persisted table row —
    and consolidation tracks its own turn marker the same way; this
    mirrors that pattern for the digest specifically, without touching
    `_maybe_run_daily` itself.

    `llm_cost` (the one process-wide, not-per-account, LLM spend line --
    see `_llm_cost_line`'s and `events.daily_digest`'s own docstrings) is
    computed ONCE, outside the per-account loop, and handed unchanged to
    both accounts' digests -- summing it twice would double-count nothing
    (it already reads the same total table both times), but computing it
    twice would be wasted work for an identical answer.

    Each account's send is isolated in its own try/except: a failure
    building or sending shadow's digest (a dead notifier, a broken store)
    must never prevent paper's from going out, or from being marked done
    for the day, and vice versa -- same isolation discipline as
    `_run_sentinel_pass`'s per-account loop and `run_daily_jobs`'s own.

    Returns whether EVERY account that still needed a digest today got one
    (I4): `run_daily_jobs` folds that into its own return, which is what
    lets `_maybe_run_daily` retry a night whose digest silently failed
    instead of marking the day done."""
    today = datetime.now(UTC).astimezone(ET).date().isoformat()
    llm_cost = _llm_cost_line(components)
    all_ok = True
    for account, acc in components.accounts.items():
        # setup-wizard T1: nothing to report for an account whose keys
        # aren't set -- its journal and review queue are empty by
        # construction, so the digest would be a nightly email saying
        # nothing happened on an account the user hasn't connected yet.
        # No watermark is stamped either: the day this account IS
        # configured, that evening's digest must still go out.
        if _is_unconfigured(acc):
            continue
        if _last_digest_date(components.app_state, account) == today:
            continue
        try:
            all_ok = _send_one_digest(components, account, acc, today,
                                      llm_cost) and all_ok
        except Exception as exc:  # noqa: BLE001 — one account's digest must
            # never take the other account's down.
            print(f"[digest:{account}] failed: {exc}", file=sys.stderr)
            all_ok = False
    return all_ok


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


def _run_account_daily(account: str, acc, settings, *, verbose: bool) -> bool:
    """One account's reflection -> consolidation pair (shadow-dual-active
    T4, spec §③). Order matches `run_daily_jobs`'s own docstring: reflection
    before consolidation, so the same night's consolidation pass can pick up
    reflection's memory_update conclusions. Each step keeps its own
    try/except, same as the pre-dual-active single-account version, so one
    broken task never silently prevents the other from running -- and,
    since this whole function is called once per account from a loop, a
    failure inside it (there shouldn't be one left uncaught, but belt and
    suspenders) must never be allowed to stop the OTHER account's nightly
    chain either; see run_daily_jobs's own per-account try/except.

    Returns whether both steps completed without an error (I4b) -- swallowed
    exceptions still get REPORTED upward as False, so a partial night is
    retried on a later tick rather than being marked done. A step that was
    deliberately skipped (setting off, no active strategies, no such
    component wired) is not a failure and keeps the return True."""
    # setup-wizard T1: an account with no Alpaca keys yet has nothing to
    # reflect on -- `Reflector.run_daily` reads `broker.get_positions()`,
    # which raises here, and a reflection over zero trades would burn a
    # real LLM call to conclude nothing either way. Returns True, not
    # False: an unfinished setup is a SKIP, not a failure, and returning
    # False would have `_maybe_run_daily` retry the whole night --
    # shadow's chain included -- three times before giving up, every
    # single night, until the user finishes setup.
    if _is_unconfigured(acc):
        if verbose:
            print(f"[daily:{account}] skipped (Alpaca keys not set)")
        return True

    ok = True
    reflector = acc.reflector
    if reflector is not None and settings.daily_reflection:
        if _account_has_active_strategy(acc.strategies):
            try:
                status = reflector.run_daily()
                if verbose:
                    print(f"[reflection:{account}] {status}")
            except Exception as exc:  # noqa: BLE001 — must not stop consolidation
                print(f"[reflection:{account}] failed: {exc}", file=sys.stderr)
                ok = False
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
            ok = False
    return ok


def _run_publish_step(components, *, verbose: bool) -> bool:
    """Step 4 of the nightly chain: POST today's ET digest (PAPER account
    only -- `components.broker`/`.journal`/`.reports`/`.queue` are the
    legacy aliases for `accounts["paper"]`, see app.py's `Components`
    docstring) to the public journal page, gated purely on both
    `publish_url` and `publish_token` being set. Off by default: an install
    that hasn't opted in never even builds the digest.

    Local imports (rather than a module-level import of publish.py) because
    publish.py itself imports `ET`/`ts_to_et_date` from this module --
    importing publish.py at scheduler.py's own module-load time would be a
    circular import; deferring it to call time (same as `_llm_cost_line`'s
    own local imports of llm/prices.py and web/format.py) avoids that
    entirely.

    Isolated in its own try/except by the caller (`run_daily_jobs`), same as
    the digest and each account's reflection/consolidation pair -- a broken
    publish endpoint must never take the rest of the night down, and the
    reverse: a broken reflection must never suppress tonight's publish.

    Returns whether the step succeeded (built AND sent) so `_maybe_run_daily`
    can retry a night whose publish silently failed instead of marking the
    day done, same I4b contract as every other step."""
    if not (components.settings.publish_url and components.settings.publish_token):
        if verbose:
            print("[publish] skipped (not configured)")
        return True
    from allpath_trade.publish import build_daily_digest, publish_digest

    digest = build_daily_digest(components, today_et_date())
    ok = publish_digest(components.settings.publish_url,
                        components.settings.publish_token, digest)
    if verbose:
        print("[publish] ok" if ok else "[publish] failed")
    return ok


def run_daily_jobs(components, verbose: bool = False) -> bool:
    """The after-close daily sequence, shared by `build_jobs` (`serve`) and
    `cli.py`'s `run` daemon so the two entry points can't drift out of sync
    again (docs/TODO.md's Phase 5 leftover: `run` used to skip the digest
    entirely and never gated consolidation on `daily_consolidation`).

    Order: digest -> {reflection -> consolidation} PER ACCOUNT -> publish
    (shadow-dual-active T4, spec §③, plus the journal-publisher step) --
    within one account, reflection still runs before consolidation (spec
    §①: reflection's memory_update conclusions need to land before
    consolidation runs, so the same night's pass can pick them up); each
    account's whole reflection+consolidation pair is further isolated in
    its own try/except in the loop below, so paper's nightly chain failing
    outright can never silently prevent shadow's from running, or the
    reverse.

    The digest fires unconditionally, once per account, BEFORE the
    reflection/consolidation loop -- shadow-dual-active T7 (carried from
    T4, which deliberately left the digest as one email covering both
    accounts pending this task's subject-prefixing work): `_send_daily_
    digest` now loops `components.accounts` itself and sends one
    `[Paper]`/`[Shadow]`-prefixed digest per account, each gated on its
    own `digest_last_date:{account}` watermark.

    The publish step runs LAST, once (paper only, not per account -- see
    `_run_publish_step`), after every account's reflection/consolidation
    pair -- the public journal's reflection_summary/reflection_body come
    straight from that same night's reflection report, so publish has to
    wait for it, and pending_proposals should reflect anything reflection
    itself queued tonight.

    Callers are expected to wrap this in their own once-per-day gate (see
    `_maybe_run_daily`) -- this function itself is state-free.

    Returns whether the whole night completed cleanly: False if the digest
    failed to send for any account, if any account's reflection or
    consolidation raised, or if publish was configured but failed. Every
    failure is still swallowed and printed (one step's bad night must not
    take the others down), but I4b needs the outcome REPORTED so
    `_maybe_run_daily` can leave the day open for a retry instead of
    stamping it done. The retry is cheap because each sub-step has its own
    persisted idempotency marker.

    `verbose=True` restores the success-path prints the headless `run`
    daemon had before this helper was extracted: `run` has no web UI, so
    its stdout is the operator's only window into whether the nightly
    chain did anything. `serve` keeps verbose=False -- its operator reads
    the Reports page, and the pre-refactor daily() never printed on
    success there either."""
    ok = True
    try:
        ok = _send_daily_digest(components)
    except Exception as exc:  # noqa: BLE001 — a failed digest must not stop the rest
        print(f"[digest] failed: {exc}")
        ok = False

    for account, acc in components.accounts.items():
        try:
            ok = _run_account_daily(account, acc, components.settings,
                                    verbose=verbose) and ok
        except Exception as exc:  # noqa: BLE001 — one account's nightly chain
            # must never take the other account's down (belt and
            # suspenders: _run_account_daily already catches its own two
            # steps individually, so this is not expected to be reachable).
            print(f"[daily:{account}] failed: {exc}", file=sys.stderr)
            ok = False

    try:
        ok = _run_publish_step(components, verbose=verbose) and ok
    except Exception as exc:  # noqa: BLE001 — a failed publish must not stop the rest
        print(f"[publish] failed: {exc}", file=sys.stderr)
        ok = False
    return ok


def build_jobs(scheduler, holder) -> None:
    """Attach the sentinel and the after-close daily jobs to a scheduler
    owned by someone else (the `serve` process).

    Same job bodies as `run_daemon` (see `_run_sentinel_pass` /
    `_maybe_run_daily`), minus the terminal progress lines — the server
    process has no one to print them to.

    I8: these are TWO independent interval jobs, not one tick that calls
    the nightly chain at the end of itself. APScheduler runs a job with
    `max_instances=1` (the default) strictly serially, so while the old
    combined job sat inside a reflection -- an LLM session that can
    legitimately run for many minutes, times however many accounts -- every
    sentinel tick behind it was swallowed: no fill refresh, no strategy
    evaluation, no heartbeat, for either account, until the night finished.
    Monitoring and the nightly chain are different jobs with different
    cadences and different failure modes; splitting them lets the sentinel
    keep its interval while reflection takes as long as it takes.

    The daily job carries `max_instances=1` + `coalesce=True` explicitly
    rather than relying on defaults, because both matter here and are worth
    stating: a chain that overruns its own interval must queue exactly one
    successor, not stack up, and a chain that overran by an hour must run
    ONCE when it finishes, not replay every tick it missed.
    """
    state = new_daily_state()

    def job() -> None:
        components = holder.get()
        _run_sentinel_pass(components.accounts, app_state=components.app_state)

    def daily() -> None:
        components = holder.get()
        _maybe_run_daily(lambda: run_daily_jobs(components), state)

    scheduler.add_job(job, "interval",
                      minutes=holder.settings().sentinel_interval_minutes,
                      next_run_time=datetime.now(UTC), id=SENTINEL_JOB_ID)
    scheduler.add_job(daily, "interval", minutes=DAILY_CHECK_INTERVAL_MINUTES,
                      next_run_time=datetime.now(UTC), id=DAILY_JOB_ID,
                      max_instances=1, coalesce=True)


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
