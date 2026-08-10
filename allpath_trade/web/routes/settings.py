from __future__ import annotations

import re
import secrets
import sys

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from allpath_trade.config import Settings, describe_validation_error
from allpath_trade.notify.email import EmailNotifier
from allpath_trade.notify.ntfy import NtfyNotifier
from allpath_trade.scheduler import reschedule_sentinel_job
from allpath_trade.web import models_catalog
from allpath_trade.web.auth import COOKIE
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.templating import templates

router = APIRouter()

# Plain values: rendered, editable, rewritten on every save.
PLAIN_FIELDS = ["llm_provider", "chat_model", "review_model", "memory_model",
                "smtp_host", "smtp_port", "smtp_user", "smtp_from", "notify_to",
                "ntfy_url", "sentinel_interval_minutes", "context_budget_tokens"]

# Checkbox values: a browser omits an unchecked box from the form body
# entirely, so these need explicit "present -> true, absent -> false"
# handling rather than the "only touch what's present" rule PLAIN_FIELDS
# uses -- otherwise they could never be turned off again.
BOOLEAN_FIELDS = ["daily_consolidation", "consolidate_after_chat"]

# Secret values: never rendered back. A blank field means "leave it alone".
SECRET_FIELDS = ["openrouter_api_key", "openai_api_key", "anthropic_api_key",
                 "alpaca_api_key", "alpaca_secret_key", "smtp_password"]

# Gmail displays app passwords as "abcd efgh ijkl mnop"; pasting that
# verbatim used to store the spaces and fail SMTP auth with an opaque 535.
# Strip the grouping ONLY when the value has exactly that shape -- a real
# password that legitimately contains spaces never matches four
# space-separated groups of four lowercase letters, so nothing else is
# rewritten (and .env stays a byte-for-byte store for every other value).
_GMAIL_APP_PASSWORD_RE = re.compile(r"^[a-z]{4}( [a-z]{4}){3}$")


def _normalize_app_password(value: str) -> str:
    if _GMAIL_APP_PASSWORD_RE.match(value):
        return value.replace(" ", "")
    return value


# The last 4 characters are only ever unmasked when doing so still hides
# more of the value than it shows. `len(value) > 4` (the old guard) let a
# 5-character secret through with 4 of its 5 characters on screen -- not
# meaningfully different from printing it outright. Requiring the hidden
# remainder to outnumber the shown tail (len - 4 > 4, i.e. length > 8) is
# the actual "long enough" the comment below claims.
_MIN_LENGTH_TO_UNMASK_TAIL = 8


def _mask(value: str) -> str:
    # At most the last 4 characters are ever disclosed, and only when the
    # value is long enough that doing so doesn't reveal the whole thing.
    # Leading characters are deliberately never shown: for an SMTP app
    # password (a flat run of random letters) a leading slice is just as
    # secret as a trailing one, so there is no "safe prefix" to expose.
    if not value:
        return ""
    if len(value) > _MIN_LENGTH_TO_UNMASK_TAIL:
        return f"{'•' * 8}{value[-4:]}"
    return "•" * 8


