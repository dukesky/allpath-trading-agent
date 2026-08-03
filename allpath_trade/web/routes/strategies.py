from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from allpath_trade.strategy.loader import is_valid_strategy_id
from allpath_trade.strategy.model import RuleState, StrategyDoc
from allpath_trade.web.routes.dashboard import error_redirect, nav_context
from allpath_trade.web.templating import templates

router = APIRouter()

# A strategy with a long edit history would otherwise render an unbounded
# table on every detail-page load; the store keeps every row (other
# callers, e.g. action-tool tests, rely on that), so the cap lives here.
_MAX_VERSIONS_SHOWN = 20


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
        "error": message, **nav_context(c)}, status_code=404)


@router.get("/strategies", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    c = request.app.state.holder.get()
    errors: list[str] = []
    docs = c.strategies.load_all(status=None, errors=errors)
    return templates.TemplateResponse(request, "strategies.html", {
        "page": "strategies", "docs": docs, "errors": errors,
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
    return templates.TemplateResponse(request, "strategy_detail.html", {
        "page": "strategies", "doc": doc,
        "yaml_text": path.read_text() if path.exists() else "",
        "versions": c.strategies.versions(strategy_id)[:_MAX_VERSIONS_SHOWN],
        "error": request.query_params.get("error"), **nav_context(c)})


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
