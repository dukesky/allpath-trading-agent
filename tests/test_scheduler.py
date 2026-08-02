import threading
from datetime import UTC, datetime

from tests.test_sentinel import make, strategy_yaml
from tradewind.scheduler import is_market_hours, run_daemon

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
    monkeypatch.setattr("tradewind.scheduler.is_market_hours", lambda: True)
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
    monkeypatch.setattr("tradewind.scheduler.is_market_hours", lambda: False)
    calls = []

    def sentinel_factory():
        calls.append(1)
        raise AssertionError("sentinel_factory must not be called when market is closed")

    run_daemon(sentinel_factory, 5, scheduler_cls=ImmediateScheduler)

    assert calls == []


def test_run_daemon_fires_daily_job_after_close(monkeypatch):
    import tradewind.scheduler as sched

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
