from __future__ import annotations

import json

from fastapi import APIRouter, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from allpath_trade.agent.attachments import (
    ALLOWED_MIMES,
    IMAGES_ONLY_TEXT,
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    MAX_NAME_CHARS,
    MAX_UPLOAD_BYTES,
    TOO_LARGE_MESSAGE,
    TOO_MANY_MESSAGE,
    UPLOAD_TOO_LARGE_MESSAGE,
    AttachmentError,
    ImageAttachment,
    validate_images,
)
from allpath_trade.config import normalize_llm_provider
from allpath_trade.llm.factory import LLMConfigError
from allpath_trade.web import models_catalog
from allpath_trade.web.account_ctx import bundle, current_account
from allpath_trade.web.chat_service import ChatService
from allpath_trade.web.deps import components
from allpath_trade.web.routes.dashboard import nav_context
from allpath_trade.web.routes.reviews import revision_view
from allpath_trade.web.templating import templates

router = APIRouter()

# The one route that accepts a body worth capping.
SEND_PATH = "/chat/send"


def install_upload_limit(app: FastAPI) -> None:
    """Refuse an over-large `POST /chat/send` before its body is parsed.

    Whole-branch review (Important 3). `_read_uploads` below is careful --
    it counts parts before reading any, and never reads more than
    MAX_IMAGE_BYTES + 1 from one -- but it runs far too late to matter for
    the body as a whole: FastAPI parses the multipart form to satisfy the
    handler's `images: list[UploadFile]` parameter BEFORE the handler is
    entered, and Starlette's parser spools every part over 1 MB to an
    unlinked temporary file as it goes. A client posting 500 MB of parts had
    all 500 MB written out before a single line of this module ran.

    Hence a middleware, and hence `Content-Length` -- the one thing that is
    known before the body is touched. A request that declares no length
    (chunked, or a client that omitted it) is let through to the per-part
    caps, which is exactly what they are for: this guard raises the floor,
    it does not replace them. A length that doesn't parse is treated the
    same as an absent one rather than as an error -- the parser downstream
    will have its own opinion about a body that doesn't match its header.

    Registered BEFORE `install_auth`, so the auth middleware wraps this one
    and an unauthenticated stranger still meets the login redirect first --
    a 413 is a fact about this install's limits, and there is no reason to
    hand it to someone who hasn't signed in.
    """

    @app.middleware("http")
    async def limit_upload(request: Request, call_next):
        if (request.method == "POST"
                and (request.url.path.rstrip("/") or "/") == SEND_PATH):
            raw = request.headers.get("content-length")
            try:
                length = int(raw) if raw is not None else None
            except ValueError:
                length = None
            if length is not None and length > MAX_UPLOAD_BYTES:
                # Plain text, not the usual `_chat_messages.html` fragment:
                # rendering that needs the account's transcript (blocking
                # sqlite reads, on the event loop, from a middleware), and
                # swapping a transcript-less fragment into #messages would
                # wipe the conversation off the screen to report a rejected
                # upload. chat.html's own htmx hook shows this as the same
                # inline notice a 400 produces, without swapping.
                return PlainTextResponse(UPLOAD_TOO_LARGE_MESSAGE,
                                         status_code=413)
        return await call_next(request)


def _service(request: Request) -> ChatService:
    # shadow-dual-active T5: routed to the CURRENT account's own ChatService
    # (its own conversation history, memory context, queue) rather than
    # always the single paper instance -- `app.state.chat_services` is built
    # once at app startup (web/app.py's create_app) and shared with the
    # Telegram poller. A settings save invalidates the cached session in
    # place (ChatService.invalidate()) instead of swapping this out for a
    # new instance.
    return request.app.state.chat_services[current_account(request)]


def _vision_hint(chat_model: str, provider: str) -> bool:
    """Whether the configured chat model is believed able to read images.

    True is the quiet default and covers every uncertain case: the hint is
    informational, so an unfetched catalog, a curated-list provider, or an
    unlisted slug must never produce a warning about a model that may well
    see fine. Only a catalog entry that exists AND does not list "image"
    among its input modalities flips this to False (see
    `models_catalog.cached_input_modalities`, which never touches the
    network -- /chat renders on every turn and cannot own an HTTP call).
    """
    # Normalized the same way `llm/factory.py` and `web/setup_status.py`
    # read LLM_PROVIDER (config.normalize_llm_provider) -- an `.env` with
    # `LLM_PROVIDER=OpenRouter` builds an OpenRouter client and passes the
    # setup gate, so it must consult the OpenRouter catalog here too rather
    # than falling into the "unknown provider" silence.
    modalities = models_catalog.cached_input_modalities(
        normalize_llm_provider(provider), chat_model)
    return modalities is None or "image" in modalities


