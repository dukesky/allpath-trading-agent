from __future__ import annotations

import contextlib
import hashlib
import queue
import sys
import threading
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from allpath_trade.broker.base import Broker
from allpath_trade.config import Settings
from allpath_trade.store.accounts import ACCOUNTS, DEFAULT_ACCOUNT
from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, AppState
from allpath_trade.telegram import TelegramAPI, TelegramPoller, prefixed_chunks
from allpath_trade.web.auth import install_auth
from allpath_trade.web.chat_service import ChatService
from allpath_trade.web.deps import ComponentHolder
from allpath_trade.web.markdown import (
    MAX_TELEGRAM_REPLY_CHUNKS,
    to_telegram_html,
)

STATIC_DIR = Path(__file__).parent / "static"

# Bound on `_MirrorQueue`'s backlog -- see the class docstring's Finding 3
# note for why "bounded, drop-oldest" beats both "unbounded" and "blocks the
# caller".
_MIRROR_QUEUE_MAXSIZE = 50


class _MirrorQueue:
    """Fire-and-forget mirror-send queue for spec §④ (Finding 3 fix).

    ONE per `FastAPI` app instance -- created in `_start_telegram`, stored
    on `app.state.mirror_queue`, shut down in `_stop_telegram` -- rather
    than the module-level `ThreadPoolExecutor` singleton this replaced.
    That singleton was never shut down anywhere: `_stop_telegram` touched
    neither it nor the mirror hook, so its non-daemon worker thread was
    still joined at interpreter exit -- with a dead Telegram endpoint and
    unbounded queueing (every send retries for up to 10s per chunk), a
    heavy web-chat session could leave minutes of queued mirror sends
    blocking a clean `Ctrl-C` shutdown. A per-app instance also means
    `_stop_telegram` can actually shut its queue down for real: a shared
    process-wide singleton can't be permanently shut down without breaking
    every subsequent `create_app()` call in the same process (the test
    suite alone creates dozens of `FastAPI` apps in one process).

    Bounded to `_MIRROR_QUEUE_MAXSIZE` entries with drop-oldest-under-
    backlog semantics -- consistent with docs/TODO.md's existing "mirror
    sends never retried, web ConversationStore is the authoritative record"
    entry: dropping the oldest queued mirror message under sustained
    backlog is the honest continuation of that policy, not a new one.

    A single background worker thread processes the queue strictly in
    order, preserving the ordering guarantee the previous single-worker
    executor gave: "You (web): ..." always sent before the reply."""

    def __init__(self, maxsize: int = _MIRROR_QUEUE_MAXSIZE) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="telegram-mirror")
        self._thread.start()

    def submit(self, fn, *args) -> None:
        try:
            self._queue.put_nowait((fn, args))
        except queue.Full:
            # Drop the oldest queued send to make room -- best-effort
            # delivery under backlog (see the class docstring) rather than
            # blocking the caller (a web request thread) or growing
            # unbounded while a dead/blocked Telegram endpoint holds up
            # every send for its full retry timeout.
            with contextlib.suppress(queue.Empty):
                self._queue.get_nowait()
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait((fn, args))

    def _run(self) -> None:
        while True:
            try:
                fn, args = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                fn(*args)
            except Exception:  # noqa: BLE001, S110 — background worker thread;
                pass          # the send fns already swallow+log internally,
                              # this is a pure belt-and-suspenders catch.

    def shutdown(self, wait: bool = False, cancel_futures: bool = True) -> None:
        self._stop.set()
        if cancel_futures:
            # Drain anything still queued -- shutdown must not block on a
            # dead Telegram endpoint's retry/backoff (Finding 3).
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
        if wait and self._thread is not None:
            self._thread.join(timeout=2)


