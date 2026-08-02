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


def _render(request: Request, template: str) -> HTMLResponse:
    c = request.app.state.holder.get()
    service = _service(request)
    pending = {r["id"]: dict(r) for r in c.queue.list("pending")}
    return templates.TemplateResponse(request, template, {
        "page": "chat", "messages": service.messages(),
        "activity": service.activity, "pending": pending, **nav_context(c)})


@router.get("/chat", response_class=HTMLResponse)
def chat(request: Request) -> HTMLResponse:
    return _render(request, "chat.html")


@router.post("/chat/send", response_class=HTMLResponse)
def send(request: Request, message: str = Form("")) -> HTMLResponse:
    text = message.strip()
    if text:
        _service(request).send(text)
    return _render(request, "_chat_messages.html")
