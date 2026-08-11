from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from allpath_trade.execution import ExecutionError
from allpath_trade.store.reviews import ReviewError, RevisionValidationError
from allpath_trade.web.routes.dashboard import order_price_context
from allpath_trade.web.templating import templates

router = APIRouter()

# Every route here lives under this prefix and is exempt from the normal
# session-cookie auth gate (see web/auth.py's `install_auth`) -- security
# comes instead from the per-review, single-use token these routes require
# and burn on use (ReviewQueue.validate_token/consume_token), not from a
# logged-in session. Nothing here ever calls Executor.execute() directly;
# resolution always goes through the same ReviewQueue.approve/reject the
# in-app /reviews page uses.
PREFIX = "/a"


def _invalid_page(request: Request) -> HTMLResponse:
    # Deliberately uniform: whether the review id doesn't exist, the token
    # is wrong, expired, already used, or the review is no longer pending,
    # this is the ONLY thing a visitor is ever shown -- no oracle for
    # *why* the link doesn't work. Always HTTP 200 (not 404/403), so the
    # response itself gives away nothing either.
    return templates.TemplateResponse(request, "approve_invalid.html", {})


def _result_page(request: Request, *, ok: bool, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "approve_result.html", {"ok": ok, "message": message})


def _confirm_context(c, row) -> dict:
    """Everything the standalone GET confirmation page needs, built once
    from the validated row -- ticker/side/amount for an order-kind review,
    a simpler revision-shaped summary for a strategy_revision (the link is
    kind-agnostic: both resolve through the same POST routes below), plus
    the shared price context (Part B) reused verbatim from the in-app
    reviews page so the two surfaces never disagree on a number."""
    kind = row["kind"]
    ctx: dict = {
        "review_id": row["id"], "ticker": row["ticker"],
        "condition": row["condition"] if kind == "order" else "",
        "kind": kind, "strategy_id": row["strategy_id"],
        "side": "", "amount_label": "",
        "trigger_price": None, "current_price": None, "price_class": "",
        "day_change_pct": None, "deviation_pct": None, "est_shares": None,
        "market_open": False,
    }
    if kind == "strategy_revision":
        ctx["side"] = "Strategy revision"
        ctx["amount_label"] = f"Proposed change to {row['strategy_id']}"
        return ctx

    intent = json.loads(row["intent"]) if row["intent"] else None
    if intent is not None:
        ctx["side"] = intent.get("side", "")
        if intent.get("qty"):
            ctx["amount_label"] = f"{intent['qty']} shares"
        elif intent.get("notional"):
            try:
                ctx["amount_label"] = f"${Decimal(str(intent['notional'])):,.2f}"
            except (InvalidOperation, TypeError):
                ctx["amount_label"] = f"${intent['notional']}"

    snapshot = json.loads(row["snapshot"]) if row["snapshot"] else None
    pc = order_price_context(c.data, row["ticker"], snapshot, intent)
    ctx.update(pc)
    return ctx


@router.get(PREFIX + "/{review_id}", response_class=HTMLResponse)
def confirm(request: Request, review_id: int, k: str = "") -> HTMLResponse:
    c = request.app.state.holder.get()
    row = c.queue.validate_token(review_id, k)
    if row is None:
        return _invalid_page(request)
    context = _confirm_context(c, row)
    context["token"] = k
    return templates.TemplateResponse(request, "approve_confirm.html", context)


def _resolve(request: Request, review_id: int, token: str, *, reject: bool) -> HTMLResponse:
    c = request.app.state.holder.get()
    # Burns the token BEFORE acting (see ReviewQueue.consume_token's
    # docstring for why that ordering specifically matters for the
    # RevisionValidationError case below): whatever happens next, this
    # link is dead the moment it's submitted -- a failed execution must
    # push the user back into the app, not invite a retry via the same link.
    row = c.queue.consume_token(review_id, token)
    if row is None:
        return _invalid_page(request)

    if reject:
        try:
            c.queue.reject(review_id)
        except ReviewError as exc:
            return _result_page(request, ok=False, message=f"Not processed: {exc}")
        return _result_page(request, ok=True, message=f"Rejected #{review_id}.")

    try:
        result = c.queue.approve(review_id)
    except RevisionValidationError as exc:
        return _result_page(
            request, ok=False,
            message=(f"Revision failed re-validation and was left pending "
                     f"for you to retry or reject in the app: {exc}"))
    except ExecutionError as exc:
        return _result_page(
            request, ok=False,
            message=f"Approved, but execution failed: {exc}")
    except ReviewError as exc:
        return _result_page(request, ok=False, message=f"Not processed: {exc}")

    if row["kind"] == "strategy_revision":
        return _result_page(
            request, ok=True, message=f"Revision applied to {row['strategy_id']}.")
    if not result.submitted:
        reasons = "; ".join(result.decision.reasons)
        return _result_page(request, ok=False,
                            message=f"Rejected by the risk gate: {reasons}")
    return _result_page(request, ok=True, message="Order submitted.")


@router.post(PREFIX + "/{review_id}/approve", response_class=HTMLResponse)
def approve(request: Request, review_id: int, k: str = Form("")) -> HTMLResponse:
    return _resolve(request, review_id, k, reject=False)


@router.post(PREFIX + "/{review_id}/reject", response_class=HTMLResponse)
def reject(request: Request, review_id: int, k: str = Form("")) -> HTMLResponse:
    return _resolve(request, review_id, k, reject=True)