def _send_mirror_text(api: TelegramAPI, app_state: AppState, text: str,
                      account: str = DEFAULT_ACCOUNT) -> None:
    """Runs on the `_MirrorQueue` worker thread -- never on the
    request/turn thread that queued it. Re-reads the paired chat id on
    *every* call rather than once when the mirror fn was registered: pairing
    can change (re-pair, Unpair on the settings page) while the process
    keeps running, and a stale captured chat id would either leak a mirror
    to a chat that unpaired, or silently drop one to a chat that just
    (re-)paired. Not paired -> silently do nothing, same invariant
    TelegramPoller's own unpaired-chat handling follows.

    Wrapped in a broad except: this runs on a background thread with
    nothing watching it for an uncaught exception (same reasoning as
    TelegramPoller.run_forever's own top-level catch) -- a formatting bug in
    `to_telegram_html`/`split_for_telegram` must degrade to one scrubbed
    stderr line, never take the worker down or (worse) go unnoticed.

    I3: `text` arrives as raw Markdown and the `[Paper] `/`[Shadow] ` prefix
    is applied HERE, after `to_telegram_html` has run -- never by the caller
    beforehand. Prefixing the Markdown pushed every block construct off the
    start of its line, so the converter no longer recognized it: a fenced
    code block mirrored as literal backticks plus an empty `<pre></pre>`, a
    heading lost its `<b>`, and a table was re-parsed with the prefix as its
    first cell. `prefixed_chunks` (telegram.py, shared with the poller's own
    reply path) also budgets the prefix's length into the split limit -- see
    its docstring for I2."""
    try:
        chat_id = app_state.get(TELEGRAM_CHAT_ID_KEY)
        if not chat_id:
            return
        chunks = prefixed_chunks(account, to_telegram_html(text))
        if len(chunks) > MAX_TELEGRAM_REPLY_CHUNKS:
            # Defensive belt (Finding 1), mirrored from
            # `TelegramPoller._handle_chat_text`'s own cap -- see its
            # comment for why this exists even though `split_for_telegram`
            # is now proven correct for the corpus this codebase's tests
            # exercise.
            print(f"[telegram] mirror text split into {len(chunks)} chunks "
                  f"(cap {MAX_TELEGRAM_REPLY_CHUNKS}); truncating", file=sys.stderr)
            chunks = [*chunks[:MAX_TELEGRAM_REPLY_CHUNKS], "(message truncated: too long for Telegram)"]
        for chunk in chunks:
            api.send_message(chat_id, chunk)
    except Exception as exc:  # noqa: BLE001 — background thread, must not raise
        print(f"[telegram] mirror send failed: {api._scrub(str(exc))}", file=sys.stderr)


