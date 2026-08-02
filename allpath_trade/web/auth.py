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
    cookie = request.cookies.get(COOKIE, "")
    return bool(cookie) and hmac.compare_digest(cookie, token)


def install_auth(app: FastAPI) -> None:
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)

        token = request.app.state.holder.settings().web_token
        if not _authorized(request, token):
            return RedirectResponse("/login", status_code=303)

        if request.method not in ("GET", "HEAD"):
            # Same-origin check: on a LAN, another device could otherwise
            # serve a page that posts orders into this session. When the
            # Origin header is absent, the request is allowed through: a
            # browser omits it for some same-origin requests (plain HTML
            # form posts, some navigations), so rejecting on absence would
            # break normal use of this very login/logout flow. A malicious
            # non-browser client that strips the header gets in, but it
            # still needs the session cookie first (HttpOnly, so it can't be
            # read via script from a page on another origin) — this check's
            # job is to stop a same-network browser page from riding an
            # already-authenticated session, not to authenticate on its own.
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
        if not expected or not hmac.compare_digest(token, expected):
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
