"""setup-wizard T3: the first-run wizard at `/setup`.

Four steps behind one URL (`?step=1..4`), each of which can be skipped:
LLM key, Alpaca paper keys, the shadow ledger, and a closing checklist.
The gate in `web/auth.py` (T2) is what sends an unconfigured install here;
this module is what it finds.

Two rules run through everything below:

* **Nothing new is invented.** Saving writes existing `Settings` fields
  through the same `SettingsStore` + `holder.rebuild()` path the settings
  page uses (`routes/settings.py::save`), so a key entered here is live
  without a restart and reads back identically on `/settings`. No trading
  parameter is reachable from this page at all.
* **Secrets go in, never out.** Stored keys are rendered as `_mask`
  (settings.py's own helper) and the inputs never carry a `value=`; the
  two Test endpoints below persist nothing and pass every error through
  `_sanitize_error`, which redacts the key it was just handed before
  truncating.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from allpath_trade.broker.alpaca import AlpacaBroker
from allpath_trade.config import (
    Settings,
    describe_validation_error,
    normalize_llm_provider,
)
from allpath_trade.llm.factory import build_llm
from allpath_trade.web.account_ctx import set_account_cookie
from allpath_trade.web.format import money
from allpath_trade.web.routes.dashboard import nav_context

# The masking and ledger-summary helpers the settings page already uses.
# Imported rather than reimplemented: a second mask that disclosed one more
# character, or a second ledger summary that counted positions differently,
# would be a silent divergence between two pages showing the same facts.
from allpath_trade.web.routes.settings import _mask, _shadow_ledger_summary
from allpath_trade.web.setup_status import (
    SETUP_DISMISSED_KEY,
    alpaca_keys_missing,
    llm_key_missing,
)
from allpath_trade.web.templating import templates

router = APIRouter()

# (number, progress-header label). The order is the wizard's order and the
# progress header's order -- one list so they cannot disagree.
STEPS = [(1, "LLM"), (2, "Paper account"), (3, "Shadow account"), (4, "Done")]
FIRST_STEP, LAST_STEP = STEPS[0][0], STEPS[-1][0]

# Provider -> the `Settings` field holding its key. Deliberately a subset of
# `setup_status._PROVIDER_KEY_FIELDS`: the wizard offers the two providers
# the spec walks a new user through, while Settings keeps offering all
# three (an existing `openai` install is never touched by this page -- it
# just isn't one of the two radio options).
PROVIDER_KEY_FIELDS = {"openrouter": "openrouter_api_key",
                       "anthropic": "anthropic_api_key"}

# Every secret the wizard shows a mask for. Same fields, same mask, as the
# settings page renders for them.
MASKED_FIELDS = ["openrouter_api_key", "anthropic_api_key",
                 "alpaca_api_key", "alpaca_secret_key"]

# The one message the LLM test sends. Short on purpose: this is a
# reachability check (key valid, model exists, a reply comes back), not a
# capability probe, and the user pays for the tokens.
PROBE_MESSAGE = "Reply with OK."

# How much of a failure's text is shown. Long enough for "401 Unauthorized"
# or a DNS failure to be recognizable, short enough that a provider that
# echoes the whole request body back in its error cannot paint the page
# with it.
ERROR_CHARS = 120


def _default_step(settings: Settings) -> int:
    """The first step that still has something to do. A user who has
    already entered both keys (the "Re-run setup" link from Settings) lands
    on step 3 rather than being walked through two finished forms."""
    if llm_key_missing(settings):
        return 1
    if alpaca_keys_missing(settings):
        return 2
    return 3


def _resolve_step(raw: str, settings: Settings) -> int:
    """`?step=` -> a real step number. Anything unparseable or out of range
    falls back to the default step instead of erroring: this URL is typed by
    hand, linked to from Settings, and left in browser history across
    releases, and none of those deserve a 422."""
    try:
        step = int(raw)
    except (TypeError, ValueError):
        return _default_step(settings)
    if FIRST_STEP <= step <= LAST_STEP:
        return step
    return _default_step(settings)


def _page(request: Request, step: int, *, error: str = "",
          status_code: int = 200) -> HTMLResponse:
    c = request.app.state.holder.get()
    s = c.settings
    provider = normalize_llm_provider(s.llm_provider)
    return templates.TemplateResponse(request, "setup.html", {
        "page": "setup", "step": step, "steps": STEPS, "s": s, "error": error,
        # Which radio is pre-selected. An install configured for a provider
        # this page doesn't offer (`openai`) shows the default rather than
        # no selection at all -- and saving is what would change it, so
        # simply opening the wizard never rewrites anything.
        "provider": provider if provider in PROVIDER_KEY_FIELDS else "openrouter",
        "masks": {f: _mask(str(getattr(s, f, "") or "")) for f in MASKED_FIELDS},
        "shadow": _shadow_ledger_summary(c.accounts["shadow"]),
        **nav_context(request)}, status_code=status_code)


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, step: str = "") -> HTMLResponse:
    """Never redirects -- not even when nothing is missing. The "Re-run
    setup" link on Settings has to land somewhere permanently, and a wizard
    that bounced you out the moment it had nothing left to ask would make
    rotating a key through the guided flow impossible."""
    settings = request.app.state.holder.get().settings
    return _page(request, _resolve_step(step, settings))


def _save(request: Request, updates: dict[str, str], *, redirect: str,
          step: int) -> Response:
    """Write `updates` to `.env` and swap the live components -- the same
    sequence as `routes/settings.py::save`, including validating the
    settings that *would* result before touching the file. Writing an
    unloadable value would brick every future start, and unlike the
    settings page there is no second form here to fix it from."""
    holder = request.app.state.holder
    candidate = holder.get().settings.model_dump()
    candidate.update(updates)
    try:
        Settings(_env_file=None, **candidate)
    except ValidationError as exc:
        return _page(request, step, status_code=400,
                     error="Could not save: " + describe_validation_error(exc))

    store = holder.store()
    for field, value in updates.items():
        store.set(field.upper(), value)
    holder.rebuild()
    # Same reasoning as the settings save: every account's ChatService holds
    # a cached AgentSession built against the pre-rebuild components, and
    # the whole point of entering an LLM key here is that the next message
    # uses it without a restart.
    for service in request.app.state.chat_services.values():
        service.invalidate()
    return RedirectResponse(redirect, status_code=303)


@router.post("/setup/step/1")
async def save_llm(request: Request) -> Response:
    form = await request.form()
    provider = str(form.get("llm_provider", "")).strip().lower()
    if provider not in PROVIDER_KEY_FIELDS:
        return _page(request, 1, status_code=400,
                     error="Choose OpenRouter or Anthropic.")
    updates = {"llm_provider": provider}
    # Blank means "keep what is stored" -- the same rule the settings page's
    # secret fields follow, and the reason the input is never prefilled.
    key = str(form.get("llm_api_key", "")).strip()
    if key:
        updates[PROVIDER_KEY_FIELDS[provider]] = key
    return _save(request, updates, redirect="/setup?step=2", step=1)


@router.post("/setup/step/2")
async def save_alpaca(request: Request) -> Response:
    form = await request.form()
    updates = {}
    for field in ("alpaca_api_key", "alpaca_secret_key"):
        value = str(form.get(field, "")).strip()
        if value:
            updates[field] = value
    # ALPACA_PAPER is deliberately absent here, exactly as it is on the
    # settings page: switching to real money should require editing `.env`
    # by hand, never a control on a page reachable from the LAN.
    return _save(request, updates, redirect="/setup?step=3", step=2)


@router.post("/setup/step/3")
def continue_from_shadow() -> Response:
    """Step 3 has no fields of its own -- its actions (Open Chat, CSV
    import) each post somewhere else and take effect on their own. This is
    just "next"."""
    return RedirectResponse("/setup?step=4", status_code=303)


@router.post("/setup/open-chat")
def open_chat() -> Response:
    """Straight into the shadow account's chat, where the import
    conversation happens. Sets the account cookie through `account_ctx`'s
    own setter rather than a second `set_cookie` call, so this cookie can
    never drift from the switcher's (HttpOnly, SameSite=Strict, 1 year)."""
    response = RedirectResponse("/chat?hint=import", status_code=303)
    set_account_cookie(response, "shadow")
    return response


