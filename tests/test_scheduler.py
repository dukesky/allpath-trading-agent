import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from allpath_trade.scheduler import (
    SENTINEL_JOB_ID,
    SENTINEL_MARKET_OPEN_KEY,
    build_jobs,
    is_market_hours,
    reschedule_sentinel_job,
    run_daemon,
    run_daily_jobs,
)
from allpath_trade.store.db import connect
from allpath_trade.store.llm_usage import LLMUsage
from tests.test_sentinel import make, strategy_yaml

# 2026-07-29 is a Wednesday. 15:00 UTC = 11:00 ET (EDT, UTC-4).


def test_open_wednesday_11am_et():
    assert is_market_hours(datetime(2026, 7, 29, 15, 0, tzinfo=UTC))


def test_closed_before_open():
    # 13:00 UTC = 09:00 ET < 09:30
    assert not is_market_hours(datetime(2026, 7, 29, 13, 0, tzinfo=UTC))


def test_closed_after_close():
    # 20:30 UTC = 16:30 ET
    assert not is_market_hours(datetime(2026, 7, 29, 20, 30, tzinfo=UTC))


def test_closed_weekend():
    # 2026-08-01 is a Saturday
    assert not is_market_hours(datetime(2026, 8, 1, 15, 0, tzinfo=UTC))


def test_boundary_open_and_close():
    # 13:30 UTC = 09:30 ET exactly -> open; 20:00 UTC = 16:00 ET exactly -> closed
    assert is_market_hours(datetime(2026, 7, 29, 13, 30, tzinfo=UTC))
    assert not is_market_hours(datetime(2026, 7, 29, 20, 0, tzinfo=UTC))


class ImmediateScheduler:
    """Runs the job on a non-main thread, like APScheduler's worker pool,
    so we exercise real sqlite thread-affinity behavior."""

    def __init__(self):
        self.fn = None

    def add_job(self, fn, *args, **kwargs):
        self.fn = fn

    def start(self):
        t = threading.Thread(target=self.fn)
        t.start()
        t.join()


def test_run_daemon_runs_job_on_worker_thread_against_real_store(
        tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: True)
    s, _store, _ex, _q, _n = make(tmp_path, strategy_yaml(condition="price < 100"))
    accounts = {"paper": SimpleNamespace(
        sentinel=s, journal=SimpleNamespace(unfilled_recent=lambda hours=48: []),
        broker=SimpleNamespace())}

    run_daemon(lambda: accounts, 5, scheduler_cls=ImmediateScheduler)

    out = capsys.readouterr().out
    assert "checked=1" in out
    assert "errors=0" in out


def test_run_daemon_skips_sentinel_when_market_closed(monkeypatch):
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: False)
    accounts = {"paper": SimpleNamespace(
        sentinel=RaisingSentinel(),
        journal=SimpleNamespace(unfilled_recent=lambda hours=48: []),
        broker=SimpleNamespace())}

    run_daemon(lambda: accounts, 5, scheduler_cls=ImmediateScheduler)  # must not raise


