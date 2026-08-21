from __future__ import annotations

import difflib
import itertools
import json
import time

import yaml
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from allpath_trade.execution import ExecutionError
from allpath_trade.store.reviews import ReviewError, RevisionValidationError
from allpath_trade.web.account_ctx import bundle, bundle_for
from allpath_trade.web.deps import components
from allpath_trade.web.routes import dashboard as dashboard_route
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
    # fixed; re-fetching a quote for it would only be misleading). Callers
    # only ever pass the pending `items` list now (M7: a resolved-only
    # `recent` list is a guaranteed no-op through this same branch, so that
    # call was dropped from the route below).
    #
    # I1: one shared QUOTES_BUDGET_SECONDS deadline for the whole loop --
    # the same pattern dashboard.py's `quote_deadline` uses -- rather than
    # a fresh BROKER_TIMEOUT_SECONDS allowance per row. Measured: 7 pending
    # rows against a stalled data source cost 22.3s serially (7 *
    # BROKER_TIMEOUT_SECONDS) without this; rows past the deadline omit
    # price context gracefully instead of each paying their own timeout.
    deadline = time.monotonic() + dashboard_route.QUOTES_BUDGET_SECONDS
    for item in items:
        if (item["kind"] == "order" and item["status"] == "pending"
                and time.monotonic() < deadline):
            item["price_context"] = order_price_context(
                c.data, item["ticker"], item["snapshot"], item["intent"])
        else:
            item["price_context"] = None


_DIFF_CONTEXT = 2  # lines of unchanged context kept on each side of a change


def split_diff_rows(old: str, new: str) -> list[dict]:
    """Builds side-by-side diff rows from `old`/`new` text for the split
    view shared by all three strategy_revision diff renders (pending and
    resolved review cards -- _review_card.html -- and the approve-link
    confirm page -- approve_confirm.html). Each row pairs a left (old) and
    right (new) cell so the two sides lay out as matching table columns
    instead of a scrolling unified diff (this also sidesteps the old
    unified-diff renderer's double-spacing bug: that view put each line in
    its own `display:block` <span> *and* a literal template newline right
    after it -- inside a <pre>, which preserves whitespace, so every diff
    line got two line breaks instead of one. A table has no such
    footgun -- each line is exactly one <tr>, full stop).

    `difflib.SequenceMatcher.get_opcodes()` gives the alignment: an
    'equal' run becomes context rows (class `ctx`) on both sides;
    'replace'/'delete'/'insert' are all handled as one case -- pair the
    two segments position-by-position with `zip_longest`, tinting a
    present old line `del` and a present new line `add`; whichever side
    runs out first pads with an empty cell (class `empty`) so the row
    never goes ragged. Long equal runs are collapsed to a single `gap`
    row via `_collapse_context`, keeping `_DIFF_CONTEXT` lines adjacent to
    each surrounding change -- a proposal that only touches one rule in a
    40-line strategy file shouldn't force scrolling past the other 35
    unchanged lines to find it. A diff with no changes at all collapses to
    one explanatory row rather than a wall of context."""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    opcodes = difflib.SequenceMatcher(
        a=old_lines, b=new_lines, autojunk=False).get_opcodes()

    # Two empty texts produce ZERO opcodes (not one 'equal'), so check both
    # shapes -- otherwise the documented "No changes" row silently becomes an
    # empty table body.
    if not opcodes or (len(opcodes) == 1 and opcodes[0][0] == "equal"):
        return [{"kind": "none", "text": "No changes"}]

    rows: list[dict] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for a_line, b_line in zip(old_lines[i1:i2], new_lines[j1:j2]):
                rows.append({"kind": "row", "ctx": True,
                             "left": {"cls": "ctx", "text": a_line},
                             "right": {"cls": "ctx", "text": b_line}})
        else:
            for a_line, b_line in itertools.zip_longest(
                    old_lines[i1:i2], new_lines[j1:j2]):
                left = ({"cls": "del", "text": a_line} if a_line is not None
                        else {"cls": "empty", "text": ""})
                right = ({"cls": "add", "text": b_line} if b_line is not None
                         else {"cls": "empty", "text": ""})
                rows.append({"kind": "row", "ctx": False, "left": left, "right": right})

    return _collapse_context(rows)


