"""Web-layer account context: which account (`paper`/`shadow`) the CURRENT
request's browser is looking at, and the bundle of account-scoped stores
that answers to it.

This is the web UI's own switch, independent of Telegram's (`telegram.py`
keeps its own `TELEGRAM_ACCOUNT_KEY` in app_state -- spec: "手机和电脑各自
有上下文"). Reading order for every account-aware web route is: `bundle
(request)` for anything account-scoped (broker, journal, queue, strategies,
memory, reports, conversations, observations, sentinel, consolidator,
reflector), `components(request)` (web/deps.py) for anything genuinely
shared across both accounts (settings, app_state, the data source, the
notifier, the sqlite connection, llm_usage).
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from allpath_trade.app import AccountComponents, Components
from allpath_trade.store.accounts import DEFAULT_ACCOUNT, is_valid_account
from allpath_trade.web.deps import components

router = APIRouter()

ACCOUNT_COOKIE = "account"
# 1 year -- same "remembered until explicitly changed" shape as the session
# cookie's own `remember` max_age (web/auth.py), but unconditional: there is
# no "don't remember me" checkbox for which account you're looking at, and a
# cookie that silently expired back to paper mid-session would be a far more
# surprising failure mode here than for a login session.
_COOKIE_MAX_AGE_SECONDS = 365 * 24 * 3600


def current_account(request: Request) -> str:
    """The web UI's current account: the `account` cookie's value if it's
    one of `ACCOUNTS`, else `DEFAULT_ACCOUNT` ("paper") -- both for a
    missing cookie (a browser that's never switched) and for a garbled/
    forged one (an unrecognized value must never resolve to an unscoped or
    third partition -- see `is_valid_account`'s own docstring)."""
    raw = request.cookies.get(ACCOUNT_COOKIE)
    return raw if raw and is_valid_account(raw) else DEFAULT_ACCOUNT


def bundle_for(c: Components, account: str) -> Components | AccountComponents:
    """The account-scoped bundle for `account`, given an already-fetched
    `Components`.

    For `DEFAULT_ACCOUNT` ("paper") this returns `c` ITSELF, not
    `c.accounts["paper"]` -- a deliberate choice, not a shortcut: `Components`
    already carries every field `AccountComponents` does (shadow-dual-active
    T4's legacy alias surface, extended by T5 with `conversations`/`search`/
    `account` -- see app.py's `Components` docstring) as direct references
    to the SAME objects `accounts["paper"]` holds. Returning `c` means
    "the current account's bundle" and "the shared `Components` object" are
    the same object for paper, so every pre-existing test that monkeypatches
    an attribute directly on `holder.get()` (`.broker`, `.data`, `.queue`,
    ...) keeps working unchanged through this function -- `accounts["paper"]`
    is a genuinely different object whose fields were only ever copied once,
    at construction time, and would never see such a monkeypatch."""
    if account == DEFAULT_ACCOUNT:
        return c
    return c.accounts[account]


def bundle(request: Request) -> Components | AccountComponents:
    """The current request's account bundle -- `bundle_for` resolved
    against `current_account(request)`. The one function almost every
    account-aware route body needs."""
    return bundle_for(components(request), current_account(request))


def _safe_redirect_target(request: Request) -> str:
    """Where `POST /account/switch` sends the browser back to: the
    `Referer` header when it's same-origin, `/` otherwise (a missing/absent
    Referer, a cross-origin one, or one this server can't parse). Mirrors
    web/auth.py's own Origin-check reasoning for why this matters on a LAN:
    a same-origin check on a value the client controls is not itself a
    security boundary (SameSite=Strict on the cookie already stops a
    cross-site POST from carrying it) -- it exists so a forged/garbled
    Referer can't redirect the browser off this app entirely, not to
    authorize the switch itself."""
    referer = request.headers.get("referer")
    if not referer:
        return "/"
    try:
        parsed = urlparse(referer)
    except ValueError:
        return "/"
    if parsed.scheme and parsed.netloc and (parsed.scheme, parsed.netloc) != (
            request.url.scheme, request.url.netloc):
        return "/"
    path = parsed.path or "/"
    # shadow-dual-active T5 review Minor: a Referer whose path itself starts
    # with `//` (e.g. `https://this-host//evil.example/x`) parses with an
    # empty netloc override but a path a browser will still treat as
    # protocol-relative when handed back in a Location header -- `RedirectResponse`
    # doesn't re-validate this string, so returning it as-is would send the
    # browser off this app to whatever host follows the `//`, defeating the
    # same-origin check just above.
    if path.startswith("//"):
        return "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


@router.post("/account/switch")
def switch_account(request: Request, account: str = Form(DEFAULT_ACCOUNT)):
    # An invalid posted value (a hand-crafted form, a stale client) falls
    # back to DEFAULT_ACCOUNT rather than rejecting the request outright --
    # same "never resolve to an unscoped/third partition, degrade to a known
    # value instead" posture as `current_account` above.
    target = account if is_valid_account(account) else DEFAULT_ACCOUNT
    response = RedirectResponse(_safe_redirect_target(request), status_code=303)
    # Same cookie discipline as the session cookie (web/auth.py's COOKIE):
    # HttpOnly (no reason for page JS to ever read this) and SameSite=Strict
    # (never sent on a cross-site request, so a third-party page cannot
    # flip which account this browser is looking at).
    response.set_cookie(ACCOUNT_COOKIE, target, httponly=True, samesite="strict",
                        max_age=_COOKIE_MAX_AGE_SECONDS)
    return response