def test_run_daemon_fires_daily_job_after_close(monkeypatch):
    import allpath_trade.scheduler as sched

    calls = []

    class OneShotScheduler:
        def add_job(self, fn, *a, **k):
            self.fn = fn

        def start(self):
            self.fn()

    monkeypatch.setattr(sched, "is_market_hours", lambda now=None: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    sched.run_daemon(dict, 60, scheduler_cls=OneShotScheduler,
                     daily_job=lambda: calls.append(1))
    assert calls == [1]


# -- build_jobs: same job body as run_daemon, wired to a live ComponentHolder
# instead of a factory, and silent (the server process shouldn't spam stdout
# on every sentinel/consolidation pass). --


class FakeScheduler:
    """Records the job APScheduler would have scheduled, without running it."""

    def __init__(self):
        self.job = None
        self.trigger = None
        self.kwargs = None

    def add_job(self, fn, trigger, **kwargs):
        self.job = fn
        self.trigger = trigger
        self.kwargs = kwargs


class FakeSentinel:
    def __init__(self):
        self.calls = 0

    def run_once(self):
        self.calls += 1
        return SimpleNamespace(strategies_checked=1, outcomes=[], errors=[])


class FakeConsolidator:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def run_daily(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return "ok"


class FakeReflector:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def run_daily(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return "ok"


class FakeHolder:
    def __init__(self, components):
        self._components = components

    def get(self):
        return self._components

    def settings(self):
        return self._components.settings


class FakeJournal:
    def __init__(self, trades=0, unfilled=None):
        self._trades = trades
        self._unfilled = unfilled if unfilled is not None else []
        self.refreshed = []

    def trades_today(self):
        return self._trades

    def unfilled_recent(self, hours=48):
        return self._unfilled

    def refresh_fill(self, trade_id, order):
        self.refreshed.append((trade_id, order))


class FakeSchedulerBroker:
    """Minimal Broker stand-in for the fill-refresh sweep (deliverable 2) --
    build_jobs/run_daemon only need something to pass through to
    refresh_pending_fills; the sweep's own behavior is covered by
    test_execution.py, so this just records get_order calls."""

    def __init__(self, orders=None):
        self._orders = orders or {}
        self.get_order_calls = []

    def get_order(self, order_id):
        self.get_order_calls.append(order_id)
        return self._orders[order_id]


class FakeQueue:
    def __init__(self, pending=None):
        self._pending = pending if pending is not None else []

    def list(self, status="pending"):
        return self._pending


class FakeObservations:
    def __init__(self, rows=None):
        self._rows = rows if rows is not None else []

    def recent(self, since_iso=None, limit=200):
        return self._rows


class FakeStrategies:
    """Stands in for StrategyStore's `.load_all()` -- the reflection cost
    gate (shadow-dual-active T4, spec §③) calls this and skips the whole
    reflection pass for an account with zero active strategies. `active`
    defaults True so every existing test not specifically exercising the
    gate itself sees a reflector run exactly as it did before this feature
    existed."""

    def __init__(self, active: bool = True):
        self._active = active

    def load_all(self, status=None, errors=None):
        return ["strategy"] if self._active else []


class RaisingSentinel:
    """A sentinel whose `run_once` must never be called -- used to prove a
    sentinel pass was correctly skipped (market closed) rather than merely
    not reported."""

    def run_once(self):
        raise AssertionError("sentinel.run_once() must not be called here")


class FakeAppState:
    """Stands in for allpath_trade.store.app_state.AppState -- a plain dict
    is enough to prove build_jobs/run_daemon call .set() with the right key,
    without touching a real sqlite connection (that's AppState's own test,
    tests/test_app_state.py)."""

    def __init__(self):
        self.values: dict[str, str] = {}

    def set(self, key, value):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)


class DigestNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))
        return True


def _account_bundle(sentinel=None, journal=None, broker=None, queue=None,
                    observations=None, strategies=None, reflector=None,
                    consolidator=None):
    return SimpleNamespace(
        sentinel=sentinel, reflector=reflector, consolidator=consolidator,
        journal=journal if journal is not None else FakeJournal(),
        broker=broker if broker is not None else FakeSchedulerBroker(),
        queue=queue if queue is not None else FakeQueue(),
        observations=observations if observations is not None else FakeObservations(),
        strategies=strategies if strategies is not None else FakeStrategies())


def _components(sentinel, consolidator=None, daily_consolidation=True, interval=5,
                journal=None, queue=None, notifier=None, observations=None,
                app_state=None, reflector=None, daily_reflection=True, broker=None,
                llm_usage=None, strategies=None, accounts=None):
    # shadow-dual-active T4: every field below (sentinel/journal/broker/
    # queue/observations/reflector/consolidator/strategies) is ALSO
    # reachable as the flat legacy attribute (mirrors app.py's
    # `Components` legacy alias surface, pointing at the "paper" bundle) --
    # `_send_daily_digest` still reads the flat attributes directly, while
    # `_run_sentinel_pass`/`run_daily_jobs` read `.accounts`. Both views are
    # literally the SAME objects, so a test that asserts against e.g. the
    # `journal`/`sentinel` it passed in keeps working unchanged whichever
    # path the production code reads them through.
    journal = journal if journal is not None else FakeJournal()
    broker = broker if broker is not None else FakeSchedulerBroker()
    queue = queue if queue is not None else FakeQueue()
    observations = observations if observations is not None else FakeObservations()
    strategies = strategies if strategies is not None else FakeStrategies()
    bundle = _account_bundle(sentinel=sentinel, journal=journal, broker=broker,
                             queue=queue, observations=observations,
                             strategies=strategies, reflector=reflector,
                             consolidator=consolidator)
    return SimpleNamespace(
        sentinel=sentinel,
        consolidator=consolidator,
        reflector=reflector,
        journal=journal,
        broker=broker,
        queue=queue,
        notifier=notifier if notifier is not None else DigestNotifier(),
        observations=observations,
        strategies=strategies,
        app_state=app_state if app_state is not None else FakeAppState(),
        # `_llm_cost_line`'s only touch point: `.summary_for_day()` -- empty
        # by default, matching "no LLM usage recorded" (no cost line in the
        # digest), same as every test in this file before this feature
        # existed.
        llm_usage=llm_usage if llm_usage is not None
        else SimpleNamespace(summary_for_day=lambda date_utc=None: []),
        accounts=accounts if accounts is not None else {"paper": bundle},
        settings=SimpleNamespace(daily_consolidation=daily_consolidation,
                                 daily_reflection=daily_reflection,
                                 sentinel_interval_minutes=interval),
    )