def _validation_message(exc: ValidationError) -> str:
    return "Could not save: " + describe_validation_error(exc)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "") -> HTMLResponse:
    c = request.app.state.holder.get()
    s = c.settings
    return templates.TemplateResponse(request, "settings.html", {
        "page": "settings", "s": s, "saved": bool(saved), "error": "",
        "masks": {f: _mask(str(getattr(s, f, ""))) for f in SECRET_FIELDS},
        # Fetched (or served from cache/fallback) for the *active* provider
        # only -- all three model fields share one provider, so one catalog
        # covers all three selects. models_catalog.list_models() carries its
        # own timeout and any-failure fallback, so this can never be what
        # makes a GET here hang or 500.
        "model_options": models_catalog.list_models(s.llm_provider),
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
        if field == "smtp_password":
            value = _normalize_app_password(value)
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
        # Redisplay exactly what the user typed for every PLAIN_FIELDS input,
        # not the last-saved value -- a validation failure on, say,
        # sentinel_interval_minutes must not also discard an unrelated,
        # perfectly valid smtp_host edit sitting in the same form. This is a
        # raw, unvalidated copy for *display only*: model_copy(update=...)
        # bypasses Settings' own type coercion/validation (unlike
        # constructing a new Settings(**candidate), which is exactly what
        # just failed above), so an out-of-range or non-numeric string can
        # sit in `s.sentinel_interval_minutes` here purely to be echoed back
        # into its <input value="...">. Secrets are deliberately excluded --
        # `updates` only ever holds a secret when the user typed a new one,
        # and secrets still render through `masks`, never as plain text.
        display = current.model_copy(
            update={f: updates[f] for f in PLAIN_FIELDS if f in updates})
        return templates.TemplateResponse(request, "settings.html", {
            "page": "settings", "s": display, "saved": False,
            "error": _validation_message(exc),
            "masks": {f: _mask(str(getattr(current, f, ""))) for f in SECRET_FIELDS},
            # display.llm_provider, not current.llm_provider -- the user may
            # have changed the provider dropdown in the same submit that
            # failed validation elsewhere; the catalog must match whichever
            # provider is actually selected on the redisplayed page.
            "model_options": models_catalog.list_models(display.llm_provider),
            **nav_context(c)}, status_code=400)

    old_interval = current.sentinel_interval_minutes
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

    new_interval = holder.get().settings.sentinel_interval_minutes
    scheduler = getattr(request.app.state, "scheduler", None)
    # `scheduler` is only set when `serve` started one (create_app's
    # start_scheduler=True) -- a test build or the standalone `run` daemon
    # has none, and rebuild() above already made the interval correct for
    # the *next* full restart either way. A reschedule failure (e.g. the
    # scheduler was mid-shutdown) must not turn a successful settings save
    # into a 500 -- the write to .env already succeeded.
    if scheduler is not None and new_interval != old_interval:
        try:
            reschedule_sentinel_job(scheduler, new_interval)
        except Exception as exc:  # noqa: BLE001 — see comment above
            print(f"[settings] could not reschedule sentinel job: {exc}",
                  file=sys.stderr)

    return RedirectResponse("/settings?saved=1", status_code=303)


_TEST_SUBJECT = "AllPath Trade test"
_TEST_BODY = ("This is a test notification. If you are reading it, "
              "notification delivery works.")


def _test_result_fragment(request: Request, *, ok: bool, message: str) -> HTMLResponse:
    # A tiny, standalone fragment -- not the full settings page -- swapped
    # into the section's own result <div> by htmx. Jinja autoescapes both
    # values, but `message` is always one of this module's own fixed
    # strings below, never anything the user typed (the password especially
    # must never round-trip back onto the page).
    return templates.TemplateResponse(request, "_settings_test_result.html", {
        "ok": ok, "message": message})


@router.post("/settings/test-email", response_class=HTMLResponse)
async def test_email(request: Request) -> HTMLResponse:
    """Send one test email using exactly what's currently typed in the
    Email notifications section -- nothing is read from, or written to,
    stored Settings except the password fallback below. Never saves."""
    form = await request.form()
    current = request.app.state.holder.get().settings

    host = str(form.get("smtp_host", "")).strip()
    if not host:
        return _test_result_fragment(
            request, ok=False, message="SMTP host is required to send a test email.")

    port_raw = str(form.get("smtp_port", "")).strip()
    try:
        port = int(port_raw) if port_raw else current.smtp_port
    except ValueError:
        return _test_result_fragment(
            request, ok=False, message="SMTP port must be a number.")

    user = str(form.get("smtp_user", "")).strip()
    sender = str(form.get("smtp_from", "")).strip()
    to = str(form.get("notify_to", "")).strip()
    # Same keep-what-is-stored semantics as saving: a blank password field
    # means "use the one already on file", not "send with no password".
    password = _normalize_app_password(str(form.get("smtp_password", "")).strip())
    if not password:
        password = current.smtp_password

    notifier = EmailNotifier(host, port, user, password, sender, to)
    ok = notifier.send(_TEST_SUBJECT, _TEST_BODY)
    message = ("Test email sent — check your inbox." if ok else
               "Test email failed to send — check your settings and server logs.")
    return _test_result_fragment(request, ok=ok, message=message)


@router.post("/settings/test-push", response_class=HTMLResponse)
async def test_push(request: Request) -> HTMLResponse:
    """Send one test push using exactly the ntfy URL currently typed in the
    Push notifications section. Never saves."""
    form = await request.form()
    url = str(form.get("ntfy_url", "")).strip()
    if not url:
        return _test_result_fragment(
            request, ok=False, message="Enter a ntfy topic URL first.")
    try:
        # Reuses Settings' own field validator rather than duplicating its
        # http(s)-scheme regex here -- one source of truth for what counts
        # as a valid ntfy_url.
        Settings._ntfy_url_needs_a_scheme(url)
    except ValueError as exc:
        return _test_result_fragment(request, ok=False, message=str(exc))

    notifier = NtfyNotifier(url)
    ok = notifier.send(_TEST_SUBJECT, _TEST_BODY)
    message = ("Test push sent — check your phone." if ok else
               "Test push failed to send — check your settings and server logs.")
    return _test_result_fragment(request, ok=ok, message=message)


@router.post("/settings/reset-token")
def reset_token(request: Request) -> Response:
    token = secrets.token_urlsafe(24)
    request.app.state.holder.store().set("WEB_TOKEN", token)
    request.app.state.holder.rebuild()
    print(f"[allpath-trade] new access token: {token}")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response
