from __future__ import annotations

import contextlib
import hashlib
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from allpath_trade.broker.base import Broker
from allpath_trade.config import Settings
from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, AppState
from allpath_trade.telegram import TelegramAPI, TelegramPoller
from allpath_trade.web.auth import install_auth
from allpath_trade.web.chat_service import ChatService
from allpath_trade.web.deps import ComponentHolder
from allpath_trade.web.markdown import split_for_telegram, to_telegram_html

STATIC_DIR = Path(__file__).parent / "static"

# Fire-and-forget mirror sends (spec §④) get their own single-worker pool --
# same shape as execution.py's `_poll_pool` and models_catalog.py's
# `_fetch_pool` -- so a slow/hung Telegram API call never shares a thread
# with, or blocks behind, unrelated background work in this process.
# Single-worker keeps the two-message-per-turn mirror send strictly ordered
# ("You (web): ..." before the reply), matching how a reader would expect a
# conversation transcript to read.
_MIRROR_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telegram-mirror")


def _send_mirror_text(api: TelegramAPI, app_state: AppState, text: str) -> None:
    """Runs on `_MIRROR_EXECUTOR`'s single worker thread -- never on the
    request/turn thread that queued it. Re-reads the paired chat id on
    *every* call rather than once when the mirror fn was registered: pairing
    can change (re-pair, Unpair on the settings page) while the process
    keeps running, and a stale captured chat id would either leak a mirror
    to a chat that unpaired, or silently drop one to a chat that just
    (re-)paired. Not paired -> silently do nothing, same invariant
    TelegramPoller's own unpaired-chat handling follows.

    Wrapped in a broad except: this runs on a background thread pool worker
    with nothing watching it for an uncaught exception (same reasoning as
    TelegramPoller.run_forever's own top-level catch) -- a formatting bug in
    `to_telegram_html`/`split_for_telegram` must degrade to one scrubbed
    stderr line, never take the worker down or (worse) go unnoticed."""
    try:
        chat_id = app_state.get(TELEGRAM_CHAT_ID_KEY)
        if not chat_id:
            return
        html = to_telegram_html(text)
        for chunk in split_for_telegram(html):
            api.send_message(chat_id, chunk)
    except Exception as exc:  # noqa: BLE001 — background thread, must not raise
        print(f"[telegram] mirror send failed: {api._scrub(str(exc))}", file=sys.stderr)


def _mirror_to_telegram(source: str, text: str, reply: str, *,
                        api: TelegramAPI, app_state: AppState) -> None:
    """The direction policy for spec §④'s full mirroring: ChatService fires
    this hook for every turn/note regardless of source (see
    `chat_service.py`'s `set_mirror` docstring); direction is this
    function's call to make, not ChatService's.

    `source == "telegram"` -> no-op: the poller already replied in-channel,
    and mirroring it back would be an echo loop. `source == "web"` ->
    fire-and-forget submit(s) to `_MIRROR_EXECUTOR`, never a direct call:
    the caller here is `ChatService.send`/`note_resolution`, already outside
    `_turn_lock` by the time this runs, but a synchronous Telegram HTTP call
    would still add latency to the web request that triggered it.

    Two shapes, both from spec §④: the normal chat-turn case sends "You
    (web): {text}" and then the reply as two separate messages (so a reader
    scrolling Telegram sees the same back-and-forth the web UI shows); the
    `note_resolution` case (an approval/reject receipt) calls this with
    `reply=""` -- `text` there is already the complete record line (e.g.
    "You resolved #1. Result: order submitted"), so it's sent verbatim with
    no "You (web):" prefix, which would be redundant framing on a line that
    already reads as a record, not a chat message."""
    if source != "web":
        return
    if reply:
        _MIRROR_EXECUTOR.submit(_send_mirror_text, api, app_state, f"You (web): {text}")
        _MIRROR_EXECUTOR.submit(_send_mirror_text, api, app_state, reply)
    else:
        _MIRROR_EXECUTOR.submit(_send_mirror_text, api, app_state, text)


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