def test_build_jobs_registers_interval_job_from_holder_settings():
    scheduler = FakeScheduler()
    holder = FakeHolder(_components(sentinel=FakeSentinel(), interval=17))

    build_jobs(scheduler, holder)

    assert scheduler.trigger == "interval"
    assert scheduler.kwargs["minutes"] == 17
    assert "next_run_time" in scheduler.kwargs


def test_build_jobs_runs_sentinel_when_market_open(monkeypatch):
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: True)
    sentinel = FakeSentinel()
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(sentinel=sentinel)))

    scheduler.job()

    assert sentinel.calls == 1


def test_build_jobs_skips_sentinel_when_market_closed(monkeypatch):
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: False)
    sentinel = FakeSentinel()
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(sentinel=sentinel)))

    scheduler.job()

    assert sentinel.calls == 0


def test_build_jobs_records_heartbeat_when_market_open(monkeypatch):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: True)
    monkeypatch.setattr(sched, "datetime", SimpleNamespace(
        now=lambda tz=None: datetime(2026, 8, 9, 15, 0, tzinfo=UTC)))
    app_state = FakeAppState()
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(sentinel=FakeSentinel(),
                                                 app_state=app_state)))

    scheduler.job()

    assert app_state.get(f"{sched.SENTINEL_HEARTBEAT_KEY}:paper") == "2026-08-09T15:00:00+00:00"


def test_build_jobs_records_heartbeat_even_when_market_closed(monkeypatch):
    # This is the whole point of the feature: the heartbeat proves the
    # *scheduler* is alive, not the market -- it must fire on every tick,
    # not only the ticks where is_market_hours() happens to be True. This
    # suite already burned once on an unpatched clock (_is_after_close), so
    # both is_market_hours and the clock are patched explicitly here rather
    # than trusted to the real wall clock.
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "datetime", SimpleNamespace(
        now=lambda tz=None: datetime(2026, 8, 9, 3, 0, tzinfo=UTC)))
    sentinel = FakeSentinel()
    app_state = FakeAppState()
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(sentinel=sentinel, app_state=app_state)))

    scheduler.job()

    assert sentinel.calls == 0  # market closed: sentinel itself did not run
    assert app_state.get(f"{sched.SENTINEL_HEARTBEAT_KEY}:paper") == "2026-08-09T03:00:00+00:00"


def test_run_daemon_records_heartbeat_even_when_market_closed(monkeypatch):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "datetime", SimpleNamespace(
        now=lambda tz=None: datetime(2026, 8, 9, 3, 0, tzinfo=UTC)))
    app_state = FakeAppState()
    accounts = {"paper": SimpleNamespace(
        sentinel=RaisingSentinel(),
        journal=SimpleNamespace(unfilled_recent=lambda hours=48: []),
        broker=SimpleNamespace())}

    run_daemon(lambda: accounts, 5, scheduler_cls=ImmediateScheduler, app_state=app_state)

    assert app_state.get(f"{sched.SENTINEL_HEARTBEAT_KEY}:paper") == "2026-08-09T03:00:00+00:00"


def test_build_jobs_runs_daily_consolidation_once_per_day_after_close(monkeypatch):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    consolidator = FakeConsolidator()
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(
        _components(sentinel=FakeSentinel(), consolidator=consolidator)))

    scheduler.job()
    scheduler.job()

    assert consolidator.calls == 1


def test_build_jobs_skips_consolidation_when_setting_disabled(monkeypatch):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    consolidator = FakeConsolidator()
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(
        sentinel=FakeSentinel(), consolidator=consolidator, daily_consolidation=False)))

    scheduler.job()

    assert consolidator.calls == 0


def test_build_jobs_swallows_daily_consolidation_failure(monkeypatch, capsys):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    consolidator = FakeConsolidator(fail=True)
    notifier = DigestNotifier()
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(
        _components(sentinel=FakeSentinel(), consolidator=consolidator,
                    notifier=notifier)))

    scheduler.job()  # must not raise

    assert consolidator.calls == 1
    assert "failed" in capsys.readouterr().out
    # The digest is the user's daily signal that the system is alive — a
    # broken consolidator must not also cost them that, today or on any
    # future day (it doesn't poison the once-per-day gate either).
    assert len(notifier.sent) == 1
    scheduler.job()
    assert len(notifier.sent) == 1  # still once-per-day, not retried


def test_build_jobs_prints_nothing_on_a_normal_sentinel_pass(monkeypatch, capsys):
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: True)
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(sentinel=FakeSentinel())))

    scheduler.job()

    assert capsys.readouterr().out == ""


