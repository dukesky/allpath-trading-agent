from __future__ import annotations

import re
import secrets
import sys
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from allpath_trade.config import Settings, describe_validation_error
from allpath_trade.llm.prices import PRICES_UPDATED, estimate_cost
from allpath_trade.notify.email import EmailNotifier
from allpath_trade.notify.ntfy import NtfyNotifier
from allpath_trade.scheduler import reschedule_sentinel_job
from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, TELEGRAM_USER_ID_KEY
from allpath_trade.web import models_catalog
from allpath_trade.web.auth import COOKIE
from allpath_trade.web.format import money
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.templating import templates

router = APIRouter()

# Plain values: rendered, editable, rewritten on every save.
PLAIN_FIELDS = ["llm_provider", "chat_model", "review_model", "memory_model",
                "smtp_host", "smtp_port", "smtp_user", "smtp_from", "notify_to",
                "ntfy_url", "sentinel_interval_minutes", "context_budget_tokens",
                "web_base_url"]

# Checkbox values: a browser omits an unchecked box from the form body
# entirely, so these need explicit "present -> true, absent -> false"
# handling rather than the "only touch what's present" rule PLAIN_FIELDS
# uses -- otherwise they could never be turned off again.
BOOLEAN_FIELDS = ["daily_consolidation", "consolidate_after_chat"]

# Secret values: never rendered back. A blank field means "leave it alone".
SECRET_FIELDS = ["openrouter_api_key", "openai_api_key", "anthropic_api_key",
                 "alpaca_api_key", "alpaca_secret_key", "smtp_password",
                 "telegram_bot_token"]

# The page's 7 sections, now tabs (see settings.html) -- ordered as they
# appear left-to-right in the tab bar. Keys are also the `#hash` each tab's
# link uses, so the client-side switcher script and this list must agree.
TABS = [
    ("model", "Model"),
    ("brokerage", "Brokerage"),
    ("email", "Email notifications"),
    ("push", "Push notifications"),
    ("telegram", "Telegram"),
    ("sentinel", "Sentinel and memory"),
    ("access", "Access"),
    # Read-only -- no fields of its own, so unlike every other tab it has no
    # entries to add to PLAIN_FIELDS/BOOLEAN_FIELDS/SECRET_FIELDS/
    # FIELD_TO_TAB below (nothing here is ever posted back).
    ("usage", "Usage"),
]
DEFAULT_TAB = TABS[0][0]

# field name -> tab it lives on. Used only to pick which tab a validation
# error should redisplay (see `_error_tab` below) -- every field across
# PLAIN_FIELDS/BOOLEAN_FIELDS/SECRET_FIELDS must have an entry here, or a
# failure on that field would silently fall back to the default tab instead
# of opening the one the user actually needs to fix.
FIELD_TO_TAB = {
    "llm_provider": "model", "chat_model": "model", "review_model": "model",
    "memory_model": "model", "openrouter_api_key": "model",
    "openai_api_key": "model", "anthropic_api_key": "model",
    "alpaca_api_key": "brokerage", "alpaca_secret_key": "brokerage",
    "smtp_host": "email", "smtp_port": "email", "smtp_user": "email",
    "smtp_from": "email", "notify_to": "email", "smtp_password": "email",
    "ntfy_url": "push",
    "telegram_bot_token": "telegram",
    "sentinel_interval_minutes": "sentinel", "context_budget_tokens": "sentinel",
    "daily_consolidation": "sentinel", "consolidate_after_chat": "sentinel",
    "web_base_url": "access",
}


def _error_tab(exc: ValidationError) -> str:
    """Which tab to reopen so the first validation error is actually
    visible, rather than silently landing on a hidden panel. `exc.errors()`
    is never empty for a raised ValidationError; falls back to the default
    tab only if pydantic ever reports a field this map doesn't know about."""
    errors = exc.errors()
    if not errors:
        return DEFAULT_TAB
    field = str(errors[0]["loc"][0]) if errors[0].get("loc") else ""
    return FIELD_TO_TAB.get(field, DEFAULT_TAB)

# Gmail displays app passwords as "abcd efgh ijkl mnop"; pasting that
# verbatim used to store the separators and fail SMTP auth. The separators
# are not always plain spaces: Google's dialog copies U+00A0 (no-break
# space) on some platforms, which smtplib can't even ASCII-encode -- the
# send then dies locally with UnicodeEncodeError before reaching Google
# (seen in the field on macOS). So: drop ALL Unicode whitespace, and use
# the cleaned form only when what remains is exactly the 16 lowercase
# letters of an app password. Any other value -- including a real password
# that merely contains spaces -- is stored byte-for-byte as typed.
def _normalize_app_password(value: str) -> str:
    cleaned = "".join(value.split())  # str.split() splits on all Unicode whitespace
    if re.fullmatch(r"[a-z]{16}", cleaned):
        return cleaned
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


