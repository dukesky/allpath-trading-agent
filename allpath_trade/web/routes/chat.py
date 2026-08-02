from __future__ import annotations

import threading

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

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
    activity = service.activity if include_activity else []
    return templates.TemplateResponse(request, template, {
        "page": "chat", "messages": service.messages(),
        "activity": activity, "pending": pending, **nav_context(c)})


@router.get("/chat", response_class=HTMLResponse)
def chat(request: Request) -> HTMLResponse:
    return _render(request, "chat.html", include_activity=False)


@router.post("/chat/send", response_class=HTMLResponse)
def send(request: Request, message: str = Form("")) -> HTMLResponse:
    text = message.strip()
    ran_turn = bool(text)
    if ran_turn:
        _service(request).send(text)
    return _render(request, "_chat_messages.html", include_activity=ran_turn)