# -- build_jobs: daily digest email --


def test_build_jobs_sends_daily_digest_once_per_day_after_close(monkeypatch):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    notifier = DigestNotifier()
    components = _components(
        sentinel=FakeSentinel(), notifier=notifier,
        journal=FakeJournal(trades=3), queue=FakeQueue(pending=[1, 2]),
        observations=FakeObservations(
            rows=[{"source": "sentinel"}, {"source": "sentinel"}, {"source": "chat"}]))
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(components))

    scheduler.job()
    scheduler.job()  # second tick same day: must not send again

    assert len(notifier.sent) == 1
    subject, body = notifier.sent[0]
    assert subject == "[Paper] [AllPath] Daily summary"
    # 2 sentinel-sourced observations, 3 trades, 2 pending reviews
    assert "2 rule trigger(s)" in body
    assert "3 trade(s)" in body
    assert "2 item(s) still waiting" in body
    assert "http" not in body.lower() and "<" not in body


def test_build_jobs_daily_digest_mentions_llm_cost_when_usage_recorded(monkeypatch, tmp_path):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    notifier = DigestNotifier()
    llm_usage = LLMUsage(connect(tmp_path / "t.db"))
    llm_usage.record(tier="chat", model="claude-sonnet-5", input_tokens=1_000_000,
                     output_tokens=1_000_000, purpose="chat")
    components = _components(sentinel=FakeSentinel(), notifier=notifier,
                             llm_usage=llm_usage)
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(components))

    scheduler.job()

    [(_subject, body)] = notifier.sent
    assert "Estimated LLM cost today (all accounts): $18.00" in body


def test_build_jobs_daily_digest_omits_cost_line_when_no_usage(monkeypatch, tmp_path):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    notifier = DigestNotifier()
    llm_usage = LLMUsage(connect(tmp_path / "t.db"))
    components = _components(sentinel=FakeSentinel(), notifier=notifier,
                             llm_usage=llm_usage)
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(components))

    scheduler.job()

    [(_subject, body)] = notifier.sent
    assert "LLM cost" not in body


def test_build_jobs_daily_digest_cost_line_excludes_yesterdays_utc_usage(monkeypatch, tmp_path):
    # Important 2: the cost line is a UTC-calendar-day cut, not a rolling
    # 24h window -- a row from late yesterday (UTC) must not bleed into
    # "today"'s figure just because it's still within the last 24h.
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    notifier = DigestNotifier()
    conn = connect(tmp_path / "t.db")
    llm_usage = LLMUsage(conn)
    today = datetime.now(UTC).date().isoformat()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    # Late yesterday: within the last 24h from "now", but not today's
    # calendar day -- must be excluded.
    conn.execute(
        "INSERT INTO llm_usage (ts, tier, model, input_tokens, output_tokens, purpose)"
        " VALUES (?, 'chat', 'claude-sonnet-5', 1000000, 0, 'chat')",
        (f"{yesterday}T23:00:00+00:00",))
    conn.commit()
    components = _components(sentinel=FakeSentinel(), notifier=notifier, llm_usage=llm_usage)
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(components))

    scheduler.job()

    [(_subject, body)] = notifier.sent
    assert "LLM cost" not in body  # yesterday's usage must not count as "today"
    assert today  # sanity: today's date was computable in this environment


def test_build_jobs_daily_digest_survives_a_broken_usage_store(monkeypatch, capsys):
    # Important 3: a usage-store read failure must not take down the whole
    # digest -- it runs before notifier.send inside _send_daily_digest,
    # which already marks digest_last_date done regardless, so an
    # unswallowed exception here would silently cancel the digest for the
    # entire day (trigger/trade/pending counts included).
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    notifier = DigestNotifier()

    class BrokenLLMUsage:
        def summary_for_day(self, date_utc=None):
            raise RuntimeError("usage store is on fire")

    components = _components(sentinel=FakeSentinel(), notifier=notifier,
                             journal=FakeJournal(trades=3), queue=FakeQueue(pending=[1]),
                             llm_usage=BrokenLLMUsage())
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(components))

    scheduler.job()  # must not raise

    assert len(notifier.sent) == 1  # the digest itself still went out
    [(_subject, body)] = notifier.sent
    assert "LLM cost" not in body
    assert "3 trade(s)" in body
    assert "usage store is on fire" in capsys.readouterr().err


