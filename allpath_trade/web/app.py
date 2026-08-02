from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from allpath_trade.broker.base import Broker
from allpath_trade.config import Settings
from allpath_trade.web.deps import ComponentHolder

STATIC_DIR = Path(__file__).parent / "static"


def _start_scheduler(app: FastAPI) -> None:
    from apscheduler.schedulers.background import BackgroundScheduler

    from allpath_trade.scheduler import build_jobs

    holder = app.state.holder
    scheduler = BackgroundScheduler()
    build_jobs(scheduler, holder)
    scheduler.start()
    app.state.scheduler = scheduler


def create_app(settings: Settings, broker: Broker | None = None,
               start_scheduler: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            _start_scheduler(app)
        yield
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="AllPath Trade", lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.holder = ComponentHolder(settings, broker)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    return app
