import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from tests.test_sentinel import FakeBroker


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    app = create_app(settings, broker=FakeBroker())
    with TestClient(app) as c:
        yield c


def test_healthz_needs_no_auth(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_components_are_available_on_the_app(client):
    holder = client.app.state.holder
    assert holder.get().broker is not None


# -- start_scheduler=True: the actual "FastAPI + sentinel in one process"
# wiring this task exists to deliver. A fake `scheduler_cls` is injected
# rather than a real BackgroundScheduler so these tests don't depend on
# wall-clock timing or a live thread pool. --


def _settings(tmp_path):
    (tmp_path / "strategies").mkdir(exist_ok=True)
    return Settings(_env_file=None, db_path=tmp_path / "t.db",
                    strategies_dir=tmp_path / "strategies",
                    memory_dir=tmp_path / "memory")


class RecordingScheduler:
    """Fake matching the (add_job, start, shutdown) surface `_start_scheduler`
    uses from APScheduler's BackgroundScheduler."""

    def __init__(self):
        self.started = False
        self.shutdown_called = False

    def add_job(self, fn, trigger, **kwargs):
        pass

    def start(self):
        self.started = True

    def shutdown(self, wait=False):
        self.shutdown_called = True


class FailingScheduler(RecordingScheduler):
    """Simulates a scheduler whose `.start()` blows up mid-startup."""

    def start(self):
        self.started = True
        raise RuntimeError("boom")


def test_scheduler_starts_on_entry_and_shuts_down_on_exit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    instances = []

    class TrackedScheduler(RecordingScheduler):
        def __init__(self):
            super().__init__()
            instances.append(self)

    app = create_app(_settings(tmp_path), broker=FakeBroker(), start_scheduler=True,
                     scheduler_cls=TrackedScheduler)

    with TestClient(app):
        assert instances[0].started is True
        assert instances[0].shutdown_called is False

    assert instances[0].shutdown_called is True


def test_scheduler_is_shut_down_when_startup_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    instances = []

    class TrackedFailingScheduler(FailingScheduler):
        def __init__(self):
            super().__init__()
            instances.append(self)

    app = create_app(_settings(tmp_path), broker=FakeBroker(), start_scheduler=True,
                     scheduler_cls=TrackedFailingScheduler)

    with pytest.raises(RuntimeError, match="boom"), TestClient(app):
        pass  # startup should fail before the app ever serves a request

    assert instances[0].shutdown_called is True
    # A scheduler that never finished starting shouldn't be reachable off the
    # app either -- nothing downstream can wrongly assume it's live.
    assert not hasattr(app.state, "scheduler")
