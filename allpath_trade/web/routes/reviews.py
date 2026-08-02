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
    # One query, then filter, then slice: `list(None)` already carries the
    # pending rows, so a second `list("pending")` query is redundant. And the
    # slice-then-filter order matters — with >=20 pending rows, slicing
    # first would take only pending rows off the top and filtering would
    # then empty the "Resolved" section even when resolved rows exist.
    all_items = [_decorate(r) for r in c.queue.list(None)]
    items = [r for r in all_items if r["status"] == "pending"]
    recent = [r for r in all_items if r["status"] != "pending"][:20]
    return templates.TemplateResponse(request, "reviews.html", {
        "page": "reviews", "items": items, "recent": recent,
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


def _echo_resolution(request: Request, review_id: int, row_source: str, summary: str) -> None:
    # Only chat-sourced reviews have a live conversation to report back
    # into; a sentinel-triggered row has no ChatService turn waiting on it.
    service = getattr(request.app.state, "chat", None)
    if service is not None and row_source == "chat":
        service.note_resolution(f"You resolved #{review_id}. Result: {summary}")


@router.post("/reviews/{review_id}/approve")
def approve(request: Request, review_id: int) -> Response:
    c = request.app.state.holder.get()
    try:
        row_source = c.queue.get(review_id)["source"]
        result = c.queue.approve(review_id)
    except ReviewError as exc:
        # Nothing was claimed: the atomic UPDATE never matched a pending
        # row (already resolved, missing, corrupt intent). The review's
        # state is unchanged from before the click.
        return _back_to_reviews(f"Not processed: {exc}")
    except ExecutionError as exc:
        # The review WAS claimed (status is already "approved" in the
        # store) before the broker call failed. The user has to reason
        # about a claimed-but-unknown-outcome order, not a no-op.
        _echo_resolution(request, review_id, row_source, f"execution failed: {exc}")
        return _back_to_reviews(f"Review claimed, but execution failed: {exc}")
    if not result.submitted:
        reasons = "; ".join(result.decision.reasons)
        _echo_resolution(request, review_id, row_source,
                         f"blocked by the risk gate ({reasons})")
        return _back_to_reviews(f"Rejected by the risk gate: {reasons}")
    _echo_resolution(request, review_id, row_source, "order submitted")
    return _back_to_reviews()


@router.post("/reviews/{review_id}/reject")
def reject(request: Request, review_id: int, note: str = Form("")) -> Response:
    c = request.app.state.holder.get()
    try:
        row_source = c.queue.get(review_id)["source"]
        c.queue.reject(review_id, note)
    except ReviewError as exc:
        return _back_to_reviews(f"Not processed: {exc}")
    summary = f"rejected ({note})" if note else "rejected"
    _echo_resolution(request, review_id, row_source, summary)
    return _back_to_reviews()
