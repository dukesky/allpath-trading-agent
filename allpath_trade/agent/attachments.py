"""Image attachments on a chat turn.

Images are a *transient* input: they ride along on the one user message
being sent to the model and never touch the history dict at all. The turn's
attachments are held on the session (`AgentSession._pending_images`, cleared
in a `finally` on every exit path) and injected into the throwaway message
list built for the turn's FIRST `llm.complete` call by `loop._with_images`
-- so nothing that reads history, mid-turn or after, can observe bytes.

Nothing an attachment reaches after this module STORES it: no store, no log,
no file this app writes or keeps. The only durable trace of an attachment is
its `placeholder()` string, which is what the transcript, the FTS index and
the Telegram mirror get.

That is a claim about storage, not about every byte's whole journey through
the process. On the web path the HTTP layer sees the upload first: Starlette
spools any multipart part over 1 MB to an *unlinked* temporary file while
parsing the request, before this module (or the route) is reached at all.
That file has no name in the filesystem, belongs to nothing but the
in-flight request, and is released the moment the request ends. It is why
`routes/chat.py` rejects an over-large request from a middleware, on
`Content-Length`, before the form is parsed -- see MAX_UPLOAD_BYTES below.

Validation is deliberately *not* driven by the declared content type or
the filename: a browser upload and a Telegram document both let the
sender choose both freely. Only the magic bytes decide (spec ③).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_MIMES = ("image/png", "image/jpeg", "image/webp")

# Reply used when the configured chat model rejects the request because it
# cannot read images at all (see llm/base.py's LLMImageUnsupported and
# ChatService.send). Fixed copy: the user needs the two ways forward, not
# the provider's wording.
IMAGE_UNSUPPORTED_REPLY = ("This model can't read images — switch CHAT_MODEL to a "
                           "vision-capable model in Settings, or type the positions "
                           "instead.")

# The three fixed rejection messages (spec ③). Named because the transports
# reject before `validate_images` ever sees the bytes where they can: the
# web route (setup-wizard T6) caps each upload read at MAX_IMAGE_BYTES + 1
# and refuses an over-count before reading anything at all, and must raise
# the SAME copy the validator would have -- one string each, in one place,
# rather than a second hand-typed copy per transport.
TOO_MANY_MESSAGE = f"Up to {MAX_IMAGES} images per message."
TOO_LARGE_MESSAGE = f"Image too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB)."
BAD_TYPE_MESSAGE = "Only PNG, JPEG, or WebP images are supported."

# The whole-request ceiling (whole-branch review, Important 3): what the
# four per-part limits add up to, plus a megabyte for the text field, the
# multipart boundaries and the part headers. Enforced by a middleware on
# `Content-Length` alone, because the per-part caps in `routes/chat.py`
# cannot run until FastAPI has already parsed (and spooled) the body.
#
# Deliberately generous rather than exact: it is a ceiling on absurdity, not
# a second validator. Anything under it still faces every check above.
MAX_UPLOAD_BYTES = MAX_IMAGES * MAX_IMAGE_BYTES + 1024 * 1024
UPLOAD_TOO_LARGE_MESSAGE = "Upload too large."

# Default text for an images-only message (spec ③ allows empty text). The
# model gets a real sentence rather than an empty user turn, and the
# transcript reads as something a human could have typed.
IMAGES_ONLY_TEXT = "Here is an image."

# Public (whole-branch review, M11): chat.html's optimistic echo mirrors
# `_clean_name` client-side and needs the same ceiling, rendered from here
# rather than typed into the template as a second 60.
MAX_NAME_CHARS = 60
_WHITESPACE = re.compile(r"\s+")


class AttachmentError(ValueError):
    """A rejected attachment. The message is user-facing copy, shown
    verbatim by the web form and the Telegram bot -- no turn is recorded."""


@dataclass(frozen=True)
class ImageAttachment:
    data: bytes
    mime: str
    name: str

    @property
    def size(self) -> int:
        return len(self.data)

    def placeholder(self) -> str:
        # Half-up KB rounding via integer arithmetic (`round()` is
        # banker's rounding, which would render a 1.5 KB file as "2 KB"
        # but a 2.5 KB one as "2 KB" too). Never "0 KB": a file that
        # exists is at least 1 KB as far as the transcript is concerned.
        kb = max(1, (self.size + 512) // 1024)
        return f"[image: {self.name}, {kb} KB]"


def sniff_mime(data: bytes) -> str | None:
    """The declared type and the filename are attacker-controlled; these
    four byte patterns are not. Returns None for anything else, including
    an image type we deliberately don't accept (GIF, HEIC, SVG)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _clean_name(name: str) -> str:
    # `name` lands in placeholder(), i.e. in the transcript, the FTS index,
    # and the prompt the model reads. Collapse newlines/tabs (a multi-line
    # filename would break the one-line display) and bound the length.
    cleaned = _WHITESPACE.sub(" ", name or "").strip()
    if not cleaned:
        return "image"
    return cleaned[:MAX_NAME_CHARS]


def validate_images(items: list[tuple[bytes, str]]) -> list[ImageAttachment]:
    """`items` are `(data, filename)` pairs straight off the wire.

    Raises AttachmentError with the exact user-facing copy for each of the
    three rejection reasons. Callers are expected to have capped the read
    at MAX_IMAGE_BYTES already where the transport allows it; the size
    check here is the authoritative one either way."""
    if len(items) > MAX_IMAGES:
        raise AttachmentError(TOO_MANY_MESSAGE)
    out: list[ImageAttachment] = []
    for data, name in items:
        if len(data) > MAX_IMAGE_BYTES:
            raise AttachmentError(TOO_LARGE_MESSAGE)
        mime = sniff_mime(data)
        # Belt and braces: `ALLOWED_MIMES` is the list the web form's
        # `accept=`, the Telegram document filter and the model-catalog hint
        # are all written against, so it -- not `sniff_mime`'s own set of
        # recognized signatures -- is what decides here. Teaching sniff_mime
        # a fourth format (say GIF) then stays a pure detection change and
        # cannot silently widen what the chat accepts.
        if mime is None or mime not in ALLOWED_MIMES:
            raise AttachmentError(BAD_TYPE_MESSAGE)
        out.append(ImageAttachment(data=data, mime=mime, name=_clean_name(name)))
    return out


def placeholders(images: list[ImageAttachment] | None) -> str:
    return " ".join(i.placeholder() for i in images or [])


def display_for(images: list[ImageAttachment] | None, text: str) -> str:
    """The human-facing text for a turn: what the transcript, the FTS index
    and the Telegram mirror show. One helper so `run_turn`'s stored
    `display` and `ChatService.send`'s mirror text can never drift apart.

    An images-only message (empty `text`, allowed by spec ③) yields just the
    placeholders -- no trailing space."""
    prefix = placeholders(images)
    if not prefix:
        return text
    return f"{prefix} {text}" if text else prefix
