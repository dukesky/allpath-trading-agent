from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from allpath_trade.config import Settings
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
    # At most the last 4 characters are ever disclosed, and only when the
    # value is long enough that doing so doesn't reveal the whole thing.
    # Leading characters are deliberately never shown: for an SMTP app
    # password (a flat run of random letters) a leading slice is just as
    # secret as a trailing one, so there is no "safe prefix" to expose.
    if not value:
        return ""
    if len(value) > 4:
        return f"{'•' * 8}{value[-4:]}"
    return "•" * 8


def _validation_message(exc: ValidationError) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return "Could not save: " + "; ".join(parts)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "", note: str = "") -> HTMLResponse:
    c = request.app.state.holder.get()
    s = c.settings
    return templates.TemplateResponse(request, "settings.html", {
        "page": "settings", "s": s, "saved": bool(saved), "note": note, "error": "",
        "masks": {f: _mask(str(getattr(s, f, ""))) for f in SECRET_FIELDS},
        **nav_context(c)})


@router.post("/settings")
async def save(request: Request) -> Response:
    form = await request.form()
    holder = request.app.state.holder
    current = holder.get().settings

    updates: dict[str, str] = {}
    for field in BOOLEAN_FIELDS:
        updates[field] = "true" if form.get(field) else "false"
    for field in PLAIN_FIELDS:
        if field in form:
            updates[field] = str(form[field]).strip()
    for field in SECRET_FIELDS:
        value = str(form.get(field, "")).strip()
        if value:  # blank means "keep what is stored"
            updates[field] = value
    # ALPACA_PAPER is deliberately absent: switching to real money should
    # require editing .env by hand, not a checkbox reachable from the LAN.

    # Validate the settings that *would* result before writing anything to
    # disk. A field typed `int` on `Settings` (e.g. sentinel_interval_minutes)
    # accepts arbitrary text in this plain HTML input; writing it to `.env`
    # unchecked and only discovering the type error inside `rebuild()` would
    # leave `.env` holding a value `Settings` can never load again, bricking
    # every future start (the settings page included, so there would be no
    # way back in short of hand-editing the file).
    candidate = current.model_dump()
    candidate.update(updates)
    try:
        Settings(_env_file=None, **candidate)
    except ValidationError as exc:
        c = holder.get()
        return templates.TemplateResponse(request, "settings.html", {
            "page": "settings", "s": current, "saved": False, "note": "",
            "error": _validation_message(exc),
            "masks": {f: _mask(str(getattr(current, f, ""))) for f in SECRET_FIELDS},
            **nav_context(c)}, status_code=400)

    store = holder.store()
    for field, value in updates.items():
        store.set(field.upper(), value)
    holder.rebuild()
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
    # The redirect only ever carries a fixed, known token -- never freeform
    # text -- so a crafted `?note=...` link can't make this page render
    # arbitrary copy. The actual message lives in one place: the template.
    note = "email-sent" if ok else "email-failed"
    return RedirectResponse(f"/settings?note={note}", status_code=303)


@router.post("/settings/reset-token")
def reset_token(request: Request) -> Response:
    token = secrets.token_urlsafe(24)
    request.app.state.holder.store().set("WEB_TOKEN", token)
    request.app.state.holder.rebuild()
    print(f"[allpath-trade] new access token: {token}")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response