def _dismiss(request: Request) -> Response:
    request.app.state.holder.get().app_state.set(SETUP_DISMISSED_KEY, "1")
    return RedirectResponse("/", status_code=303)


@router.post("/setup/skip")
def skip(request: Request) -> Response:
    return _dismiss(request)


@router.post("/setup/finish")
def finish(request: Request) -> Response:
    """Finishing sets the SAME flag as skipping, and never clears it. The
    flag only lifts the redirect; the banner is driven by `setup_missing`,
    so anything still unset keeps nagging -- and the Settings link means
    re-running the wizard never depends on this flag either way."""
    return _dismiss(request)


# -- the two test endpoints ------------------------------------------------
#
# Both take the values currently typed into the step (falling back to what
# is stored, so "Test" works on a page where the user only wants to check
# the key already on file), talk to the network in a threadpool, and
# persist NOTHING -- no `.env` write, no `rebuild()`, no components touched.


def _test_fragment(request: Request, *, ok: bool, message: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "_setup_test_result.html", {
        "ok": ok, "message": message})


def _sanitize_error(exc: BaseException, *secrets: str) -> str:
    """`ExceptionType: first 120 characters`, with every secret this call
    was given redacted first.

    Redaction before truncation, not after: a provider that echoes the
    offending API key back inside its error message would otherwise put it
    on the page whenever it appeared in the first 120 characters. Newlines
    are collapsed so a multi-line traceback-ish message can't push the
    rendered fragment down the page.
    """
    text = str(exc)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    text = " ".join(text.split())
    if len(text) > ERROR_CHARS:
        text = text[:ERROR_CHARS] + "…"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


