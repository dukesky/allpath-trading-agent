from __future__ import annotations

import concurrent.futures
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from allpath_trade.app import Components
from allpath_trade.web.templating import templates

router = APIRouter()

BROKER_TIMEOUT_SECONDS = 10
# A dedicated, small pool -- not FastAPI/Starlette's own sync-handler
# threadpool. A sync route handler already runs in that shared pool, which
# every other page's request also needs a worker from; a broker call that
# just hangs (a phone that keeps reloading against a stalled Alpaca
# connection) would occupy one of those workers indefinitely, and enough
# concurrent hangs exhaust the pool and take the whole app down with it —
# login, chat, reviews, everything. Submitting to this pool instead means a
# hang can only ever consume *this* pool's capacity, and `.result(timeout=)`
# lets the request thread give up and return the request promptly either
# way; the submitted call keeps running in the background rather than being
# killed (Python has no way to force that), but nothing here waits on it.
_broker_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="dashboard-broker")


def nav_context(components: Components) -> dict:
    return {"pending_count": len(components.queue.list())}


def _with_timeout(fn):
    # Reads the module attribute at call time, not as a bound default
    # argument -- a default value is captured once at function-definition
    # time, which would make BROKER_TIMEOUT_SECONDS un-monkeypatchable
    # (tests need to shrink it to prove the bound is actually enforced,
    # without waiting out the real production timeout).
    return _broker_pool.submit(fn).result(timeout=BROKER_TIMEOUT_SECONDS)


def error_redirect(target: str, message: str | None = None) -> RedirectResponse:
    """The shared "303 back to a page, error riding along as a query
    param" idiom. Every POST-triggered failure across the app (reviews.py's
    approve/reject, strategies.py's rearm) is hit by a plain
    `<form method="post">`, not an htmx partial swap — there is no element
    in the current page for a bare HTML fragment to swap into, so a route
    can't just return one directly without stranding the user with no nav
    and no way back. Redirecting (303, so the browser re-issues as GET)
    keeps the page coherent either way; `message`, when given, is rendered
    at the top of whichever page `target` is."""
    if message:
        return RedirectResponse(f"{target}?error={quote(message)}", status_code=303)
    return RedirectResponse(target, status_code=303)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    c = request.app.state.holder.get()
    account = None
    positions: list = []
    broker_error = ""
    try:
        account = _with_timeout(c.broker.get_account)
        positions = _with_timeout(c.broker.get_positions)
    except concurrent.futures.TimeoutError:
        broker_error = f"Broker unavailable: timed out after {BROKER_TIMEOUT_SECONDS}s"
    except Exception as exc:  # noqa: BLE001 — a broker outage must not blank the page
        broker_error = f"Broker unavailable: {exc}"

    errors: list[str] = []
    strategies = c.strategies.load_all(status=None, errors=errors)
    return templates.TemplateResponse(request, "dashboard.html", {
        "page": "dashboard", "account": account, "positions": positions,
        "broker_error": broker_error, "strategies": strategies,
        "strategy_errors": errors, "trades": c.journal.recent(limit=8),
        **nav_context(c)})