def _telegram_status(chat_id: str | None) -> str:
    """Pairing status line for the Telegram section (spec §①): a paired chat
    id is masked to its last 4 characters, same "never show more than the
    tail" discipline as `_mask` above -- a Telegram chat id is not a secret
    the way a bot token is, but it's still an identifier this page has no
    reason to print in full. `chat_id` is `None`/`""` (AppState.get's
    "never set" return, or the empty string Unpair used to leave before it
    switched to a real delete) for "not paired" -- both read the same way to
    a user looking at this line."""
    if not chat_id:
        return "Not paired"
    tail = chat_id[-4:] if len(chat_id) > 4 else chat_id
    return f"Paired (chat …{tail})"


# Usage panel windows (Settings -> Usage, read-only): the two headline
# summary windows plus how many days of the per-day mini table to show.
# 14 days of daily rows is enough to see a trend without turning the tab
# into an ever-growing table.
_USAGE_SUMMARY_WINDOWS = (7, 30)
_USAGE_DAILY_DAYS = 14


def _usage_window(rows) -> dict:
    """One `summary(days)` result -> the per-tier breakdown + grand total
    the Usage panel renders for that window. `is_default_rate` on a tier
    means at least one of its rows priced against `prices.DEFAULT_PRICE`
    (an unknown model) rather than a real catalog entry -- surfaced so the
    panel can flag which numbers are the least trustworthy."""
    tiers: dict[str, dict] = {}
    total_cost = Decimal(0)
    total_input = 0
    total_output = 0
    for row in rows:
        cost, is_default = estimate_cost(row["model"], row["input_tokens"], row["output_tokens"])
        tier = tiers.setdefault(row["tier"], {
            "input_tokens": 0, "output_tokens": 0, "cost": Decimal(0),
            "is_default_rate": False, "models": [],
        })
        tier["input_tokens"] += row["input_tokens"]
        tier["output_tokens"] += row["output_tokens"]
        tier["cost"] += cost
        tier["is_default_rate"] = tier["is_default_rate"] or is_default
        tier["models"].append({
            "model": row["model"], "calls": row["calls"],
            "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
            "cost": money(cost), "is_default_rate": is_default,
        })
        total_cost += cost
        total_input += row["input_tokens"]
        total_output += row["output_tokens"]
    for tier in tiers.values():
        tier["cost"] = money(tier["cost"])
    return {
        "tiers": tiers, "total_cost": money(total_cost),
        "total_input_tokens": total_input, "total_output_tokens": total_output,
        "has_usage": bool(rows),
    }


