"""Image attachments on a chat turn.

Images are a *transient* input: they ride along on the one user message
being sent to the model and are dropped before anything durable sees the
turn (see `AgentSession.run_turn`'s `finally: message.pop("images")`).
Nothing here writes bytes to disk, logs them, or hands them to a store --
the only durable trace of an attachment is its `placeholder()` string,
which is what the transcript, the FTS index, and the Telegram mirror get.

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

_MAX_NAME_CHARS = 60
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
    return cleaned[:_MAX_NAME_CHARS]


def validate_images(items: list[tuple[bytes, str]]) -> list[ImageAttachment]:
    """`items` are `(data, filename)` pairs straight off the wire.

    Raises AttachmentError with the exact user-facing copy for each of the
    three rejection reasons. Callers are expected to have capped the read
    at MAX_IMAGE_BYTES already where the transport allows it; the size
    check here is the authoritative one either way."""
    if len(items) > MAX_IMAGES:
        raise AttachmentError(f"Up to {MAX_IMAGES} images per message.")
    out: list[ImageAttachment] = []
    for data, name in items:
        if len(data) > MAX_IMAGE_BYTES:
            raise AttachmentError(
                f"Image too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB).")
        mime = sniff_mime(data)
        # Belt and braces: `ALLOWED_MIMES` is the list the web form's
        # `accept=`, the Telegram document filter and the model-catalog hint
        # are all written against, so it -- not `sniff_mime`'s own set of
        # recognized signatures -- is what decides here. Teaching sniff_mime
        # a fourth format (say GIF) then stays a pure detection change and
        # cannot silently widen what the chat accepts.
        if mime is None or mime not in ALLOWED_MIMES:
            raise AttachmentError("Only PNG, JPEG, or WebP images are supported.")
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
