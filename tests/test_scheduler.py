import threading
from datetime import UTC, datetime
from types import SimpleNamespace

from allpath_trade.scheduler import (
    SENTINEL_JOB_ID,
    SENTINEL_MARKET_OPEN_KEY,
    build_jobs,
    is_market_hours,
    reschedule_sentinel_job,
    run_daemon,
)
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
    calls = []

    def sentinel_factory():
        calls.append(1)
        return s

    run_daemon(sentinel_factory, 5, scheduler_cls=ImmediateScheduler)

    assert calls == [1]
    out = capsys.readouterr().out
    assert "checked=1" in out
    assert "errors=0" in out


def test_run_daemon_skips_sentinel_when_market_closed(monkeypatch):
    monkeypatch.setattr("allpath_trade.scheduler.is_market_hours", lambda: False)
    calls = []

    def sentinel_factory():
        calls.append(1)
        raise AssertionError("sentinel_factory must not be called when market is closed")

    run_daemon(sentinel_factory, 5, scheduler_cls=ImmediateScheduler)

    assert calls == []


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
    sched.run_daemon(lambda: None, 60, scheduler_cls=OneShotScheduler,
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


class FakeHolder:
    def __init__(self, components):
        self._components = components

    def get(self):
        return self._components

    def settings(self):
        return self._components.settings


class FakeJournal:
    def __init__(self, trades=0):
        self._trades = trades

    def trades_today(self):
        return self._trades


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


def _components(sentinel, consolidator=None, daily_consolidation=True, interval=5,
                journal=None, queue=None, notifier=None, observations=None,
                app_state=None):
    return SimpleNamespace(
        sentinel=sentinel,
        consolidator=consolidator,
        journal=journal if journal is not None else FakeJournal(),
        queue=queue if queue is not None else FakeQueue(),
        notifier=notifier if notifier is not None else DigestNotifier(),
        observations=observations if observations is not None else FakeObservations(),
        app_state=app_state if app_state is not None else FakeAppState(),
        settings=SimpleNamespace(daily_consolidation=daily_consolidation,
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

    assert app_state.get(sched.SENTINEL_HEARTBEAT_KEY) == "2026-08-09T15:00:00+00:00"


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
    assert app_state.get(sched.SENTINEL_HEARTBEAT_KEY) == "2026-08-09T03:00:00+00:00"


def test_run_daemon_records_heartbeat_even_when_market_closed(monkeypatch):
    import allpath_trade.scheduler as sched

    monkeypatch.setattr(sched, "is_market_hours", lambda: False)
    monkeypatch.setattr(sched, "datetime", SimpleNamespace(
        now=lambda tz=None: datetime(2026, 8, 9, 3, 0, tzinfo=UTC)))
    app_state = FakeAppState()

    run_daemon(lambda: None, 5, scheduler_cls=ImmediateScheduler, app_state=app_state)

    assert app_state.get(sched.SENTINEL_HEARTBEAT_KEY) == "2026-08-09T03:00:00+00:00"


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
    assert subject == "[AllPath] Daily summary"
    # 2 sentinel-sourced observations, 3 trades, 2 pending reviews
    assert "2 rule trigger(s)" in body
    assert "3 trade(s)" in body
    assert "2 item(s) still waiting" in body
    assert "http" not in body.lower() and "<" not in body


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

    run_daemon(lambda: None, 5, scheduler_cls=ImmediateScheduler, app_state=app_state)

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
