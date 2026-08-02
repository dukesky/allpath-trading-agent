from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from allpath_trade.app import Components
from allpath_trade.web.templating import templates

router = APIRouter()


def nav_context(components: Components) -> dict:
    return {"pending_count": len(components.queue.list())}


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    c = request.app.state.holder.get()
    account = None
    positions: list = []
    broker_error = ""
    try:
        account = c.broker.get_account()
        positions = c.broker.get_positions()
    except Exception as exc:  # noqa: BLE001 — a broker outage must not blank the page
        broker_error = f"Broker unavailable: {exc}"

    errors: list[str] = []
    strategies = c.strategies.load_all(status=None, errors=errors)
    return templates.TemplateResponse(request, "dashboard.html", {
        "page": "dashboard", "account": account, "positions": positions,
        "broker_error": broker_error, "strategies": strategies,
        "strategy_errors": errors, "trades": c.journal.recent(limit=8),
        **nav_context(c)})