@router.post("/setup/test-llm", response_class=HTMLResponse)
async def test_llm(request: Request) -> HTMLResponse:
    form = await request.form()
    provider = str(form.get("llm_provider", "")).strip().lower()
    if provider not in PROVIDER_KEY_FIELDS:
        return _test_fragment(request, ok=False,
                              message="Choose OpenRouter or Anthropic.")
    field = PROVIDER_KEY_FIELDS[provider]
    current = request.app.state.holder.get().settings
    key = (str(form.get("llm_api_key", "")).strip()
           or str(getattr(current, field, "") or "").strip())
    if not key:
        return _test_fragment(request, ok=False, message="Enter an API key first.")

    # A throwaway Settings carrying only the provider/key under test.
    # `model_copy` deliberately, not a new `Settings(**dump)`: this object
    # never reaches disk or the component graph, it exists for the length of
    # this request purely so `build_llm` can read the two fields it needs.
    probe = current.model_copy(update={"llm_provider": provider, field: key})
    try:
        client = build_llm(probe, tier="chat")
        # `complete` is a blocking HTTP call; this route is `async def`
        # (it awaits the form), so it has to be pushed off the event loop
        # explicitly -- see routes/settings.py::test_email's own note.
        await run_in_threadpool(client.complete,
                                [{"role": "user", "content": PROBE_MESSAGE}])
    except Exception as exc:  # noqa: BLE001 — any failure is a failed test, not a 500
        return _test_fragment(request, ok=False, message=_sanitize_error(exc, key))
    return _test_fragment(request, ok=True, message=f"OK · {client.model} replied")


def _probe_account(key: str, secret: str):
    # Module-global `AlpacaBroker` on purpose (not a local import): it is
    # the seam this route's tests replace. Constructing the broker is part
    # of what runs in the threadpool -- `TradingClient`'s constructor is the
    # SDK's, and nothing guarantees it stays purely local.
    return AlpacaBroker(key, secret, paper=True).get_account()


@router.post("/setup/test-broker", response_class=HTMLResponse)
async def test_broker(request: Request) -> HTMLResponse:
    form = await request.form()
    current = request.app.state.holder.get().settings
    key = (str(form.get("alpaca_api_key", "")).strip()
           or str(current.alpaca_api_key or "").strip())
    secret = (str(form.get("alpaca_secret_key", "")).strip()
              or str(current.alpaca_secret_key or "").strip())
    if not (key and secret):
        return _test_fragment(
            request, ok=False,
            message="Enter both the API key and the secret key first.")
    try:
        account = await run_in_threadpool(_probe_account, key, secret)
    except Exception as exc:  # noqa: BLE001 — any failure is a failed test, not a 500
        return _test_fragment(request, ok=False,
                              message=_sanitize_error(exc, key, secret))
    # paper=True is hardcoded in `_probe_account`: the wizard only ever
    # onboards a paper account, so this equity is always the paper one.
    return _test_fragment(request, ok=True,
                          message=f"Connected · equity {money(account.equity)}")
