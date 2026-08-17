from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from allpath_trade.execution import ExecutionError
from allpath_trade.store.reviews import ReviewError, RevisionValidationError
from allpath_trade.web.routes.dashboard import order_price_context

# `split_diff_rows` is the same server-side diff-pairing helper the in-app
# review card (web/routes/reviews.py -> _review_card.html, via the shared
# _split_diff.html macro) uses to render a strategy_revision's diff --
# reused verbatim (I3) so the approve-link confirm page never invents its
# own escaping/pairing logic for the same content. No import cycle:
# reviews.py never imports approve.py.
from allpath_trade.web.routes.reviews import (
    current_strategy_yaml,
    revision_view,
    split_diff_rows,
)
from allpath_trade.web.templating import templates

router = APIRouter()

# M3: every /a/ response is a one-shot page reached from a notification
# (often over a shared/forwarded link) that must never be cached by an
# intermediary or leak its URL (which embeds the single-use token) via the
# Referer header on an outbound link/asset request.
_SECURITY_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

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
    # response itself gives away nothing either. M1: this also covers a
    # non-numeric review id in the URL (`/a/abc`) -- see `confirm`/
    # `_resolve` below, which parse it themselves rather than letting
    # FastAPI's own `int` path-converter reject it with a raw 422 that
    # would otherwise be the one distinguishing, stack-revealing response
    # on this whole surface.
    return templates.TemplateResponse(
        request, "approve_invalid.html", {}, headers=_SECURITY_HEADERS)


def _result_page(request: Request, *, ok: bool, message: str,
                 burned: bool = True) -> HTMLResponse:
    # `burned` controls the closing hint's claim about the link itself
    # (approve_result.html): true for every normal resolution (the token
    # was consumed on the way in), false only for the M6 case below, where
    # nothing was claimed and the link is deliberately still live.
    return templates.TemplateResponse(
        request, "approve_result.html",
        {"ok": ok, "message": message, "burned": burned},
        headers=_SECURITY_HEADERS)


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
        "market_open": False, "rationale": "", "diff_rows": [],
        "stale": False,
    }
    if kind == "strategy_revision":
        # I3: a revision confirm page has no order to price -- the template
        # gates the whole price/market block on `kind == "order"` (see
        # approve_confirm.html) so it's never rendered here at all, rather
        # than rendering a false "Market closed" line above a Confirm
        # button with no evidence of *what* is being confirmed. Rationale +
        # a diff (not a live re-generate -- see reviews.py's `_revision_diff`
        # docstring for why a resolved-row diff is shown historically; here
        # the row is still pending, but a link visitor has no session to
        # re-check the file against, so the diff shown is built from the
        # proposal's own frozen old_yaml/new_yaml, labelled "Base (at
        # proposal)" in the template rather than "Current" -- honest about
        # what the left column actually is for a visitor with no session)
        # are what a human needs to decide, and are now what's shown
        # instead.
        ctx["side"] = "Strategy revision"
        ctx["amount_label"] = f"Proposed change to {row['strategy_id']}"
        snapshot = json.loads(row["snapshot"]) if row["snapshot"] else {}
        ctx["rationale"] = snapshot.get("rationale", "")
        ctx["diff_rows"] = split_diff_rows(
            snapshot.get("old_yaml", ""), snapshot.get("new_yaml", ""))
        # Minor 4: this route computes staleness too now, the same
        # current-file check `_revision_diff` uses for the in-app card
        # (reviews.py's `current_strategy_yaml`) -- an unauthenticated
        # link visitor has even less context than the in-app user, so
        # silently omitting the warning here (as before) was worse, not
        # better. Doesn't change what `diff_rows` is built from -- still
        # the frozen old_yaml/new_yaml (see the docstring above) -- only
        # whether the warning paragraph renders.
        ctx["stale"] = (current_strategy_yaml(c, row["strategy_id"])
                        != snapshot.get("old_yaml", ""))
        # Same proposer/new-strategy/safety-warning flags the in-app review
        # card computes (reviews.py's revision_view) -- this unauthenticated
        # one-click confirm page is, if anything, the MORE important place
        # to show them: a visitor here may never have seen the in-app card
        # at all.
        ctx.update(revision_view(row["source"], snapshot))
        ctx["diff_left_label"] = ("— (new strategy)" if ctx["is_new"]
                                  else "Base (at proposal)")
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


def _parse_review_id(review_id: str) -> int | None:
    # M1: the path param is a plain `str`, parsed here instead of via
    # FastAPI's own `{review_id:int}` converter -- that converter fails a
    # non-numeric segment with a raw 422 JSON error, the one response on
    # this whole surface that would (a) distinguish itself from every other
    # invalid-link case and (b) hand a visitor a bare framework error page.
    # `/a/abc` must land on exactly the same uniform invalid page as a
    # wrong token or an expired one.
    try:
        return int(review_id)
    except ValueError:
        return None


@router.get(PREFIX + "/{review_id}", response_class=HTMLResponse)
def confirm(request: Request, review_id: str, k: str = "") -> HTMLResponse:
    c = request.app.state.holder.get()
    rid = _parse_review_id(review_id)
    if rid is None:
        return _invalid_page(request)
    row = c.queue.validate_token(rid, k)
    if row is None:
        return _invalid_page(request)
    context = _confirm_context(c, row)
    context["token"] = k
    return templates.TemplateResponse(
        request, "approve_confirm.html", context, headers=_SECURITY_HEADERS)


def _resolve(request: Request, review_id: str, token: str, *, reject: bool) -> HTMLResponse:
    c = request.app.state.holder.get()
    rid = _parse_review_id(review_id)
    if rid is None:
        return _invalid_page(request)

    # M6: an order-kind row with no executable intent (a malformed row --
    # legitimate rows always have one, see ReviewQueue.add's caller in
    # sentinel.py) can never succeed either way `approve()` is called: from
    # `_approve_order`'s own guard. That's a real error worth telling the
    # user about honestly, not something to silently swallow -- but it must
    # be caught here, BEFORE the token is burned, specifically so the link
    # doesn't get eaten by an approval that could never have executed
    # anything; a reject, by contrast, never touches intent and is left to
    # proceed normally. Read-only (validate_token, not consume_token) --
    # nothing is claimed or acted on by this check.
    if not reject:
        preview = c.queue.validate_token(rid, token)
        if preview is not None and preview["kind"] == "order" and not preview["intent"]:
            return _result_page(
                request, ok=False, burned=False,
                message=(f"Review #{rid} has no executable order attached; "
                         "nothing was done. Resolve it from the app."))

    # Burns the token BEFORE acting (see ReviewQueue.consume_token's
    # docstring for why that ordering specifically matters for the
    # RevisionValidationError case below): whatever happens next, this
    # link is dead the moment it's submitted -- a failed execution must
    # push the user back into the app, not invite a retry via the same link.
    row = c.queue.consume_token(rid, token)
    if row is None:
        return _invalid_page(request)
    review_id = rid

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
def approve(request: Request, review_id: str, k: str = Form("")) -> HTMLResponse:
    return _resolve(request, review_id, k, reject=False)


@router.post(PREFIX + "/{review_id}/reject", response_class=HTMLResponse)
def reject(request: Request, review_id: str, k: str = Form("")) -> HTMLResponse:
    return _resolve(request, review_id, k, reject=True)