def _usage_context(c) -> dict:
    """Settings -> Usage tab context: `windows` keyed by the number of days
    (7/30, matching `_USAGE_SUMMARY_WINDOWS`) plus a per-day token table for
    the last `_USAGE_DAILY_DAYS` days. Read-only -- nothing here is ever
    posted back, so unlike every other tab this needs no PLAIN_FIELDS/
    FIELD_TO_TAB entries at all."""
    return {
        "usage_windows": {days: _usage_window(c.llm_usage.summary(days))
                         for days in _USAGE_SUMMARY_WINDOWS},
        # The template iterates windows in a fixed order to render the tab
        # buttons/panels -- passed explicitly rather than hardcoded as
        # `[7, 30]` in settings.html, so a future change to
        # `_USAGE_SUMMARY_WINDOWS` (e.g. adding a 90-day window) can't
        # silently drift out of sync with what the template renders.
        "usage_windows_order": list(_USAGE_SUMMARY_WINDOWS),
        "usage_daily": [{"day": row["day"], "input_tokens": row["input_tokens"],
                         "output_tokens": row["output_tokens"]}
                       for row in c.llm_usage.daily(_USAGE_DAILY_DAYS)],
        "prices_updated": PRICES_UPDATED,
    }


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
        "telegram_status": _telegram_status(c.app_state.get(TELEGRAM_CHAT_ID_KEY)),
        "tabs": TABS, "active_tab": DEFAULT_TAB,
        **_usage_context(c), **nav_context(request)})


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
            "telegram_status": _telegram_status(c.app_state.get(TELEGRAM_CHAT_ID_KEY)),
            # Reopen the tab the first invalid field actually lives on --
            # otherwise the error banner at the top of the page points at a
            # hidden panel and the user has no idea what to fix.
            "tabs": TABS, "active_tab": _error_tab(exc),
            **_usage_context(c), **nav_context(request)}, status_code=400)

    old_interval = current.sentinel_interval_minutes
    store = holder.store()
    for field, value in updates.items():
        store.set(field.upper(), value)
    holder.rebuild()
    # The in-flight turn (if any) already holds its own AgentSession object
    # (captured out of ChatService.session() before this ran) and keeps
    # running against the configuration it captured -- this doesn't touch it.
    # `app.state.chat_service` itself is a single instance shared with the
    # Telegram poller for the life of the process (web/app.py), so it is no
    # longer replaced here the way `app.state.chat = None` used to force a
    # fresh ChatService -- that would leave the poller holding a stale,
    # orphaned object. Instead, invalidate() drops just the cached session,
    # so the *next* call to `session()` rebuilds against the just-rebuilt
    # Components, picking up the new provider/model/key.
    #
    # shadow-dual-active T5 review Important 2: `app.state.chat_service` is
    # only the paper alias (web/app.py) -- `holder.rebuild()` above rebuilds
    # the shared `Components` (and both accounts' bundles) for BOTH
    # accounts, but invalidating just the paper alias left shadow's
    # ChatService holding a stale AgentSession built against the pre-rebuild
    # Components until its own staleness timer expired. Every account's
    # instance in `app.state.chat_services` must be invalidated here.
    for service in request.app.state.chat_services.values():
        service.invalidate()

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

    to = str(form.get("notify_to", "")).strip()
    if not to:
        return _test_result_fragment(
            request, ok=False,
            message="Add a \"Send notifications to\" address first.")

    port_raw = str(form.get("smtp_port", "")).strip()
    try:
        port = int(port_raw) if port_raw else current.smtp_port
    except ValueError:
        return _test_result_fragment(
            request, ok=False, message="SMTP port must be a number.")

    user = str(form.get("smtp_user", "")).strip()
    sender = str(form.get("smtp_from", "")).strip()
    # Same keep-what-is-stored semantics as saving: a blank password field
    # means "use the one already on file", not "send with no password".
    password = _normalize_app_password(str(form.get("smtp_password", "")).strip())
    if not password:
        password = current.smtp_password

    notifier = EmailNotifier(host, port, user, password, sender, to)
    # EmailNotifier.send() is a blocking socket call with a 10s connect
    # timeout (see notify/email.py) -- run it in Starlette's threadpool so a
    # slow or unreachable SMTP host cannot freeze this process's single
    # asyncio event loop (and with it every other request: dashboard, chat,
    # the sentinel) for the duration of the timeout. chat.py's /chat/send
    # gets the same effect for free by being a plain `def` route -- this
    # route is `async def` (it awaits `request.form()`), so the blocking
    # call has to be pushed off the loop explicitly instead.
    ok = await run_in_threadpool(notifier.send, _TEST_SUBJECT, _TEST_BODY)
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
    # See test_email's comment above -- NtfyNotifier.send() is also a
    # blocking HTTP call and must not run directly on the event loop.
    ok = await run_in_threadpool(notifier.send, _TEST_SUBJECT, _TEST_BODY)
    message = ("Test push sent — check your phone." if ok else
               "Test push failed to send — check your settings and server logs.")
    return _test_result_fragment(request, ok=ok, message=message)


@router.post("/settings/telegram/unpair")
def unpair_telegram(request: Request) -> Response:
    """Clears pairing state (spec §①: "Unpair 按钮清 app_state key,
    POST,不碰交易语义"). Deletes both keys `TelegramPoller._handle_update`
    checks (chat id AND the paired user id -- see `telegram.py`'s
    belt-and-suspenders match on both) so a stale row can never leave the
    pairing check half-satisfied. Idempotent: unpairing when nothing is
    paired just deletes rows that were never there, no error. Does not
    touch `.env`/Settings or `holder.rebuild()` -- this is Telegram-poller
    runtime state (app_state), the same distinction store/app_state.py's
    module docstring draws for the Telegram keys, not configuration."""
    c = request.app.state.holder.get()
    c.app_state.delete(TELEGRAM_CHAT_ID_KEY)
    c.app_state.delete(TELEGRAM_USER_ID_KEY)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/reset-token")
def reset_token(request: Request) -> Response:
    token = secrets.token_urlsafe(24)
    request.app.state.holder.store().set("WEB_TOKEN", token)
    request.app.state.holder.rebuild()
    print(f"[allpath-trade] new access token: {token}")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response
