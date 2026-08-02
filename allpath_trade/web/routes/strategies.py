from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from allpath_trade.strategy.loader import is_valid_strategy_id
from allpath_trade.strategy.model import RuleState, StrategyDoc
from allpath_trade.web.routes.dashboard import nav_context
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
    if not is_valid_strategy_id(strategy_id):
        raise HTTPException(status_code=404, detail="not found")
    c = request.app.state.holder.get()
    # A strategy whose YAML is missing, unparseable, or fails validation is
    # simply absent from load_all's result (errors are collected, not
    # raised) -- it 404s here rather than the page crashing on a bad file.
    doc = _find_doc(c, strategy_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="not found")
    path = c.strategies.directory / f"{strategy_id}.yaml"
    return templates.TemplateResponse(request, "strategy_detail.html", {
        "page": "strategies", "doc": doc,
        "yaml_text": path.read_text() if path.exists() else "",
        "versions": c.strategies.versions(strategy_id)[:_MAX_VERSIONS_SHOWN],
        "error": request.query_params.get("error"), **nav_context(c)})


@router.post("/strategies/{strategy_id}/rules/{rule_id}/rearm")
def rearm(request: Request, strategy_id: str, rule_id: str) -> Response:
    if not is_valid_strategy_id(strategy_id):
        raise HTTPException(status_code=404, detail="not found")
    c = request.app.state.holder.get()
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
        return RedirectResponse(f"/strategies?error={quote(message)}", status_code=303)
    if not any(r.id == rule_id for r in doc.rules):
        message = f"Not processed: rule '{rule_id}' not found in strategy '{strategy_id}'"
        return RedirectResponse(
            f"/strategies/{strategy_id}?error={quote(message)}", status_code=303)
    c.strategies.set_rule_state(strategy_id, rule_id, RuleState.ARMED)
    return RedirectResponse(f"/strategies/{strategy_id}", status_code=303)