def test_build_jobs_digest_is_not_resent_after_a_simulated_restart(monkeypatch):
    # I3: _maybe_run_daily's once-per-day gate (state["last_daily"]) is
    # in-memory only and owned by build_jobs's own closure -- a `serve`
    # restart after 16:05 gets a brand-new `state` dict, so without a
    # persisted marker the digest would go out a second time for the same
    # ET day. Simulated here by calling build_jobs TWICE (a fresh closure
    # each time, exactly like a fresh process) against the SAME app_state,
    # the way a real sqlite-backed AppState would survive a restart.
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    notifier = DigestNotifier()
    shared_app_state = FakeAppState()
    components = _components(sentinel=FakeSentinel(), notifier=notifier,
                             app_state=shared_app_state)

    scheduler1 = FakeScheduler()
    build_jobs(scheduler1, FakeHolder(components))
    scheduler1.job()  # "process 1": sends the digest

    scheduler2 = FakeScheduler()
    build_jobs(scheduler2, FakeHolder(components))  # "restart": fresh in-memory state
    scheduler2.job()  # same ET day: must not re-send

    assert len(notifier.sent) == 1


def test_build_jobs_digest_counts_only_sentinel_observations(monkeypatch):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    notifier = DigestNotifier()
    components = _components(
        sentinel=FakeSentinel(), notifier=notifier,
        observations=FakeObservations(
            rows=[{"source": "chat"}, {"source": "consolidation"}]))
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(components))

    scheduler.job()

    _subject, body = notifier.sent[0]
    assert "0 rule trigger(s)" in body


def test_build_jobs_digest_excludes_sentinel_errors(monkeypatch):
    # A per-strategy failure (bad quote, etc.) logs under "sentinel_error",
    # not "sentinel" — see Sentinel.run_once. If the digest ever counted
    # those too, a day with only errors and zero real triggers would
    # falsely tell the user a rule fired.
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    notifier = DigestNotifier()
    components = _components(
        sentinel=FakeSentinel(), notifier=notifier,
        observations=FakeObservations(
            rows=[{"source": "sentinel_error"}, {"source": "sentinel_error"},
                  {"source": "sentinel"}]))
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(components))

    scheduler.job()

    _subject, body = notifier.sent[0]
    assert "1 rule trigger(s)" in body


def test_build_jobs_sends_digest_even_when_consolidation_disabled(monkeypatch):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    notifier = DigestNotifier()
    consolidator = FakeConsolidator()
    components = _components(
        sentinel=FakeSentinel(), notifier=notifier, consolidator=consolidator,
        daily_consolidation=False)
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(components))

    scheduler.job()

    assert consolidator.calls == 0        # consolidation still respects its own flag
    assert len(notifier.sent) == 1        # digest is a separate concern


def test_build_jobs_registers_the_interval_job_under_a_stable_id():
    # reschedule_sentinel_job needs a stable id to target -- without one,
    # a settings-page interval change can only add a second interval job
    # alongside the original, not replace it.
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(sentinel=FakeSentinel())))
    assert scheduler.kwargs["id"] == SENTINEL_JOB_ID


def test_reschedule_sentinel_job_updates_the_running_jobs_interval():
    # Finding 5: changing the interval on the settings page must move the
    # already-running job's cadence, not just what a *future* build_jobs
    # call would register.
    class RecordingScheduler:
        def __init__(self):
            self.rescheduled = None

        def reschedule_job(self, job_id, trigger=None, **kwargs):
            self.rescheduled = (job_id, trigger, kwargs)

    scheduler = RecordingScheduler()
    reschedule_sentinel_job(scheduler, 15)
    assert scheduler.rescheduled == (SENTINEL_JOB_ID, "interval", {"minutes": 15})


def test_reschedule_sentinel_job_moves_a_real_apschedulers_cadence():
    # F6: the RecordingScheduler-based test above (and its web-route
    # counterpart in test_web_settings.py) only proves we call the
    # scheduler's API in a particular shape -- not that a real APScheduler
    # accepts `id=` alongside `next_run_time` on add_job, or that
    # reschedule_job actually finds and updates that job. Exercise a real,
    # un-started BackgroundScheduler end to end instead of a fake.
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: None, "interval", minutes=60,
                      next_run_time=datetime.now(UTC), id=SENTINEL_JOB_ID)

    reschedule_sentinel_job(scheduler, 15)

    job = scheduler.get_job(SENTINEL_JOB_ID)
    assert job is not None
    assert job.trigger.interval.total_seconds() == 15 * 60


def test_build_jobs_no_digest_before_close(monkeypatch):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: True)
    # Unlike its siblings above, this test's whole point is the gate being
    # *closed* — it must not depend on the real wall clock, or it goes
    # intermittently red whenever the suite runs on a weekday evening.
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: False)
    notifier = DigestNotifier()
    components = _components(sentinel=FakeSentinel(), notifier=notifier)
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(components))

    scheduler.job()

    assert notifier.sent == []


