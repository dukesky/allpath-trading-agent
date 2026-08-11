from __future__ import annotations

import difflib
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from allpath_trade.execution import ExecutionError
from allpath_trade.store.reviews import ReviewError, RevisionValidationError
from allpath_trade.web.routes.dashboard import (
    error_redirect,
    nav_context,
    notice_redirect,
    order_price_context,
)
from allpath_trade.web.templating import templates

router = APIRouter()


def _decorate(row) -> dict:  # row: sqlite3.Row
    item = dict(row)
    for field in ("snapshot", "intent", "agent_analysis", "execution_result"):
        raw = item.get(field)
        item[field] = json.loads(raw) if raw else None
    # Never let the approval-link token hash (or expiry) ride along into a
    # template context -- nothing renders them today, but excluding them
    # here means a future template change can't accidentally start to,
    # matching Part A's "no token ever echoed back into HTML" constraint.
    item.pop("approval_token_hash", None)
    item.pop("token_expires_ts", None)
    return item


def _attach_price_context(c, items: list[dict]) -> None:
    # Part B: only pending order-kind rows get a price-context block -- a
    # strategy_revision has no trigger price to speak of, and a resolved
    # order's context is stale/moot by definition (the outcome is already
    # fixed; re-fetching a quote for it would only be misleading).
    for item in items:
        if item["kind"] == "order" and item["status"] == "pending":
            item["price_context"] = order_price_context(
                c.data, item["ticker"], item["snapshot"], item["intent"])
        else:
            item["price_context"] = None


def _diff_lines(diff_text: str) -> list[dict]:
    """Splits a unified-diff string into per-line dicts carrying the CSS
    class the template tints it with (`diff-add`/`diff-del` for `+`/`-`
    content lines -- the `+++`/`---` file-header lines and `@@` hunk
    headers get their own, unhighlighted classes so a real added/removed
    line is never confused with the `+++ file`/`--- file` header lines
    that also start with those characters)."""
    lines = []
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            cls = "diff-hdr"
        elif line.startswith("@@"):
            cls = "diff-hunk"
        elif line.startswith("+"):
            cls = "diff-add"
        elif line.startswith("-"):
            cls = "diff-del"
        else:
            cls = "diff-ctx"
        lines.append({"text": line, "cls": cls})
    return lines


def _revision_diff(c, item: dict) -> dict:
    """Regenerates a strategy_revision row's diff at render time against
    the file's CURRENT on-disk content, rather than trusting the diff
    recorded when the proposal was drafted (`item['snapshot']['diff']`).

    The stored diff is what the reflection agent SAW when it drafted the
    proposal; the regenerated one is what clicking Approve actually MEANS
    right now -- those diverge exactly when a sibling proposal (or a
    manual edit) has changed the file since, which is also the scenario
    `apply_revision_factory`'s base-match gate (allpath_trade/agent/
    reflection_tools.py) rejects at approval time. `stale` mirrors that
    same check (current text != the proposal's recorded base) so the card
    can warn about the failure *before* the user clicks Approve, instead
    of only after."""
    snapshot = item["snapshot"]
    strategy_id = item["strategy_id"]
    path = c.strategies.directory / f"{strategy_id}.yaml"
    current_yaml = path.read_text() if path.exists() else ""
    diff_text = "\n".join(difflib.unified_diff(
        current_yaml.splitlines(), snapshot["new_yaml"].splitlines(),
        fromfile=f"{strategy_id} (current)", tofile=f"{strategy_id} (proposed)",
        lineterm=""))
    return {"lines": _diff_lines(diff_text), "stale": current_yaml != snapshot["old_yaml"]}


def _attach_revision_diffs(c, items: list[dict]) -> None:
    for item in items:
        if item["kind"] != "strategy_revision":
            continue
        if item["status"] == "pending":
            # Live re-check against the current file -- see _revision_diff.
            item["revision_diff"] = _revision_diff(c, item)
        else:
            # A resolved row's outcome is already fixed (applied or
            # rejected); re-reading the file at render time would either
            # be redundant (approved: the file now matches new_yaml, so a
            # live diff is empty) or misleading (rejected: unrelated later
            # edits could make it look like something changed because of
            # this proposal). The historical diff the agent actually
            # proposed is the honest thing to show for a resolved row.
            item["revision_diff"] = {
                "lines": _diff_lines(item["snapshot"]["diff"]), "stale": False}


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
    _attach_revision_diffs(c, items)
    _attach_revision_diffs(c, recent)
    _attach_price_context(c, items)
    _attach_price_context(c, recent)
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
        #
        # Finding F2: the applier writes the revision's YAML verbatim and
        # never touches rule_states -- a rule that was already TRIGGERED
        # (fired, then the reflection agent proposed a tightened version of
        # that same rule id) stays TRIGGERED after this approval, silently,
        # unless surfaced here. `rearm_warning` never re-arms anything on
        # its own (re-arming could re-fire a stop against an already-sold
        # position) -- it only appends a note telling the user to do it by
        # hand if the rule should fire again.
        message = (f"Revision applied to {row['strategy_id']}."
                  + c.strategies.rearm_warning(row['strategy_id']))
        _echo_resolution(request, review_id, row_source,
                         f"revision applied to {row['strategy_id']}")
        return _back_to_reviews_ok(message)

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
