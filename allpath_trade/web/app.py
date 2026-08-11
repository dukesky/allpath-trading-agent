from __future__ import annotations

import contextlib
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from allpath_trade.broker.base import Broker
from allpath_trade.config import Settings
from allpath_trade.web.auth import install_auth
from allpath_trade.web.deps import ComponentHolder

STATIC_DIR = Path(__file__).parent / "static"


def static_content_hash(path: Path) -> str:
    """First 8 hex chars of the file's sha256 -- used as a cache-busting
    query param (?v=<hash>) on static asset URLs. StaticFiles serves with no
    Cache-Control header, so browsers fall back to heuristic caching and can
    serve a stale app.css/htmx.min.js indefinitely after a deploy; a changed
    file produces a changed URL, so a stale cache entry (keyed on the old
    URL) is simply never reused. Takes a path rather than reading STATIC_DIR
    itself so tests can point it at a temp copy instead of mutating the real
    shipped asset.

    A missing/unreadable file (a packaging error that dropped app.css from
    the wheel, a permissions issue) degrades to a constant "0" rather than
    letting a bare FileNotFoundError/OSError out of create_app -- that would
    contradict the mkdir-tolerance of the STATIC_DIR.mkdir() call right
    before this runs, crashing startup opaquely over what's ultimately just
    a cache-busting nicety. The URL still works either way (StaticFiles
    still serves the real file); only the busting query param goes stale
    across a deploy until the packaging issue is fixed."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:8]
    except OSError:
        return "0"


def _start_scheduler(app: FastAPI, scheduler_cls: type) -> None:
    from allpath_trade.scheduler import build_jobs

    holder = app.state.holder
    scheduler = scheduler_cls()
    try:
        build_jobs(scheduler, holder)
        scheduler.start()
    except Exception:
        # Startup failed after the scheduler object exists but maybe before
        # (or during) `.start()` — don't leave a partially started scheduler
        # running behind a FastAPI app that never finished coming up. If it
        # never actually started, `.shutdown()` may itself raise; that's not
        # the error the caller needs to see, so it's swallowed here.
        with contextlib.suppress(Exception):
            scheduler.shutdown(wait=False)
        raise
    app.state.scheduler = scheduler


def create_app(settings: Settings, broker: Broker | None = None,
               start_scheduler: bool = False, scheduler_cls: type | None = None) -> FastAPI:
    if scheduler_cls is None:
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler_cls = BackgroundScheduler

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            _start_scheduler(app, scheduler_cls)
        yield
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="AllPath Trade", lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.holder = ComponentHolder(settings, broker)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    install_auth(app)

    # Env globals apply to every template render regardless of the context
    # dict a given route passes in (login.html included) -- computed once
    # at startup, not per-request, since the shipped asset content only
    # changes on a deploy.
    from allpath_trade.web.templating import templates

    templates.env.globals["app_css_v"] = static_content_hash(STATIC_DIR / "app.css")
    templates.env.globals["htmx_js_v"] = static_content_hash(STATIC_DIR / "htmx.min.js")

    # Aliased: `settings` is already this function's own Settings parameter --
    # importing the routes module under that name would shadow it.
    from allpath_trade.web.routes import chat, dashboard, memory, reports, reviews, strategies
    from allpath_trade.web.routes import settings as settings_routes

    app.include_router(dashboard.router)
    app.include_router(reviews.router)
    app.include_router(chat.router)
    app.include_router(strategies.router)
    app.include_router(memory.router)
    app.include_router(reports.router)
    app.include_router(settings_routes.router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    return app