def test_build_jobs_records_market_open_true_alongside_heartbeat_when_open(monkeypatch):
    # Finding 2 (final review, phase5.5-ui-polish): the dashboard needs to
    # tell a real evaluation apart from a tick where the market was simply
    # closed -- this is the flag it reads to do that.
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: True)
    app_state = FakeAppState()
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(sentinel=FakeSentinel(),
                                                 app_state=app_state)))

    scheduler.job()

    assert app_state.get(SENTINEL_MARKET_OPEN_KEY) == "true"


def test_build_jobs_records_market_open_false_alongside_heartbeat_when_closed(monkeypatch):
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: False)
    sentinel = FakeSentinel()
    app_state = FakeAppState()
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(sentinel=sentinel, app_state=app_state)))

    scheduler.job()

    assert sentinel.calls == 0  # market closed: sentinel itself did not run
    assert app_state.get(SENTINEL_MARKET_OPEN_KEY) == "false"


def test_run_daemon_records_market_open_flag_too(monkeypatch):
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: False)
    app_state = FakeAppState()
    accounts = {"paper": SimpleNamespace(
        sentinel=RaisingSentinel(),
        journal=SimpleNamespace(unfilled_recent=lambda hours=48: []),
        broker=SimpleNamespace())}

    run_daemon(lambda: accounts, 5, scheduler_cls=ImmediateScheduler, app_state=app_state)

    assert app_state.get(SENTINEL_MARKET_OPEN_KEY) == "false"


def test_build_jobs_sentinel_runs_even_if_heartbeat_write_fails(monkeypatch, capsys):
    """Finding 1: a failed heartbeat write must not skip the sentinel pass."""
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: True)
    sentinel = FakeSentinel()
    scheduler = FakeScheduler()

    class FailingAppState:
        def set(self, key, value):
            raise RuntimeError("database is locked")

        def get(self, key):
            return None

    components = _components(sentinel=sentinel, app_state=FailingAppState())
    build_jobs(scheduler, FakeHolder(components))

    scheduler.job()

    assert sentinel.calls == 1  # sentinel ran despite the heartbeat failure
    stderr = capsys.readouterr().err
    assert "heartbeat" in stderr
    assert "failed" in stderr


# -- build_jobs: daily reflection (Task 5) --


# -- build_jobs / run_daemon: fill-refresh sweep (fill-honesty round) --


def test_build_jobs_refreshes_pending_fills_even_when_market_closed(monkeypatch):
    # The whole point: an order queued outside market hours fills at the
    # next open, and the first pass after that open must pick it up. It
    # must also run on a closed-market tick so nothing waits an extra
    # interval once the market does open.
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    order = SimpleNamespace(status=SimpleNamespace(value="filled"))
    journal = FakeJournal(unfilled=[{"id": 1, "broker_order_id": "o1"}])
    broker = FakeSchedulerBroker(orders={"o1": order})
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(
        sentinel=FakeSentinel(), journal=journal, broker=broker)))

    scheduler.job()

    assert broker.get_order_calls == ["o1"]
    assert journal.refreshed == [(1, order)]


def test_build_jobs_refreshes_pending_fills_when_market_open(monkeypatch):
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: True)
    order = SimpleNamespace(status=SimpleNamespace(value="filled"))
    journal = FakeJournal(unfilled=[{"id": 1, "broker_order_id": "o1"}])
    broker = FakeSchedulerBroker(orders={"o1": order})
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(
        sentinel=FakeSentinel(), journal=journal, broker=broker)))

    scheduler.job()

    assert journal.refreshed == [(1, order)]


def test_build_jobs_fill_refresh_failure_does_not_stop_the_sentinel_pass(
        monkeypatch, capsys):
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: True)

    class RaisingJournal:
        def unfilled_recent(self, hours=48):
            raise RuntimeError("db locked")

    sentinel = FakeSentinel()
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(
        sentinel=sentinel, journal=RaisingJournal(), broker=FakeSchedulerBroker())))

    scheduler.job()  # must not raise

    assert sentinel.calls == 1
    stderr = capsys.readouterr().err
    assert "fill-refresh" in stderr and "failed" in stderr


def test_run_daemon_refreshes_pending_fills(monkeypatch):
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: False)
    order = SimpleNamespace(status=SimpleNamespace(value="filled"))
    journal = FakeJournal(unfilled=[{"id": 7, "broker_order_id": "o7"}])
    broker = FakeSchedulerBroker(orders={"o7": order})
    accounts = {"paper": SimpleNamespace(
        sentinel=RaisingSentinel(), journal=journal, broker=broker)}

    run_daemon(lambda: accounts, 5, scheduler_cls=ImmediateScheduler)

    assert journal.refreshed == [(7, order)]