def _start_telegram(app: FastAPI, poller_cls: type) -> None:
    """Builds the Telegram transport + poller and starts its daemon thread
    only when `settings.telegram_bot_token` is non-empty (spec §①/§⑥) -- a
    blank token means the entire channel is off: no `TelegramAPI`, no
    thread, no mirror hook registered, zero overhead beyond the one
    Settings read below. `poller_cls` is injectable (defaults to the real
    `TelegramPoller` via `create_app`'s own parameter, same shape as
    `scheduler_cls`) so tests can spy on construction/`run_forever` without
    a real poller thread blocking on a live long-poll.

    The poller is handed `holder.get().app_state` -- and, via the mirror
    fn, so is the mirror -- captured ONCE, here, at startup. A settings save
    that changes `telegram_bot_token` calls `holder.rebuild()` (see
    routes/settings.py) but this thread and this `AppState` object are never
    touched by it: the new token only takes effect after a process restart.
    That's a deliberate, documented tradeoff (see settings.html's Telegram
    help text) to avoid thread lifecycle management (start/stop/rebuild a
    daemon thread mid-request) for what is, in practice, a rare
    configuration change."""
    holder = app.state.holder
    c = holder.get()
    token = c.settings.telegram_bot_token
    if not token:
        return
    api = TelegramAPI(token)
    stop = threading.Event()
    poller = poller_cls(api, app.state.chat_service, c.app_state,
                        c.settings.web_token, stop)
    thread = threading.Thread(target=poller.run_forever, daemon=True,
                              name="telegram-poller")
    thread.start()
    app.state.telegram_stop = stop
    app.state.telegram_thread = thread
    app.state.chat_service.set_mirror(
        partial(_mirror_to_telegram, api=api, app_state=c.app_state))


def _stop_telegram(app: FastAPI) -> None:
    """Mirror image of `_start_telegram`: no-op when no poller was ever
    started (the common no-token case). Sets the stop `Event` (the poller's
    long-poll self-checks it on its next loop iteration/backoff wake) and
    joins with a short bound -- `run_forever`'s own docstring notes the stop
    check happens via `self.stop.wait(delay)` during backoff, so a clean
    exit is normally near-instant; the timeout exists so a wedged poller
    (e.g. a `getUpdates` call that outlived its own socket timeout somehow)
    can never hang `serve`'s shutdown indefinitely -- the thread is a
    daemon, so the process can still exit around it either way."""
    stop = getattr(app.state, "telegram_stop", None)
    if stop is None:
        return
    stop.set()
    app.state.telegram_thread.join(timeout=2)


def create_app(settings: Settings, broker: Broker | None = None,
               start_scheduler: bool = False, scheduler_cls: type | None = None,
               telegram_poller_cls: type | None = None) -> FastAPI:
    if scheduler_cls is None:
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler_cls = BackgroundScheduler
    if telegram_poller_cls is None:
        telegram_poller_cls = TelegramPoller

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            _start_scheduler(app, scheduler_cls)
        _start_telegram(app, telegram_poller_cls)
        yield
        _stop_telegram(app)
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="AllPath Trade", lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.holder = ComponentHolder(settings, broker)
    # Constructed once, here, and shared for the life of the process --
    # not lazily inside the /chat route anymore. The Telegram poller (Task 3)
    # is handed this exact same object so its messages share the web chat's
    # `_turn_lock` and conversation; a settings save invalidates its cached
    # session (ChatService.invalidate(), called from settings.py) rather than
    # replacing the object, which would silently split the two.
    # `app.state.chat` is kept as an alias to the same instance -- existing
    # tests reach the service through that name; new code should use
    # `app.state.chat_service`.
    app.state.chat_service = ChatService(app.state.holder)
    app.state.chat = app.state.chat_service
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
    from allpath_trade.web.routes import (
        approve,
        chat,
        dashboard,
        memory,
        reports,
        reviews,
        strategies,
    )
    from allpath_trade.web.routes import settings as settings_routes

    app.include_router(dashboard.router)
    app.include_router(reviews.router)
    app.include_router(approve.router)
    app.include_router(chat.router)
    app.include_router(strategies.router)
    app.include_router(memory.router)
    app.include_router(reports.router)
    app.include_router(settings_routes.router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    return app
