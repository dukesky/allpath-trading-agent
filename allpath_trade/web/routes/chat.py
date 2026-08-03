from __future__ import annotations

import threading

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from allpath_trade.llm.factory import LLMConfigError
from allpath_trade.web.chat_service import ChatService
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.templating import templates

router = APIRouter()

# Guards the get-or-create of `request.app.state.chat`: two requests racing
# on the very first hit to /chat could otherwise each see `chat` unset and
# each build their own ChatService, silently discarding one of them along
# with whatever AgentSession it had already built.
_service_lock = threading.Lock()


def _service(request: Request) -> ChatService:
    service = getattr(request.app.state, "chat", None)
    if service is None:
        with _service_lock:
            service = getattr(request.app.state, "chat", None)
            if service is None:
                service = ChatService(request.app.state.holder)
                request.app.state.chat = service
    return service


def _render(request: Request, template: str, *, include_activity: bool) -> HTMLResponse:
    c = request.app.state.holder.get()
    service = _service(request)
    pending = {r["id"]: dict(r) for r in c.queue.list("pending")}
    # `service.activity` is only meaningful for the turn that just ran --
    # ChatService.send() resets it at the start of that turn, but never
    # clears it once the turn is over (see chat_service.py). Left to render
    # unconditionally, a plain GET /chat -- a fresh page load, or a reload
    # hours later -- would show the previous turn's tool names as if they
    # were still in flight. Only the response to the POST /chat/send that
    # populated it shows it; every other render gets an empty list.
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
    return templates.TemplateResponse(request, template, {
        "page": "chat", "messages": messages,
        "activity": activity, "pending": pending, "llm_error": llm_error,
        **nav_context(c)})


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