def test_build_jobs_daily_steps_run_in_digest_reflection_consolidation_order(
        monkeypatch):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    order: list[str] = []

    class OrderNotifier(DigestNotifier):
        def send(self, subject, body):
            order.append("digest")
            return super().send(subject, body)

    class OrderReflector(FakeReflector):
        def run_daily(self):
            order.append("reflection")
            return super().run_daily()

    class OrderConsolidator(FakeConsolidator):
        def run_daily(self):
            order.append("consolidation")
            return super().run_daily()

    components = _components(
        sentinel=FakeSentinel(), consolidator=OrderConsolidator(),
        reflector=OrderReflector(), notifier=OrderNotifier())
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(components))

    scheduler.job()

    assert order == ["digest", "reflection", "consolidation"]


def test_build_jobs_runs_reflection_once_per_day_after_close(monkeypatch):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    reflector = FakeReflector()
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(
        _components(sentinel=FakeSentinel(), reflector=reflector)))

    scheduler.job()
    scheduler.job()  # second tick, same day: must not run again

    assert reflector.calls == 1


def test_build_jobs_skips_reflection_when_setting_disabled(monkeypatch):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    reflector = FakeReflector()
    consolidator = FakeConsolidator()
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(
        sentinel=FakeSentinel(), reflector=reflector, consolidator=consolidator,
        daily_reflection=False)))

    scheduler.job()

    assert reflector.calls == 0
    assert consolidator.calls == 1  # not skipped just because reflection is off


def test_build_jobs_skips_reflection_cleanly_when_no_reflector_configured(
        monkeypatch):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    consolidator = FakeConsolidator()
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(
        sentinel=FakeSentinel(), consolidator=consolidator, reflector=None)))

    scheduler.job()  # must not raise

    assert consolidator.calls == 1


def test_build_jobs_reflection_failure_does_not_stop_consolidation(
        monkeypatch, capsys):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    consolidator = FakeConsolidator()
    reflector = FakeReflector(fail=True)
    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(
        sentinel=FakeSentinel(), consolidator=consolidator, reflector=reflector)))

    scheduler.job()  # must not raise

    assert reflector.calls == 1
    assert consolidator.calls == 1
    assert "[reflection:paper] failed" in capsys.readouterr().err


def test_build_jobs_digest_failure_does_not_stop_reflection(monkeypatch, capsys):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "_is_after_close", lambda now=None: True)
    reflector = FakeReflector()

    class RaisingNotifier:
        def send(self, subject, body):
            raise RuntimeError("boom-notify")

    scheduler = FakeScheduler()
    build_jobs(scheduler, FakeHolder(_components(
        sentinel=FakeSentinel(), reflector=reflector, notifier=RaisingNotifier())))

    scheduler.job()  # must not raise

    assert reflector.calls == 1
    # shadow-dual-active T7: _send_daily_digest now isolates each account's
    # send in its own try/except (same "[xxx:{account}] failed" shape as
    # the heartbeat/sentinel/reflection/consolidation per-account failure
    # prints elsewhere in this module) and reports to stderr, not stdout.
    assert "[digest:paper] failed" in capsys.readouterr().err


def _chatty_daily_components():
    # Reflector/consolidator stand-ins whose run_daily returns a status line,
    # like the real ones do -- verbose mode prints exactly that line.
    reflector = SimpleNamespace(run_daily=lambda: "report stored for 2026-08-11")
    consolidator = SimpleNamespace(run_daily=lambda: "consolidated 3 events")
    return _components(sentinel=FakeSentinel(), consolidator=consolidator,
                       reflector=reflector)


def test_run_daily_jobs_verbose_prints_success_lines(capsys):
    # The headless `run` daemon has no web UI -- its stdout is the operator's
    # only window into whether the nightly chain did anything, so verbose=True
    # must restore the success-path prints the pre-extraction daily() had.
    run_daily_jobs(_chatty_daily_components(), verbose=True)
    out = capsys.readouterr().out
    assert "[reflection:paper] report stored for 2026-08-11" in out
    assert "[memory:paper] consolidated 3 events" in out


def test_run_daily_jobs_default_is_quiet_on_success(capsys):
    run_daily_jobs(_chatty_daily_components())
    out = capsys.readouterr().out
    assert "[reflection:paper]" not in out
    assert "[memory:paper]" not in out


# -- shadow-dual-active T4: dual sentinel isolation + per-account nightly
# reflection gate --


