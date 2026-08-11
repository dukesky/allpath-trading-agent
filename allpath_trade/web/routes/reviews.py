from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from allpath_trade.execution import ExecutionError
from allpath_trade.store.reviews import ReviewError, RevisionValidationError
from allpath_trade.web.routes.dashboard import error_redirect, nav_context, notice_redirect
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
        "notice": request.query_params.get("notice"),
        **nav_context(c)})


def _back_to_reviews(error: str | None = None) -> RedirectResponse:
    # Shared idiom -- see dashboard.py's error_redirect docstring for why
    # this redirects instead of returning a fragment directly.
    return error_redirect("/reviews", error)


def _back_to_reviews_ok(message: str) -> RedirectResponse:
    return notice_redirect("/reviews", message)


def _echo_resolution(request: Request, review_id: int, row_source: str, summary: str) -> None:
    # Only chat-sourced reviews have a live conversation to report back
    # into; a sentinel-triggered row has no ChatService turn waiting on it.
    #
    # `summary` isn't fully trusted text: the reject path folds in a
    # user-supplied `note` and the execution-failure path folds in a raw
    # broker exception message, either of which could contain a forged
    # marker trying to impersonate a real system line. `note_resolution`
    # fences the whole line (fence_external) before it reaches the model, so
    # that's handled downstream rather than here.
    service = getattr(request.app.state, "chat", None)
    if service is not None and row_source == "chat":
        service.note_resolution(f"You resolved #{review_id}. Result: {summary}")


@router.post("/reviews/{review_id}/approve")
def approve(request: Request, review_id: int) -> Response:
    c = request.app.state.holder.get()
    try:
        row = c.queue.get(review_id)
    except ReviewError as exc:
        return _back_to_reviews(f"Not processed: {exc}")
    row_source, kind = row["source"], row["kind"]
    try:
        result = c.queue.approve(review_id)
    except RevisionValidationError as exc:
        # kind-branch HARD PREREQ (Task 3): approve() returns None for
        # strategy_revision rows -- ReviewError below would catch this too
        # (RevisionValidationError subclasses it), but this must be caught
        # first: ReviewQueue._approve_revision already rolled the row back
        # to "pending" before raising (see the loud comment there), so the
        # message here has to say that, not the generic "not processed".
        _echo_resolution(request, review_id, row_source,
                         f"revision left pending: re-validation failed ({exc})")
        return _back_to_reviews(
            f"Revision failed re-validation and was left pending -- "
            f"you can retry or reject it: {exc}")
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

    if kind == "strategy_revision":
        # approve() returns None for revision rows (Task 2) -- there is no
        # ExecutionResult to inspect, unlike the order branch below. Unlike
        # the failure paths above, this is a genuine success, so it goes
        # through the `notice` channel (Finding 4) instead of `error` --
        # the page renders it with `.flash-ok`, not red error styling.
        _echo_resolution(request, review_id, row_source,
                         f"revision applied to {row['strategy_id']}")
        return _back_to_reviews_ok(f"Revision applied to {row['strategy_id']}.")

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
