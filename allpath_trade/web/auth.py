from __future__ import annotations

import hmac
import secrets

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from allpath_trade.config import Settings, SettingsStore
from allpath_trade.web.templating import templates

COOKIE = "allpath_session"
REMEMBER_SECONDS = 30 * 24 * 3600
PUBLIC_PATHS = {"/login", "/healthz"}


def ensure_token(store: SettingsStore, settings: Settings) -> str:
    """Return the access token, generating and persisting one on first run."""
    if settings.web_token:
        return settings.web_token
    token = secrets.token_urlsafe(24)
    store.set("WEB_TOKEN", token)
    settings.web_token = token
    return token


def _authorized(request: Request, token: str) -> bool:
    # `bool(cookie)` is load-bearing: without it, an empty/unset token
    # (`token == ""`) compared against a browser sending no cookie (which
    # also reads as `""` from `.get(COOKIE, "")`) would pass
    # `hmac.compare_digest("", "")` and authenticate anyone. An unconfigured
    # WEB_TOKEN must never grant access.
    #
    # Comparing utf-8-encoded bytes rather than the raw `str`s is also
    # load-bearing: `hmac.compare_digest` raises `TypeError` on `str`
    # operands containing non-ASCII characters, and both the cookie and a
    # forged cookie value come straight from the client. Encoding first
    # keeps a non-ASCII cookie a routine rejection instead of an unhandled
    # 500 on the pre-auth path.
    cookie = request.cookies.get(COOKIE, "")
    return bool(cookie) and hmac.compare_digest(cookie.encode(), token.encode())


def install_auth(app: FastAPI) -> None:
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        # A trailing slash (e.g. a health probe hitting `/healthz/`) must
        # still count as the public path -- otherwise it fails closed into
        # a login redirect instead of the health check it asked for.
        normalized = path.rstrip("/") or "/"
        if normalized in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)

        token = request.app.state.holder.settings().web_token
        if not _authorized(request, token):
            return RedirectResponse("/login", status_code=303)

        if request.method not in ("GET", "HEAD"):
            # Same-origin check: on a LAN, another device could otherwise
            # serve a page that posts orders into this session. When the
            # Origin header is absent, the request is allowed through.
            # This is not relying on browsers omitting Origin for
            # same-origin posts -- per the Fetch standard, modern browsers
            # send it on same-origin form posts too. The real argument is
            # about who can reach this branch at all: a non-browser client
            # on the LAN (curl, a script) has no session cookie yet and is
            # stopped at the `_authorized` check above, before Origin is
            # ever consulted; and a client that somehow already holds the
            # cookie could set Origin to anything it likes, so rejecting on
            # *absence* specifically buys no defense against it either.
            # Rejecting an absent Origin would only inconvenience clients
            # that are already blocked or already unstoppable -- it isn't
            # a second authentication factor, so it isn't treated as one.
            # `SameSite=Strict` on the cookie is the real defense (it stops
            # the cookie being sent cross-site at all); this check is
            # defense-in-depth against a *mismatched* Origin on cookie-bearing
            # requests, which is the case it actually catches.
            origin = request.headers.get("origin")
            if origin is not None:
                expected = f"{request.url.scheme}://{request.url.netloc}"
                if origin != expected:
                    return Response("cross-origin request refused",
                                    status_code=403)
        return await call_next(request)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> Response:
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login(request: Request, token: str = Form(""),
              remember: str = Form("")) -> Response:
        expected = request.app.state.holder.settings().web_token
        # See `_authorized` above: encode to bytes first so a submitted
        # token containing non-ASCII characters (e.g. a pasted smart quote)
        # is a normal 401, not a 500 from `hmac.compare_digest`.
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            return templates.TemplateResponse(
                request, "login.html", {"error": "That token is not valid."},
                status_code=401)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, expected, httponly=True, samesite="strict",
                            max_age=REMEMBER_SECONDS if remember else None)
        return response

    @app.post("/logout")
    def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response
