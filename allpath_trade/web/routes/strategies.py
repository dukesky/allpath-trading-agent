from __future__ import annotations

import time

import yaml
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from allpath_trade.strategy.loader import (
    atomic_write_text,
    is_valid_strategy_id,
    parse_strategy_text,
)
from allpath_trade.strategy.model import RuleState, StrategyDoc, StrategyStatus
from allpath_trade.web.routes import dashboard as dashboard_route
from allpath_trade.web.routes.dashboard import (
    cached_quote,
    error_redirect,
    nav_context,
    summarize_strategy,
)
from allpath_trade.web.templating import templates

router = APIRouter()

# A strategy with a long edit history would otherwise render an unbounded
# table on every detail-page load; the store keeps every row (other
# callers, e.g. action-tool tests, rely on that), so the cap lives here.
_MAX_VERSIONS_SHOWN = 20

# The only lifecycle moves the web UI is allowed to make. Anything else
# (draft->paused, active->draft, archived->anything, ...) is out of scope
# for a single-click page control -- the sentinel-monitoring implication of
# each of these three is exactly the "activate arms it / pause disarms it /
# resume re-arms it" story the confirm dialog tells the user, and no other
# transition has an equally simple story to confirm.
_ALLOWED_STATUS_TRANSITIONS = {
    (StrategyStatus.DRAFT, StrategyStatus.ACTIVE),
    (StrategyStatus.ACTIVE, StrategyStatus.PAUSED),
    (StrategyStatus.PAUSED, StrategyStatus.ACTIVE),
}


def _lifecycle_chips(doc: StrategyDoc, has_pending: bool) -> list[str]:
    """Computed "what's actually happening right now" chips, shown next to
    the doc.status.value chip. Mirrors dashboard.summarize_strategy's alert
    logic (same RuleState.TRIGGERED check, same has_pending shape) rather
    than inventing a third way to answer "is anything running" -- the one
    difference is a count ("N triggered") instead of a flat "rule
    triggered" string, since this page has room to be specific."""
    triggered = sum(1 for r in doc.rules if r.state == RuleState.TRIGGERED)
    chips = []
    if triggered:
        chips.append(f"{triggered} triggered")
    if has_pending:
        chips.append("pending review")
    return chips


def _chips_by_id(c, docs: list[StrategyDoc]) -> dict[str, list[str]]:
    pending_strategy_ids = {row["strategy_id"] for row in c.queue.list("pending")}
    return {doc.id: _lifecycle_chips(doc, doc.id in pending_strategy_ids) for doc in docs}


def _cards_by_id(c, docs: list[StrategyDoc]) -> dict[str, dict]:
    """Price + signed day-change per card, reusing the dashboard's own
    cached-quote lookup and summarize_strategy shape rather than
    duplicating either (see dashboard.py's cached_quote/summarize_strategy
    docstrings) -- this page only ever reads `price`/`day_change_pct`/
    `price_class` out of the result, but summarize_strategy is a single
    pure function with no partial-field variant, so it's simpler to call it
    whole and let the template pick the fields it needs, same as the
    dashboard card does. No position/equity here: this page has no broker
    call of its own, and weight-vs-target isn't part of this card's design
    (see strategies.html)."""
    # Read at call time, not bound at import -- dashboard_route.py documents
    # QUOTES_BUDGET_SECONDS as a module attribute tests monkeypatch to a
    # tiny value (see its own docstring), which only works if callers look
    # it up through the module each time rather than capturing a copy of
    # today's value at import.
    quote_deadline = time.monotonic() + dashboard_route.QUOTES_BUDGET_SECONDS
    cards = {}
    for doc in docs:
        quote = (cached_quote(c.data, doc.position.ticker)
                 if time.monotonic() < quote_deadline else None)
        cards[doc.id] = summarize_strategy(doc, None, quote)
    return cards


def _find_doc(c, strategy_id: str) -> StrategyDoc | None:
    """Same convergence the detail route relies on: a missing file, a YAML
    syntax error, or a doc that fails validation all just don't appear in
    load_all's result -- there's no separate "exists but broken" state to
    special-case here."""
    for doc in c.strategies.load_all(status=None, errors=[]):
        if doc.id == strategy_id:
            return doc
    return None