def test_run_sentinel_pass_isolates_a_raising_account_from_the_other(monkeypatch, capsys):
    # The whole point of dual-active: paper's sentinel raising must never
    # stop shadow's pass from running, and vice versa.
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: True)
    from allpath_trade.scheduler import _run_sentinel_pass

    class RaisingPaperSentinel:
        def run_once(self):
            raise RuntimeError("paper is on fire")

    shadow_sentinel = FakeSentinel()
    accounts = {
        "paper": _account_bundle(sentinel=RaisingPaperSentinel()),
        "shadow": _account_bundle(sentinel=shadow_sentinel),
    }

    _run_sentinel_pass(accounts)  # must not raise

    assert shadow_sentinel.calls == 1  # shadow ran despite paper's failure
    stderr = capsys.readouterr().err
    assert "[sentinel] paper failed" in stderr
    assert "paper is on fire" in stderr


def test_run_sentinel_pass_writes_separate_per_account_heartbeat_keys(monkeypatch):
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: True)
    import allpath_trade.scheduler as sched

    app_state = FakeAppState()
    accounts = {
        "paper": _account_bundle(sentinel=FakeSentinel()),
        "shadow": _account_bundle(sentinel=FakeSentinel()),
    }

    sched._run_sentinel_pass(accounts, app_state=app_state)

    paper_key = app_state.get(f"{sched.SENTINEL_HEARTBEAT_KEY}:paper")
    shadow_key = app_state.get(f"{sched.SENTINEL_HEARTBEAT_KEY}:shadow")
    assert paper_key is not None
    assert shadow_key is not None
    # shadow-dual-active T7: the legacy un-suffixed key is no longer
    # written at all -- the dashboard reads the per-account key for
    # whichever account is being viewed (T5), so nothing reads this bare
    # key any more (see scheduler.py's `_run_sentinel_pass` docstring).
    assert app_state.get(sched.SENTINEL_HEARTBEAT_KEY) is None


def test_run_sentinel_pass_market_open_flag_is_a_single_shared_key(monkeypatch):
    # Market-open-ness is one global fact, not duplicated per account.
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: True)
    from allpath_trade.scheduler import SENTINEL_MARKET_OPEN_KEY, _run_sentinel_pass

    app_state = FakeAppState()
    accounts = {
        "paper": _account_bundle(sentinel=FakeSentinel()),
        "shadow": _account_bundle(sentinel=FakeSentinel()),
    }

    _run_sentinel_pass(accounts, app_state=app_state)

    assert app_state.get(SENTINEL_MARKET_OPEN_KEY) == "true"
    assert f"{SENTINEL_MARKET_OPEN_KEY}:shadow" not in app_state.values


def test_run_daily_jobs_reflection_gate_skips_shadow_with_no_active_strategies(capsys):
    # spec §③: reflection is gated on >=1 ACTIVE strategy -- an empty
    # shadow ledger the user hasn't written anything for yet must not burn
    # a nightly LLM call. Consolidation is NOT gated by this -- only
    # reflection is.
    paper_reflector = FakeReflector()
    paper_consolidator = FakeConsolidator()
    shadow_reflector = FakeReflector()
    shadow_consolidator = FakeConsolidator()
    components = _components(
        sentinel=FakeSentinel(),
        accounts={
            "paper": _account_bundle(
                strategies=FakeStrategies(active=True),
                reflector=paper_reflector, consolidator=paper_consolidator),
            "shadow": _account_bundle(
                strategies=FakeStrategies(active=False),
                reflector=shadow_reflector, consolidator=shadow_consolidator),
        })

    run_daily_jobs(components, verbose=True)

    assert paper_reflector.calls == 1  # active strategy -> reflection ran
    assert shadow_reflector.calls == 0  # NO LLM call: zero active strategies
    assert paper_consolidator.calls == 1
    assert shadow_consolidator.calls == 1  # consolidation is not gated
    out = capsys.readouterr().out
    assert "[reflection:shadow] skipped (no active strategies)" in out


def test_run_daily_jobs_runs_both_accounts_the_same_night_isolated(capsys):
    # Both accounts' reflection+consolidation run the same night, each
    # isolated from the other -- paper's reflection failing must not stop
    # shadow's chain.
    paper_reflector = FakeReflector(fail=True)
    paper_consolidator = FakeConsolidator()
    shadow_reflector = FakeReflector()
    shadow_consolidator = FakeConsolidator()
    components = _components(
        sentinel=FakeSentinel(),
        accounts={
            "paper": _account_bundle(
                strategies=FakeStrategies(active=True),
                reflector=paper_reflector, consolidator=paper_consolidator),
            "shadow": _account_bundle(
                strategies=FakeStrategies(active=True),
                reflector=shadow_reflector, consolidator=shadow_consolidator),
        })

    run_daily_jobs(components, verbose=True)

    assert paper_reflector.calls == 1
    assert shadow_reflector.calls == 1
    assert paper_consolidator.calls == 1
    assert shadow_consolidator.calls == 1
    stderr = capsys.readouterr().err
    assert "[reflection:paper] failed" in stderr
    assert "[reflection:shadow] failed" not in stderr
