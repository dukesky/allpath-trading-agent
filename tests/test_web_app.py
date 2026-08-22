import re

import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.web.app import create_app, static_content_hash
from tests.helpers import CONFIGURED_KEYS
from tests.test_sentinel import FakeBroker


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret",
                        **CONFIGURED_KEYS)
    app = create_app(settings, broker=FakeBroker())
    with TestClient(app) as c:
        yield c


def test_healthz_needs_no_auth(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_components_are_available_on_the_app(client):
    holder = client.app.state.holder
    assert holder.get().broker is not None


# --- Fix 2 (Phase 5.5.2): static asset cache-busting ------------------------
# StaticFiles serves app.css/htmx.min.js with no Cache-Control header, so a
# browser's heuristic caching can serve a stale copy indefinitely after a
# deploy. Versioning the URL with a content hash means a changed file is a
# changed URL, so a stale cache entry is simply never reused again.

def test_rendered_page_versions_the_stylesheet_url(client):
    # No login in this fixture -- the auth middleware 303s to /login, and
    # TestClient follows redirects, landing on login.html, which links the
    # same app.css and must carry the same cache-busting treatment.
    body = client.get("/").text
    match = re.search(r'/static/app\.css\?v=([0-9a-f]{8})"', body)
    assert match is not None


def test_rendered_page_versions_the_htmx_script_url(client):
    # login.html doesn't load htmx.min.js (no htmx usage on that page) --
    # check base.html's own script tag on an authenticated page instead.
    client.post("/login", data={"token": "secret"})
    body = client.get("/chat").text
    match = re.search(r'/static/htmx\.min\.js\?v=([0-9a-f]{8})"', body)
    assert match is not None


def test_static_asset_version_is_stable_across_renders(client):
    client.post("/login", data={"token": "secret"})
    body1 = client.get("/chat").text
    body2 = client.get("/chat").text
    v1 = re.search(r'app\.css\?v=([0-9a-f]{8})', body1).group(1)
    v2 = re.search(r'app\.css\?v=([0-9a-f]{8})', body2).group(1)
    assert v1 == v2


def test_static_content_hash_returns_zero_when_file_is_missing(tmp_path):
    # A packaging error (app.css dropped from the wheel) must not crash
    # create_app -- that would contradict STATIC_DIR.mkdir()'s own
    # tolerance for a fresh install the line right above it.
    missing = tmp_path / "does-not-exist.css"
    assert static_content_hash(missing) == "0"


def test_static_content_hash_changes_when_file_content_changes(tmp_path):
    # A temp copy, never the real shipped asset -- mutating
    # allpath_trade/web/static/app.css would poison every other test run
    # sharing the process/checkout.
    css = tmp_path / "app.css"
    css.write_text("body { color: red; }")
    h1 = static_content_hash(css)
    assert len(h1) == 8
    css.write_text("body { color: blue; }")
    h2 = static_content_hash(css)
    assert h1 != h2


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
