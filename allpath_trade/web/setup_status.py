"""setup-wizard T2: "is this install actually set up yet?", in one place.

Deliberately dependency-free -- no FastAPI, no sqlite, no imports from the
route modules. The gate in `web/auth.py`, the banner context in
`web/routes/dashboard.py::nav_context`, and (from Task 3) the wizard's own
pages all need the same answer, and a shared module is the only way they
cannot drift apart. It also makes the decision testable as plain functions,
without a running app.

`AppState` is taken structurally (anything with `.get(key) -> str | None`)
rather than imported, keeping this module free of the store layer.
"""

from __future__ import annotations

from typing import Protocol

from allpath_trade.config import Settings

# Written by the wizard's "skip for now" (Task 3) and read here. Lives in
# `app_state` rather than on `Settings`: it is per-install runtime state,
# not user-editable configuration, and the settings page rewrites `.env`
# wholesale (see store/app_state.py's own note on the Telegram keys).
SETUP_DISMISSED_KEY = "setup_dismissed"

# Everything that must stay reachable while setup is unfinished:
#   /setup           the wizard itself -- gating it would be a redirect loop
#   /login, /logout  signing in is what gets you to the wizard at all
#   /static          the wizard's own CSS
#   /a/              approve-by-link, exempt from the session cookie too
#                    (see web/auth.py) -- a tap from a notification must
#                    not land on a setup wizard
#   /healthz         a health probe is not a browser
#   /account/switch  the switcher posts (so the GET-only gate misses it
#                    anyway); listed so the exemption is stated, not
#                    inferred from the verb
# Matched on whole path segments, never as a bare `startswith` -- see
# `_is_exempt`.
GATE_EXEMPT_PREFIXES = ("/setup", "/login", "/logout", "/static", "/a/",
                        "/healthz", "/account/switch")

# The `Settings` field holding each provider's key. An unknown provider has
# no entry, and so reads as missing -- `llm_provider` is free text (it comes
# straight out of `.env`), and a typo must not be able to satisfy the check
# with some other provider's key.
_PROVIDER_KEY_FIELDS = {
    "openrouter": "openrouter_api_key",
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
}


class _StateReader(Protocol):
    def get(self, key: str) -> str | None: ...


def llm_key_missing(settings: Settings) -> bool:
    """True when the *selected* provider has no key. Another provider's key
    is not a substitute: `llm/factory.py` only ever reads the selected
    provider's own field, so counting any-key-anywhere as configured would
    send the user out of the wizard into a chat that fails on its first
    message."""
    field = _PROVIDER_KEY_FIELDS.get((settings.llm_provider or "").strip().lower())
    if field is None:
        return True
    return not (getattr(settings, field) or "").strip()


def alpaca_keys_missing(settings: Settings) -> bool:
    """True unless BOTH halves are present -- agreeing with
    `app.py::_build_broker`, which already treats a half-filled pair as
    unconfigured (it cannot authenticate). If these two disagreed, the
    dashboard would show a placeholder broker with nothing explaining
    why."""
    return not ((settings.alpaca_api_key or "").strip()
                and (settings.alpaca_secret_key or "").strip())


def setup_missing(settings: Settings) -> list[str]:
    """The human-readable names of what is still unset, in wizard order.

    The order is load-bearing twice over: it is the sentence the banner
    joins together, and it is the order the wizard walks the user through.
    """
    missing = []
    if llm_key_missing(settings):
        missing.append("LLM key")
    if alpaca_keys_missing(settings):
        missing.append("Alpaca keys")
    return missing


def setup_dismissed(app_state: _StateReader) -> bool:
    """Whether the user chose "skip for now". Dismissal only lifts the
    redirect -- the banner stays until setup is actually finished."""
    return bool(app_state.get(SETUP_DISMISSED_KEY))


def _is_exempt(path: str) -> bool:
    # Whole-segment matching, not a bare `startswith` on the prefixes: the
    # latter would quietly hand a free pass to any future route whose name
    # merely begins with one of them (`/settings` vs `/setup`, or a
    # `/logout-everywhere`).
    normalized = path.rstrip("/") or "/"
    for prefix in GATE_EXEMPT_PREFIXES:
        base = prefix.rstrip("/") or "/"
        if normalized == base or normalized.startswith(base + "/"):
            return True
    return False


def should_redirect_to_setup(request_method: str, path: str,
                             settings: Settings,
                             app_state: _StateReader) -> bool:
    """Whether this request should be bounced to `/setup`.

    GET only, on purpose. A 302 on a POST is re-issued by the browser as a
    GET with the body dropped, so gating writes would silently swallow form
    submissions -- starting with the settings save that enters the very
    keys being asked for. HEAD is left alone too: nothing is rendered for a
    human to read, so there is nothing to redirect them to. `path` is a
    path, not a URL -- pass `request.url.path`.
    """
    if request_method != "GET":
        return False
    if _is_exempt(path):
        return False
    # `setup_missing` before `setup_dismissed`, deliberately: the former is
    # pure attribute reads, the latter is a database round-trip, and this
    # runs on every page GET from an async middleware (so it blocks the
    # event loop for its duration). Ordering it this way means a finished
    # install -- the steady state, forever -- never pays for the query at
    # all; only an install that really is missing keys does, and its very
    # next act is a redirect.
    if not setup_missing(settings):
        return False
    return not setup_dismissed(app_state)