def _not_found(request: Request, c, message: str) -> HTMLResponse:
    # Every other "resource not found" outcome on this page stays inside the
    # app chrome (this module's own rearm redirects, reviews.py's
    # _back_to_reviews) -- raising a bare HTTPException here was the one
    # path left that dropped straight out of the templates into an unstyled
    # `{"detail": "not found"}` JSON body instead of a page with nav and
    # styling. Rendered as the strategies index (with the real doc list, so
    # there's still somewhere to go) at a genuine 404 status, not a redirect
    # -- unlike the POST-only rearm cases below, a GET to a missing resource
    # should still answer 404, just from inside the app's own template.
    errors: list[str] = []
    docs = c.strategies.load_all(status=None, errors=errors)
    return templates.TemplateResponse(request, "strategies.html", {
        "page": "strategies", "docs": docs, "errors": errors,
        "chips": _chips_by_id(c, docs), "cards": _cards_by_id(c, docs),
        "error": message, **nav_context(c)}, status_code=404)


@router.get("/strategies", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    c = request.app.state.holder.get()
    errors: list[str] = []
    docs = c.strategies.load_all(status=None, errors=errors)
    return templates.TemplateResponse(request, "strategies.html", {
        "page": "strategies", "docs": docs, "errors": errors,
        "chips": _chips_by_id(c, docs), "cards": _cards_by_id(c, docs),
        "error": request.query_params.get("error"), **nav_context(c)})


@router.get("/strategies/{strategy_id}", response_class=HTMLResponse)
def detail(request: Request, strategy_id: str) -> HTMLResponse:
    c = request.app.state.holder.get()
    if not is_valid_strategy_id(strategy_id):
        return _not_found(request, c, "Strategy not found")
    # A strategy whose YAML is missing, unparseable, or fails validation is
    # simply absent from load_all's result (errors are collected, not
    # raised) -- it 404s here rather than the page crashing on a bad file.
    doc = _find_doc(c, strategy_id)
    if doc is None:
        return _not_found(request, c, "Strategy not found")
    path = c.strategies.directory / f"{strategy_id}.yaml"
    has_pending = any(row["strategy_id"] == strategy_id
                      for row in c.queue.list("pending"))
    return templates.TemplateResponse(request, "strategy_detail.html", {
        "page": "strategies", "doc": doc,
        "chips": _lifecycle_chips(doc, has_pending),
        "yaml_text": path.read_text() if path.exists() else "",
        "versions": c.strategies.versions(strategy_id)[:_MAX_VERSIONS_SHOWN],
        "error": request.query_params.get("error"), **nav_context(c)})


@router.post("/strategies/{strategy_id}/notify-email")
def toggle_notify_email(request: Request, strategy_id: str) -> Response:
    c = request.app.state.holder.get()
    if not is_valid_strategy_id(strategy_id):
        return _not_found(request, c, "Strategy not found")
    # Same existence check the detail page uses -- an id that is well-formed
    # but doesn't correspond to a real, loadable strategy has no page of its
    # own to redirect back to, so this 404s rather than bouncing to the
    # index with an error banner the way rearm (which always has a real
    # strategy to point at) does.
    if _find_doc(c, strategy_id) is None:
        return _not_found(request, c, "Strategy not found")
    path = c.strategies.directory / f"{strategy_id}.yaml"
    # Re-parse the raw file text (not the merged doc from _find_doc, which
    # has SQLite's runtime rule state folded in) -- writing that back would
    # bake a triggered rule's runtime state into the YAML, which is
    # supposed to live only in SQLite (see StrategyStore's docstring).
    current = parse_strategy_text(strategy_id, path.read_text())
    updated = current.model_copy(update={"notify_email": not current.notify_email})
    new_text = yaml.safe_dump(updated.model_dump(mode="json"), sort_keys=False,
                              allow_unicode=True)
    atomic_write_text(path, new_text)
    c.strategies.snapshot_version(updated, "notify_email toggled via web")
    return RedirectResponse(f"/strategies/{strategy_id}", status_code=303)


@router.post("/strategies/{strategy_id}/status")
def change_status(request: Request, strategy_id: str, to: str = Form("")) -> Response:
    c = request.app.state.holder.get()
    if not is_valid_strategy_id(strategy_id):
        return _not_found(request, c, "Strategy not found")
    # Same id-gate -> find-doc -> 404 shape as notify_email's toggle above,
    # mirrored exactly per this route's design brief -- an id that is
    # well-formed but doesn't correspond to a real, loadable strategy 404s
    # rather than bouncing to the index the way rearm's own "well-formed but
    # nonexistent" case does.
    doc = _find_doc(c, strategy_id)
    if doc is None:
        return _not_found(request, c, "Strategy not found")
    # `to` defaults to "" rather than being a required Form field -- a
    # missing field is a malformed request from this page's own form, not a
    # different failure mode than an invalid status value, so it goes
    # through the exact same error_redirect path below (a bare 422 JSON
    # body, like every other unhandled-validation-error page on this app,
    # would strand the user with no nav and no way back).
    if not to:
        return error_redirect(f"/strategies/{strategy_id}",
                              "Not processed: no target status given")
    try:
        target = StrategyStatus(to)
    except ValueError:
        return error_redirect(f"/strategies/{strategy_id}",
                              f"Not processed: {to!r} is not a valid status")
    # A real, well-formed strategy exists -- unlike the 404s above, it has
    # its own detail page to bounce back to with an error, the same pattern
    # rearm uses for "a real strategy, but a bogus rule_id".
    if (doc.status, target) not in _ALLOWED_STATUS_TRANSITIONS:
        return error_redirect(
            f"/strategies/{strategy_id}",
            f"Not processed: cannot change status from {doc.status.value} "
            f"to {target.value}")
    path = c.strategies.directory / f"{strategy_id}.yaml"
    # Same raw-file re-parse as notify_email's toggle above, for the same
    # reason: writing the SQLite-merged doc back would bake a triggered
    # rule's runtime state into the YAML.
    current = parse_strategy_text(strategy_id, path.read_text())
    updated = current.model_copy(update={"status": target})
    new_text = yaml.safe_dump(updated.model_dump(mode="json"), sort_keys=False,
                              allow_unicode=True)
    atomic_write_text(path, new_text)
    c.strategies.snapshot_version(updated, f"status changed to {target.value} via web")
    return RedirectResponse(f"/strategies/{strategy_id}", status_code=303)


@router.post("/strategies/{strategy_id}/rules/{rule_id}/rearm")
def rearm(request: Request, strategy_id: str, rule_id: str) -> Response:
    c = request.app.state.holder.get()
    if not is_valid_strategy_id(strategy_id):
        return _not_found(request, c, "Strategy not found")
    # set_rule_state is a raw upsert keyed on (strategy_id, rule_id) with no
    # foreign-key check -- without confirming the strategy and rule exist
    # first, a well-formed-but-nonexistent id (or a real strategy with a
    # made-up rule_id) would silently write an orphan row and still redirect
    # as if it worked.
    doc = _find_doc(c, strategy_id)
    if doc is None:
        # The detail page for this id would itself 404, so there's nowhere
        # in the strategy's own page to surface the message -- report it on
        # the index instead, matching reviews.py's "Not processed: {exc}".
        message = f"Not processed: strategy '{strategy_id}' not found"
        return error_redirect("/strategies", message)
    if not any(r.id == rule_id for r in doc.rules):
        message = f"Not processed: rule '{rule_id}' not found in strategy '{strategy_id}'"
        return error_redirect(f"/strategies/{strategy_id}", message)
    c.strategies.set_rule_state(strategy_id, rule_id, RuleState.ARMED)
    # F7: this is the success path -- calling the "error" helper with no
    # message worked (it degrades to a plain redirect when message is None)
    # but the name says the opposite of what just happened. A bare
    # RedirectResponse says exactly what it is, matching the plain-success
    # redirects elsewhere in the app (e.g. settings.py's reset-token).
    return RedirectResponse(f"/strategies/{strategy_id}", status_code=303)
