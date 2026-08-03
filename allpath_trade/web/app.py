from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from allpath_trade.broker.base import Broker
from allpath_trade.config import Settings
from allpath_trade.web.auth import install_auth
from allpath_trade.web.deps import ComponentHolder

STATIC_DIR = Path(__file__).parent / "static"


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

    # Aliased: `settings` is already this function's own Settings parameter --
    # importing the routes module under that name would shadow it.
    from allpath_trade.web.routes import chat, dashboard, memory, reviews, strategies
    from allpath_trade.web.routes import settings as settings_routes

    app.include_router(dashboard.router)
    app.include_router(reviews.router)
    app.include_router(chat.router)
    app.include_router(strategies.router)
    app.include_router(memory.router)
    app.include_router(settings_routes.router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    return app
