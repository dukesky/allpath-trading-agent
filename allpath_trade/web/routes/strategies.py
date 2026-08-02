from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from allpath_trade.strategy.loader import is_valid_strategy_id
from allpath_trade.strategy.model import RuleState
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.templating import templates

router = APIRouter()


@router.get("/strategies", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    c = request.app.state.holder.get()
    errors: list[str] = []
    docs = c.strategies.load_all(status=None, errors=errors)
    return templates.TemplateResponse(request, "strategies.html", {
        "page": "strategies", "docs": docs, "errors": errors, **nav_context(c)})


@router.get("/strategies/{strategy_id}", response_class=HTMLResponse)
def detail(request: Request, strategy_id: str) -> HTMLResponse:
    if not is_valid_strategy_id(strategy_id):
        raise HTTPException(status_code=404, detail="not found")
    c = request.app.state.holder.get()
    # A strategy whose YAML is missing, unparseable, or fails validation is
    # simply absent from load_all's result (errors are collected, not
    # raised) -- it 404s here rather than the page crashing on a bad file.
    docs = [d for d in c.strategies.load_all(status=None, errors=[])
            if d.id == strategy_id]
    if not docs:
        raise HTTPException(status_code=404, detail="not found")
    path = c.strategies.directory / f"{strategy_id}.yaml"
    return templates.TemplateResponse(request, "strategy_detail.html", {
        "page": "strategies", "doc": docs[0],
        "yaml_text": path.read_text() if path.exists() else "",
        "versions": c.strategies.versions(strategy_id), **nav_context(c)})


@router.post("/strategies/{strategy_id}/rules/{rule_id}/rearm")
def rearm(request: Request, strategy_id: str, rule_id: str) -> Response:
    if not is_valid_strategy_id(strategy_id):
        raise HTTPException(status_code=404, detail="not found")
    c = request.app.state.holder.get()
    c.strategies.set_rule_state(strategy_id, rule_id, RuleState.ARMED)
    return RedirectResponse(f"/strategies/{strategy_id}", status_code=303)
