from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from allpath_trade.llm.factory import LLMConfigError
from allpath_trade.web.account_ctx import bundle, current_account
from allpath_trade.web.chat_service import ChatService
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.routes.reviews import revision_view
from allpath_trade.web.templating import templates

router = APIRouter()


def _service(request: Request) -> ChatService:
    # shadow-dual-active T5: routed to the CURRENT account's own ChatService
    # (its own conversation history, memory context, queue) rather than
    # always the single paper instance -- `app.state.chat_services` is built
    # once at app startup (web/app.py's create_app) and shared with the
    # Telegram poller. A settings save invalidates the cached session in
    # place (ChatService.invalidate()) instead of swapping this out for a
    # new instance.
    return request.app.state.chat_services[current_account(request)]


def _render(request: Request, template: str, *, include_activity: bool) -> HTMLResponse:
    b = bundle(request)
    service = _service(request)
    pending = {r["id"]: dict(r) for r in b.queue.list("pending")}
    # Important 1: a chat strategy_revision row is filtered into this loop
    # by `source == 'chat'` (see _chat_messages.html), not by `kind` -- it
    # is NOT safe to reuse for approval the way a chat order row is (no
    # diff, no confirm dialog on this surface). The template routes it to a
    # link-to-/reviews card instead, but that card still owes the same
    # auto/status honesty the /reviews page gives it -- so compute the same
    # `revision_view` flags here, off the row's own parsed snapshot, rather
    # than let the glance on THIS page be less honest than the one on
    # /reviews.
    for item in pending.values():
        if item["kind"] == "strategy_revision":
            item.update(revision_view(item["source"], json.loads(item["snapshot"])))
    # `service.activity` is only meaningful for the turn that just ran --
    # ChatService.send() resets it at the start of that turn, but never
    # clears it once the turn is over (see chat_service.py). Left to render
    # unconditionally, a plain GET /chat -- a fresh page load, or a reload
    # hours later -- would show the previous turn's tool names as if they
    # were still in flight. Only the response to the POST /chat/send that
    # populated it shows it; every other render gets an empty list.
    # setup-wizard T4: the per-account onboarding card above the input.
    # `hint_import` is the wizard's own "Open Chat" link (routes/setup.py's
    # shadow-account step redirects to `/chat?hint=import`) -- it forces the
    # card back on even for an account that already has turns, since
    # arriving from that link means the user came here specifically to
    # start importing, not to read back old messages. Computed from the
    # request directly (not from `messages` below) so it stays correct
    # whether or not `service.messages()` below succeeds.
    hint_import = request.query_params.get("hint") == "import"
    messages: list[dict] = []
    activity: list[str] = []
    llm_error: str | None = None
    try:
        messages = service.messages()
        activity = service.activity if include_activity else []
    except LLMConfigError as exc:
        # `serve` only requires broker credentials -- the LLM key is
        # optional at startup precisely so Settings can be reached to add
        # one. ChatService._build() (chat_service.py) raises this the first
        # time anything touches the session, and nothing downstream of it
        # catches it -- without this, the default first-run path ("start
        # serve, open Chat") is a stack trace instead of a pointer to
        # Settings.
        llm_error = str(exc)
    # `messages` is still `[]` on the LLMConfigError path above (never
    # reassigned past the point of the raise), so an unconfigured LLM
    # naturally satisfies `len(messages) == 0` here too -- the card needs no
    # LLM call to render, so it isn't gated behind `llm_error` at all.
    onboarding = len(messages) == 0 or hint_import
    return templates.TemplateResponse(request, template, {
        "page": "chat", "messages": messages,
        "activity": activity, "pending": pending, "llm_error": llm_error,
        "onboarding": onboarding,
        **nav_context(request)})


@router.get("/chat", response_class=HTMLResponse)
def chat(request: Request) -> HTMLResponse:
    return _render(request, "chat.html", include_activity=False)


@router.post("/chat/send", response_class=HTMLResponse)
def send(request: Request, message: str = Form("")) -> HTMLResponse:
    text = message.strip()
    ran_turn = bool(text)
    if ran_turn:
        try:
            _service(request).send(text)
        except LLMConfigError:
            # No turn actually ran -- `_render` below hits the same
            # exception again (session() re-raises on every call while
            # unconfigured) and renders the banner; nothing to show as
            # "activity" for a turn that never reached the LLM.
            ran_turn = False
    return _render(request, "_chat_messages.html", include_activity=ran_turn)
