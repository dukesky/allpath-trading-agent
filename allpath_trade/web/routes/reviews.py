from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from allpath_trade.execution import ExecutionError
from allpath_trade.store.reviews import ReviewError
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.templating import templates

router = APIRouter()


def _decorate(row) -> dict:  # row: sqlite3.Row
    item = dict(row)
    for field in ("snapshot", "intent", "agent_analysis", "execution_result"):
        raw = item.get(field)
        item[field] = json.loads(raw) if raw else None
    return item


@router.get("/reviews", response_class=HTMLResponse)
def reviews(request: Request) -> HTMLResponse:
    c = request.app.state.holder.get()
    items = [_decorate(r) for r in c.queue.list("pending")]
    recent = [_decorate(r) for r in c.queue.list(None)][:20]
    return templates.TemplateResponse(request, "reviews.html", {
        "page": "reviews", "items": items,
        "recent": [r for r in recent if r["status"] != "pending"],
        "error": request.query_params.get("error"),
        **nav_context(c)})


def _back_to_reviews(error: str | None = None) -> RedirectResponse:
    # These routes are hit by a plain `<form method="post">`, not an htmx
    # partial swap -- there is no element in the current page for a bare
    # HTML fragment to swap into. Returning one directly would replace the
    # whole tab with a single sentence and strand the user with no nav, no
    # way back, nothing else to review. Redirecting back to /reviews (303,
    # so the browser re-issues as GET) keeps the page coherent either way;
    # the error, when there is one, rides along as a query param and is
    # rendered at the top of the reviews page.
    target = "/reviews"
    if error:
        target += f"?error={quote(error)}"
    return RedirectResponse(target, status_code=303)


@router.post("/reviews/{review_id}/approve")
def approve(request: Request, review_id: int) -> Response:
    c = request.app.state.holder.get()
    try:
        result = c.queue.approve(review_id)
    except (ReviewError, ExecutionError) as exc:
        return _back_to_reviews(str(exc))
    if not result.submitted:
        reasons = "; ".join(result.decision.reasons)
        return _back_to_reviews(f"Rejected by the risk gate: {reasons}")
    return _back_to_reviews()


@router.post("/reviews/{review_id}/reject")
def reject(request: Request, review_id: int, note: str = Form("")) -> Response:
    c = request.app.state.holder.get()
    try:
        c.queue.reject(review_id, note)
    except ReviewError as exc:
        return _back_to_reviews(str(exc))
    return _back_to_reviews()