def _render(request: Request, template: str, *, include_activity: bool,
            notice: str | None = None, status_code: int = 200) -> HTMLResponse:
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
    # Process-wide, not account-scoped: `bundle()` above can be an
    # AccountComponents (shadow), which deliberately carries no `settings`.
    settings = components(request).settings
    return templates.TemplateResponse(request, template, {
        "page": "chat", "messages": messages,
        "activity": activity, "pending": pending, "llm_error": llm_error,
        "onboarding": onboarding, "hint_import": hint_import,
        "notice": notice,
        # setup-wizard T6: the attach control's own limits are rendered from
        # the SAME constants the server validates against, so the inline
        # client-side check can't drift into rejecting what the server
        # accepts (or, worse, promising what it rejects).
        "vision_hint": _vision_hint(settings.chat_model, settings.llm_provider),
        "max_images": MAX_IMAGES, "max_image_bytes": MAX_IMAGE_BYTES,
        "allowed_mimes": list(ALLOWED_MIMES),
        # Whole-branch review (M3, M11, Important 3): the copy and the name
        # rules the inline script mirrors, from the Python constants rather
        # than hand-typed into the template a second time.
        "images_only_text": IMAGES_ONLY_TEXT,
        "upload_too_large_message": UPLOAD_TOO_LARGE_MESSAGE,
        "max_name_chars": MAX_NAME_CHARS,
        **nav_context(request)}, status_code=status_code)


@router.get("/chat", response_class=HTMLResponse)
def chat(request: Request) -> HTMLResponse:
    return _render(request, "chat.html", include_activity=False)


async def _read_uploads(uploads: list[UploadFile]) -> list[ImageAttachment]:
    """Turn the multipart parts into validated attachments, reading no more
    than the limits allow.

    Two caps, both BEFORE `validate_images` gets a chance to apply the
    authoritative ones:

    * the count is checked first, so an over-count request is refused
      without pulling a single part into this process's own memory --
      otherwise a client could make the handler materialize 100 x 5 MB just
      to be told "up to 4";
    * each part is read with `read(MAX_IMAGE_BYTES + 1)`, never unbounded.
      The extra byte is exactly enough to distinguish "at the limit" from
      "over it" without holding the overflow: an over-large part costs 5 MB
      of memory here, whatever its real size.

    Neither cap is reached before the request has been *parsed*, though --
    FastAPI resolves `images` (and so runs Starlette's multipart parser,
    which spools any part over 1 MB to an unlinked temporary file) before
    this function is entered. `install_upload_limit` above is what stops an
    absurd body ever getting that far; these caps bound what the handler
    itself then holds. Neither is a claim that a large upload never touched
    a file descriptor -- see `agent/attachments.py`'s module docstring.

    An empty file part (a browser that submits `<input type=file>` with
    nothing chosen sends one with an empty filename) is skipped rather than
    rejected as "not an image" -- it is the absence of an attachment, not a
    bad one, and it must not count toward the four either.
    """
    parts = [u for u in uploads if u is not None and (u.filename or "").strip()]
    if len(parts) > MAX_IMAGES:
        raise AttachmentError(TOO_MANY_MESSAGE)
    items: list[tuple[bytes, str]] = []
    for upload in parts:
        data = await upload.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise AttachmentError(TOO_LARGE_MESSAGE)
        items.append((data, upload.filename or ""))
    return validate_images(items)


# The `File(...)` marker as a module-level singleton rather than inline in
# the signature: ruff's B008 rejects a call in a default argument, and
# FastAPI evaluates this marker exactly once at import time either way, so
# hoisting it is behavior-identical. `[]` is the no-files default -- FastAPI
# copies it per request, so the shared literal is never mutated.
_IMAGE_UPLOADS = File([])


@router.post("/chat/send", response_class=HTMLResponse)
async def send(request: Request, message: str = Form(""),
               images: list[UploadFile] = _IMAGE_UPLOADS) -> HTMLResponse:
    """`async def` because the uploads are read with `await upload.read(...)`
    -- which in turn means every blocking call in here has to be handed to
    `run_in_threadpool` by hand, the way Starlette would have done for the
    whole handler while it was a plain `def`. A chat turn takes 10-60s
    inside `ChatService.send`; running that on the event loop would stall
    every other request in the process (the dashboard's own polling
    included) for the duration. `_render` is offloaded for the same reason
    on a smaller scale -- it does sqlite reads and a synchronous Jinja
    render.
    """
    text = message.strip()
    try:
        attachments = await _read_uploads(images)
    except AttachmentError as exc:
        # Nothing ran: no turn is recorded, the transcript is unchanged, and
        # the fragment comes back with the reason. 400 (not 200) so the
        # rejection is honest to anything reading the status; the template's
        # `htmx:before-swap` hook is what still lets the swap through so the
        # user actually sees `notice`.
        return await run_in_threadpool(
            _render, request, "_chat_messages.html", include_activity=False,
            notice=str(exc), status_code=400)
    if attachments and not text:
        # Spec ③: images alone are a legitimate message. The model gets a
        # sentence rather than an empty user turn -- `display_for` still
        # prefixes the placeholders, so the transcript reads
        # "[image: a.png, 12 KB] Here is an image."
        text = IMAGES_ONLY_TEXT
    ran_turn = bool(text)
    if ran_turn:
        try:
            await run_in_threadpool(_service(request).send, text,
                                    images=attachments or None)
        except LLMConfigError:
            # No turn actually ran -- `_render` below hits the same
            # exception again (session() re-raises on every call while
            # unconfigured) and renders the banner; nothing to show as
            # "activity" for a turn that never reached the LLM.
            ran_turn = False
    return await run_in_threadpool(_render, request, "_chat_messages.html",
                                   include_activity=ran_turn)
