from __future__ import annotations

import secrets
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from allpath_trade.config import SettingsStore
from allpath_trade.web.auth import COOKIE
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.templating import templates

router = APIRouter()

# Plain values: rendered, editable, rewritten on every save.
PLAIN_FIELDS = ["llm_provider", "chat_model", "review_model", "memory_model",
                "smtp_host", "smtp_port", "smtp_user", "smtp_from", "notify_to",
                "sentinel_interval_minutes", "context_budget_tokens"]

# Checkbox values: a browser omits an unchecked box from the form body
# entirely, so these need explicit "present -> true, absent -> false"
# handling rather than the "only touch what's present" rule PLAIN_FIELDS
# uses -- otherwise they could never be turned off again.
BOOLEAN_FIELDS = ["daily_consolidation", "consolidate_after_chat"]

# Secret values: never rendered back. A blank field means "leave it alone".
SECRET_FIELDS = ["openrouter_api_key", "openai_api_key", "anthropic_api_key",
                 "alpaca_api_key", "alpaca_secret_key", "smtp_password"]


def _mask(value: str) -> str:
    if not value:
        return ""
    return f"{value[:6]}{'•' * 8}{value[-4:]}" if len(value) > 12 else "•" * 8


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "", note: str = "") -> HTMLResponse:
    c = request.app.state.holder.get()
    s = c.settings
    return templates.TemplateResponse(request, "settings.html", {
        "page": "settings", "s": s, "saved": bool(saved), "note": note,
        "masks": {f: _mask(str(getattr(s, f, ""))) for f in SECRET_FIELDS},
        **nav_context(c)})


@router.post("/settings")
async def save(request: Request) -> Response:
    form = await request.form()
    store = SettingsStore()
    for field in BOOLEAN_FIELDS:
        store.set(field.upper(), "true" if form.get(field) else "false")
    for field in PLAIN_FIELDS:
        if field in form:
            store.set(field.upper(), str(form[field]).strip())
    for field in SECRET_FIELDS:
        value = str(form.get(field, "")).strip()
        if value:  # blank means "keep what is stored"
            store.set(field.upper(), value)
    # ALPACA_PAPER is deliberately absent: switching to real money should
    # require editing .env by hand, not a checkbox reachable from the LAN.
    request.app.state.holder.rebuild()
    # The in-flight turn (if any) already holds its own ChatService/AgentSession
    # object and keeps running against the configuration it captured -- this
    # doesn't touch it. Clearing the attribute only means the *next* call to
    # `_service()` builds a fresh ChatService against the just-rebuilt
    # Components, so the next turn picks up the new provider/model/key.
    request.app.state.chat = None
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/test-email")
def test_email(request: Request) -> Response:
    c = request.app.state.holder.get()
    # Notifier.send() swallows its own exceptions (a broken notifier must
    # never crash the caller) and reports the outcome only through its
    # return value -- that return value is what turns this from "a request
    # happened" into "the user learns whether it worked", which is the
    # whole point of a test button for a channel whose only other failure
    # mode is a notification that silently never arrives.
    ok = c.notifier.send(
        "AllPath Trade test",
        "This is a test notification. If you are reading it, "
        "email delivery works.")
    note = "Test email sent" if ok else "Test email failed — check SMTP settings and server logs"
    return RedirectResponse(f"/settings?note={quote(note)}", status_code=303)


@router.post("/settings/reset-token")
def reset_token(request: Request) -> Response:
    token = secrets.token_urlsafe(24)
    SettingsStore().set("WEB_TOKEN", token)
    request.app.state.holder.rebuild()
    print(f"[allpath-trade] new access token: {token}")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response