def _collapse_context(rows: list[dict]) -> list[dict]:
    """Collapses an interior run of consecutive context rows down to
    `_DIFF_CONTEXT` lines on each side of a change, replacing the hidden
    middle with a single `gap` row. A run touching the very start or end
    of the diff (no change on that side) only keeps the edge nearest a
    change -- there is no change on the other side to anchor context to."""
    out: list[dict] = []
    i, n = 0, len(rows)
    while i < n:
        if not rows[i]["ctx"]:
            out.append(rows[i])
            i += 1
            continue
        j = i
        while j < n and rows[j]["ctx"]:
            j += 1
        run = rows[i:j]
        head = _DIFF_CONTEXT if i > 0 else 0
        tail = _DIFF_CONTEXT if j < n else 0
        if len(run) <= head + tail:
            out.extend(run)
        else:
            out.extend(run[:head])
            hidden = len(run) - head - tail
            out.append({"kind": "gap",
                        "text": f"… {hidden} unchanged line{'s' if hidden != 1 else ''}"})
            if tail:
                out.extend(run[-tail:])
        i = j
    return out


def _yaml_top_fields(text: str) -> dict | None:
    """Best-effort read of a strategy YAML document's top-level mapping,
    used only by `revision_view` below to compute the safety-warning
    flags -- never to validate the document (that's `parse_strategy_text`'s
    job, already done before a chat/reflection proposal ever reaches the
    queue). Returns the raw mapping on success, or `None` for anything that
    doesn't cleanly parse as a mapping (empty text, a YAML syntax error, a
    scalar/list document) -- `revision_view` treats `None` as "unknown", so
    a proposal whose base/new text can't be read this way just skips the
    flag it needs that text for, rather than crashing the page or claiming
    a false positive/negative."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return raw if isinstance(raw, dict) else None


def revision_view(source: str, snapshot: dict) -> dict:
    """Proposer/new-strategy/safety-warning flags shared by the in-app
    review card (_review_card.html) and the unauthenticated approve-link
    confirm page (approve_confirm.html) -- one function so the two surfaces
    can never disagree about when a warning should show (imported by
    web/routes/approve.py; see its own docstring for why that import
    direction, not the reverse, is the one with no cycle).

    Spec §① (2026-08-12-chat-strategy-proposals-design.md): a chat proposal
    is not frozen out of touching `authorization`/`status` the way a
    reflection proposal is (`apply_revision_factory` enforces that at
    approval time) -- so a chat proposal that flips a strategy to
    `authorization: auto`, or takes it out of `active`, must be flagged
    loudly on the card *before* the human clicks Approve, not discovered
    after. Both flags are computed by parsing YAML text (a plain
    `yaml.safe_load` + dict lookup via `_yaml_top_fields`, deliberately NOT
    a full `StrategyDoc` validation) so a proposal that fails full schema
    validation for an unrelated reason still renders its diff with the
    flags simply defaulting to `False` -- never a 500.

    `auth_becomes_auto`: new `authorization` is `auto`, and either this is
    a brand-new strategy (spec §②: no prior authorization to compare
    against -- any new `auto` strategy is worth flagging) or the base
    strategy's authorization wasn't already `auto` (approving a no-op
    re-affirmation of an existing `auto` strategy isn't a new risk).

    `status_regresses` (the Task 4 carried finding): only meaningful for an
    existing strategy (a new strategy was never `active` to begin with, so
    there's nothing to "take out of" it) -- the base status was `active`
    and the new status is a definite, successfully-parsed non-`active`
    value. A new-status parse failure intentionally does NOT count as a
    regression: this function only ever asserts what it could positively
    confirm from the text.

    `lands_as_draft` (spec §②'s carried gap): the proposed `status` is
    literally `draft` (its schema default -- see `_yaml_top_fields`'s
    `.get("status", "draft")` above) -- true for BOTH a brand-new strategy
    left at its default and an existing strategy explicitly proposed into
    `draft`. Either way the strategy won't be monitored the moment this is
    approved, which is easy to miss next to the diff -- the card/confirm
    page render one hint line so the approver knows to go Activate it
    afterward. Deliberately not gated on `is_new` (a landing existing
    strategy needs the same hint) and, like the other two flags, silently
    `False` on an unparseable `new_yaml` rather than guessing."""
    # Same fallback store/reviews.py's `_approve_revision` uses for a
    # pre-fix row that predates the `is_new` snapshot key: derive it from
    # `old_yaml == ""` rather than assuming False, so this stays consistent
    # with what approving the row would actually do.
    old_yaml = snapshot.get("old_yaml", "")
    is_new = bool(snapshot.get("is_new", old_yaml == ""))
    new_fields = _yaml_top_fields(snapshot.get("new_yaml", ""))
    new_auth = new_fields.get("authorization", "confirm") if new_fields else None
    new_status = new_fields.get("status", "draft") if new_fields else None

    base_auth = None
    base_status = None
    if not is_new:
        base_fields = _yaml_top_fields(old_yaml)
        if base_fields is not None:
            base_auth = base_fields.get("authorization", "confirm")
            base_status = base_fields.get("status", "draft")

    auth_becomes_auto = new_auth == "auto" and (is_new or base_auth != "auto")
    status_regresses = (not is_new and base_status == "active"
                        and new_status not in (None, "active"))
    lands_as_draft = new_status == "draft"

    return {
        "proposer": source,
        "is_new": is_new,
        "auth_becomes_auto": bool(auth_becomes_auto),
        "status_regresses": bool(status_regresses),
        "lands_as_draft": bool(lands_as_draft),
    }


def current_strategy_yaml(c, strategy_id: str) -> str:
    """The strategy file's CURRENT on-disk text (`""` if it doesn't exist),
    read fresh at render time. Shared by `_revision_diff` below and by
    web/routes/approve.py's confirm-page context, so the two surfaces
    compute "does the file still match what this proposal was drafted
    against" -- i.e. staleness -- against the exact same read, not two
    independently-written file checks that could quietly drift apart."""
    path = c.strategies.directory / f"{strategy_id}.yaml"
    return path.read_text() if path.exists() else ""


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
    of only after. Labelled `Current`/`Proposed` -- the left column really
    is what's on disk right now, not a frozen snapshot."""
    snapshot = item["snapshot"]
    strategy_id = item["strategy_id"]
    current_yaml = current_strategy_yaml(c, strategy_id)
    # Minor 1: labelled off the file's CURRENT reality (empty = nothing on
    # disk right now), not off the proposal's own recorded `is_new` flag --
    # `is_new` is frozen at proposal time and goes stale itself the moment
    # some OTHER change (a sibling proposal, a manual write) creates the
    # file afterward. Rendering "— (new strategy)" over rows that actually
    # came from a real, now-existing file would bury that file's content
    # under a label saying there's nothing there. The `stale` flag right
    # below already tells the user their proposal's base has moved.
    left_label = "— (new strategy)" if current_yaml == "" else "Current"
    return {
        "rows": split_diff_rows(current_yaml, snapshot["new_yaml"]),
        "stale": current_yaml != snapshot["old_yaml"],
        "left_label": left_label, "right_label": "Proposed",
    }


def _attach_revision_diffs(c, items: list[dict]) -> None:
    for item in items:
        if item["kind"] != "strategy_revision":
            continue
        # Proposer chip / New-strategy badge / auto+status warning flags --
        # a pure function of the row's own source+snapshot, so it applies
        # identically whether the row is still pending or already resolved
        # (a resolved row's card only actually renders the warnings while
        # pending -- see _review_card.html -- but the flags themselves are
        # cheap and harmless to compute either way).
        item.update(revision_view(item["source"], item["snapshot"]))
        if item["status"] == "pending":
            # Live re-check against the current file -- see _revision_diff.
            item["revision_diff"] = _revision_diff(c, item)
        else:
            # A resolved row's outcome is already fixed (applied or
            # rejected); re-reading the file at render time would either
            # be redundant (approved: the file now matches new_yaml, so a
            # live diff is empty) or misleading (rejected: unrelated later
            # edits could make it look like something changed because of
            # this proposal). The frozen old/new the agent actually
            # proposed against is the honest thing to show for a resolved
            # row -- labelled `Base (at proposal)` so it doesn't read as
            # "current", which it may no longer be (or, for a new-strategy
            # proposal, the same "— (new strategy)" label _revision_diff
            # uses -- there was never a "base" to speak of).
            snapshot = item["snapshot"]
            left_label = ("— (new strategy)" if snapshot.get("is_new")
                         else "Base (at proposal)")
            item["revision_diff"] = {
                "rows": split_diff_rows(snapshot["old_yaml"], snapshot["new_yaml"]),
                "stale": False,
                "left_label": left_label, "right_label": "Proposed",
            }


@router.get("/reviews", response_class=HTMLResponse)
def reviews(request: Request) -> HTMLResponse:
    # shadow-dual-active T5: this list is the CURRENT account's rows only
    # (spec: "Pending page lists the CURRENT account's rows"); the other
    # account's own pending count surfaces on the switcher chip instead
    # (nav_context's other_account_pending), not by merging both accounts'
    # rows into one list here.
    b = bundle(request)
    c = components(request)
    # One query, then filter, then slice: `list(None)` already carries the
    # pending rows, so a second `list("pending")` query is redundant. And the
    # slice-then-filter order matters — with >=20 pending rows, slicing
    # first would take only pending rows off the top and filtering would
    # then empty the "Resolved" section even when resolved rows exist.
    all_items = [_decorate(r) for r in b.queue.list(None)]
    items = [r for r in all_items if r["status"] == "pending"]
    recent = [r for r in all_items if r["status"] != "pending"][:20]
    _attach_revision_diffs(b, items)
    _attach_revision_diffs(b, recent)
    _attach_price_context(c, items)
    return templates.TemplateResponse(request, "reviews.html", {
        "page": "reviews", "items": items, "recent": recent,
        "error": request.query_params.get("error"),
        "notice": request.query_params.get("notice"),
        **nav_context(request)})


def _back_to_reviews(error: str | None = None) -> RedirectResponse:
    # Shared idiom -- see dashboard.py's error_redirect docstring for why
    # this redirects instead of returning a fragment directly.
    return error_redirect("/reviews", error)


def _back_to_reviews_ok(message: str) -> RedirectResponse:
    return notice_redirect("/reviews", message)


def _echo_resolution(request: Request, account: str, review_id: int,
                     row_source: str, summary: str) -> None:
    # Only chat-sourced reviews have a live conversation to report back
    # into; a sentinel-triggered row has no ChatService turn waiting on it.
    #
    # shadow-dual-active T5 (row-bound): the receipt goes into the ROW's own
    # account's conversation, via that account's own ChatService -- an
    # approval resolved from the paper view against a shadow row must not
    # echo into paper's chat transcript (or vice versa). `chat_services` is
    # keyed by account (web/app.py's create_app); a missing/unrecognized key
    # degrades to no echo rather than raising, same as the pre-existing
    # `getattr(..., None)` guard did for "no chat service configured at
    # all" in tests.
    #
    # `summary` isn't fully trusted text: the reject path folds in a
    # user-supplied `note` and the execution-failure path folds in a raw
    # broker exception message, either of which could contain a forged
    # marker trying to impersonate a real system line. `note_resolution`
    # fences the whole line (fence_external) before it reaches the model, so
    # that's handled downstream rather than here.
    services = getattr(request.app.state, "chat_services", None)
    service = services.get(account) if services else None
    if service is not None and row_source == "chat":
        service.note_resolution(f"You resolved #{review_id}. Result: {summary}")


def _locate_or_404(request: Request, review_id: int):
    """Row-bound approval (shadow-dual-active T5, spec: "批准永远作用于该
    行的账户,与当前界面选择无关"): resolves `review_id` to its OWN
    account -- which may not be the account the browser's cookie currently
    has selected -- via `ReviewQueue.locate` (an unscoped, whole-table
    lookup, see its own docstring), then hands back that account's own
    bundle. Every mutating action below (approve/reject) acts through THIS
    bundle's queue, never `bundle(request)` (the current VIEW's account).

    Returns `(account, bundle)`, or `None` if the id doesn't exist at all --
    callers translate that into the same "Not processed: review N not
    found" message `ReviewQueue.get`'s `ReviewError` used to produce, so the
    user-facing behavior for a missing id is unchanged."""
    c = components(request)
    located = c.queue.locate(review_id)
    if located is None:
        return None
    account, _row = located
    return account, bundle_for(c, account)


@router.post("/reviews/{review_id}/approve")
def approve(request: Request, review_id: int) -> Response:
    located = _locate_or_404(request, review_id)
    if located is None:
        return _back_to_reviews(f"Not processed: review {review_id} not found")
    account, b = located
    try:
        row = b.queue.get(review_id)
    except ReviewError as exc:
        return _back_to_reviews(f"Not processed: {exc}")
    row_source, kind = row["source"], row["kind"]
    try:
        result = b.queue.approve(review_id)
    except RevisionValidationError as exc:
        # kind-branch HARD PREREQ (Task 3): approve() returns None for
        # strategy_revision rows -- ReviewError below would catch this too
        # (RevisionValidationError subclasses it), but this must be caught
        # first: ReviewQueue._approve_revision already rolled the row back
        # to "pending" before raising (see the loud comment there), so the
        # message here has to say that, not the generic "not processed".
        _echo_resolution(request, account, review_id, row_source,
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
        _echo_resolution(request, account, review_id, row_source,
                         f"execution failed: {exc}")
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
                  + b.strategies.rearm_warning(row['strategy_id']))
        _echo_resolution(request, account, review_id, row_source,
                         f"revision applied to {row['strategy_id']}")
        return _back_to_reviews_ok(message)

    if not result.submitted:
        reasons = "; ".join(result.decision.reasons)
        _echo_resolution(request, account, review_id, row_source,
                         f"blocked by the risk gate ({reasons})")
        return _back_to_reviews(f"Rejected by the risk gate: {reasons}")
    _echo_resolution(request, account, review_id, row_source, "order submitted")
    return _back_to_reviews()


@router.post("/reviews/{review_id}/reject")
def reject(request: Request, review_id: int, note: str = Form("")) -> Response:
    located = _locate_or_404(request, review_id)
    if located is None:
        return _back_to_reviews(f"Not processed: review {review_id} not found")
    account, b = located
    try:
        row_source = b.queue.get(review_id)["source"]
        b.queue.reject(review_id, note)
    except ReviewError as exc:
        return _back_to_reviews(f"Not processed: {exc}")
    summary = f"rejected ({note})" if note else "rejected"
    _echo_resolution(request, account, review_id, row_source, summary)
    return _back_to_reviews()