def _mirror_to_telegram(source: str, text: str, reply: str, *,
                        api: TelegramAPI, app_state: AppState,
                        mirror_queue: _MirrorQueue,
                        account: str = DEFAULT_ACCOUNT) -> None:
    """The direction policy for spec §④'s full mirroring: ChatService fires
    this hook for every turn/note regardless of source (see
    `chat_service.py`'s `set_mirror` docstring); direction is this
    function's call to make, not ChatService's.

    `source == "telegram"` -> no-op: the poller already replied in-channel,
    and mirroring it back would be an echo loop. `source == "web"` ->
    fire-and-forget submit(s) to `mirror_queue` (this app instance's
    `_MirrorQueue`, see its own docstring for Finding 3), never a direct
    call: the caller here is `ChatService.send`/`note_resolution`, already
    outside `_turn_lock` by the time this runs, but a synchronous Telegram
    HTTP call would still add latency to the web request that triggered it.

    Two shapes, both from spec §④: the normal chat-turn case sends "You
    (web): {text}" and then the reply as two separate messages (so a reader
    scrolling Telegram sees the same back-and-forth the web UI shows); the
    `note_resolution` case (an approval/reject receipt) calls this with
    `reply=""` -- `text` there is already the complete record line (e.g.
    "You resolved #1. Result: order submitted"), so it's sent verbatim with
    no "You (web):" prefix, which would be redundant framing on a line that
    already reads as a record, not a chat message.

    `account` (shadow-dual-active T5): with two web-side ChatServices now
    mirroring into the SAME single paired Telegram chat, a mirrored line
    with no account marker would be ambiguous about which pipeline's turn
    it records -- every mirrored message is prefixed `[Paper]`/`[Shadow]`
    accordingly. `_start_telegram` binds this to each ChatService's own
    account via `functools.partial` at registration time (one partial per
    instance), so this function itself never has to look the account up;
    it defaults to `DEFAULT_ACCOUNT` only so direct callers that predate
    this parameter (existing tests) keep compiling -- every real caller
    always passes it explicitly."""
    if source != "web":
        return
    # I3: `account` is handed DOWN to `_send_mirror_text` rather than being
    # turned into a prefix string here -- the prefix can only be applied
    # after `to_telegram_html` has converted the Markdown (see that
    # function's docstring), and it comes from `events._prefix` there rather
    # than being re-derived as an f-string in a third place.
    if reply:
        mirror_queue.submit(_send_mirror_text, api, app_state,
                            f"You (web): {text}", account)
        mirror_queue.submit(_send_mirror_text, api, app_state, reply, account)
    else:
        mirror_queue.submit(_send_mirror_text, api, app_state, text, account)


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

    The poller is handed `holder` itself (not a `c.app_state`/
    `c.settings.web_token` snapshot) -- see `TelegramPoller`'s own class
    docstring for Findings 2 and 5: reading through `holder.get()` at the
    point of use means a `/settings/reset-token` save takes effect on the
    poller's very next `/start` check, and an in-place `db_path` change via
    `holder.rebuild()` never leaves the poller pinned to a stale/closed
    connection. The bot TOKEN itself (`TelegramAPI(token)` below) is still
    captured once at startup, unaffected by either finding -- changing
    *that* setting still requires a restart, same as `settings.html`'s
    Telegram help text has always documented.

    The mirror queue (`_MirrorQueue`, Finding 3) is created and started
    here too, one per app instance, and stored on `app.state.mirror_queue`
    so `_stop_telegram` can shut it down.

    shadow-dual-active T5: the poller is handed `app.state.chat_services`
    (the whole `{account: ChatService}` dict, not just paper's instance) so
    it can route paired-chat text to whichever account the chat is
    currently talking to (`/account`, defaulting to shadow -- see
    `telegram.py`). The mirror hook is registered on EVERY account's
    ChatService, each bound (via its own `functools.partial`) to that
    ChatService's own `account` -- a shadow web-chat turn must mirror into
    Telegram labelled `[Shadow]`, not silently only mirror paper's."""
    holder = app.state.holder
    c = holder.get()
    token = c.settings.telegram_bot_token
    if not token:
        return
    api = TelegramAPI(token)
    stop = threading.Event()
    poller = poller_cls(api, app.state.chat_services, holder, stop)
    thread = threading.Thread(target=poller.run_forever, daemon=True,
                              name="telegram-poller")
    thread.start()
    app.state.telegram_stop = stop
    app.state.telegram_thread = thread
    mirror_queue = _MirrorQueue()
    mirror_queue.start()
    app.state.mirror_queue = mirror_queue
    for account, service in app.state.chat_services.items():
        service.set_mirror(
            partial(_mirror_to_telegram, api=api, app_state=c.app_state,
                   mirror_queue=mirror_queue, account=account))


def _stop_telegram(app: FastAPI) -> None:
    """Mirror image of `_start_telegram`: no-op when no poller was ever
    started (the common no-token case). Sets the stop `Event` (the poller's
    long-poll self-checks it on its next loop iteration/backoff wake) and
    joins with a short bound -- `run_forever`'s own docstring notes the stop
    check happens via `self.stop.wait(delay)` during backoff, so a clean
    exit is normally near-instant; the timeout exists so a wedged poller
    (e.g. a `getUpdates` call that outlived its own socket timeout somehow)
    can never hang `serve`'s shutdown indefinitely -- the thread is a
    daemon, so the process can still exit around it either way.

    Also shuts down the per-app `_MirrorQueue` (Finding 3) with
    `wait=False, cancel_futures=True` -- shutdown must not block on a dead
    Telegram endpoint's retry/backoff, whatever is still queued is dropped
    -- and clears the mirror hook so it can never fire against a queue
    that's already been told to stop."""
    stop = getattr(app.state, "telegram_stop", None)
    if stop is None:
        return
    stop.set()
    app.state.telegram_thread.join(timeout=2)
    for service in app.state.chat_services.values():
        service.set_mirror(None)
    mirror_queue = getattr(app.state, "mirror_queue", None)
    if mirror_queue is not None:
        mirror_queue.shutdown(wait=False, cancel_futures=True)


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
    #
    # shadow-dual-active T5: one ChatService per account, keyed by
    # `store.accounts.ACCOUNTS`. `app.state.chat_service` (singular) is kept
    # as an alias to the PAPER instance -- exactly the same "legacy alias"
    # shape `app.py`'s `Components` uses for its own paper-only fields --
    # so every pre-existing caller that reaches the service through that
    # name (the whole test suite predates account-awareness) keeps working
    # unchanged; new account-aware code (web/routes/chat.py, telegram.py)
    # goes through `app.state.chat_services[account]` instead.
    # `app.state.chat` is kept as a second alias to the same paper instance,
    # for the same reason.
    app.state.chat_services = {
        account: ChatService(app.state.holder, account=account)
        for account in ACCOUNTS
    }
    app.state.chat_service = app.state.chat_services[DEFAULT_ACCOUNT]
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
    from allpath_trade.web import account_ctx
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

    app.include_router(account_ctx.router)
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
