"""Telegram Bot API transport (`TelegramAPI`) -- stdlib `urllib.request`
only, no new dependency, mirroring the precedent `notify/ntfy.py` already
set for this codebase's "one HTTP call, no SDK" shape.

This module owns the HTTP boundary only. `get_updates`/`send_message`/
`send_typing` never raise -- every failure mode (network error, HTTP error,
malformed JSON, a well-formed `{"ok": false, ...}` body) is caught here and
turned into a return value plus at most one `stderr` line, because the
caller (this module's own `TelegramPoller`, running on a daemon thread with
nothing watching for an uncaught exception) must never die from a flaky
Telegram API or a slow network.

Token scrubbing: every Telegram URL this module builds embeds the bot token
in its path -- `.../bot<token>/<method>` for the API endpoint,
`.../file/bot<token>/<file_path>` for the file endpoint `download_file`
GETs image bytes from -- so the token is in the URL string that shows up
inside
`urllib.error.URLError`/`HTTPError` messages (and in the request itself).
Every stderr line and every string derived from an exception message is run
through `TelegramAPI._scrub` first, which masks the token verbatim -- this
is the ONLY thing standing between a transient network error and the bot
token leaking into server logs, so it is applied unconditionally, not
opt-in per call site.
"""

from __future__ import annotations

import hmac
import html as _html
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from allpath_trade.agent.attachments import (
    ALLOWED_MIMES,
    IMAGES_ONLY_TEXT,
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    TOO_LARGE_MESSAGE,
    TOO_MANY_MESSAGE,
    AttachmentError,
    ImageAttachment,
    validate_images,
)
from allpath_trade.execution import ExecutionError
from allpath_trade.notify.events import _prefix
from allpath_trade.store.accounts import DEFAULT_ACCOUNT, is_valid_account
from allpath_trade.store.app_state import (
    TELEGRAM_ACCOUNT_KEY,
    TELEGRAM_CHAT_ID_KEY,
    TELEGRAM_OFFSET_KEY,
    TELEGRAM_USER_ID_KEY,
)
from allpath_trade.store.reviews import ReviewError, RevisionValidationError
from allpath_trade.web.markdown import (
    MAX_TELEGRAM_REPLY_CHUNKS,
    split_for_telegram,
    to_telegram_html,
)

_API_URL = "https://api.telegram.org/bot{token}/{method}"
# The file endpoint is a DIFFERENT host path from the API endpoint and is
# fetched with a plain GET (no JSON body) -- but it embeds the bot token the
# exact same way, so every stderr line derived from a download failure has to
# go through `_scrub` for the same reason `_API_URL`'s do.
_FILE_URL = "https://api.telegram.org/file/bot{token}/{path}"
_TOKEN_MASK = "***"

# Telegram's hard per-message ceiling (`sendMessage` rejects anything longer)
# and the much smaller one on `answerCallbackQuery`'s toast text.
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_TOAST_LIMIT = 200


def prefixed_chunks(account: str, html: str) -> list[str]:
    """Split already-converted `html` into sendable chunks with the
    `[Paper] `/`[Shadow] ` prefix on the FIRST chunk only (a prefix repeated
    on every chunk of one logical reply reads as noise).

    I2: the prefix's own length is subtracted from the split limit BEFORE
    splitting, not added to chunk 0 afterwards. Prefixing after the split
    was a silent truncation bug: `split_for_telegram` packs greedily up to
    exactly 4096, so a reply that filled a chunk to the ceiling became 4105
    characters once `[Shadow] ` was glued on, and Telegram rejected THAT
    chunk while happily delivering the rest -- a reply arriving with its
    head missing and nothing in the logs saying why.

    I3: takes HTML, never raw Markdown. The prefix has to go on after
    `to_telegram_html` has run, or the converter sees a line that starts
    with `[Shadow] ` and the markdown construct it was supposed to parse --
    a fenced code block, an ATX heading, a table row -- is no longer at the
    start of the line, so it isn't recognized at all.

    An unknown account degrades to no prefix (see `events._prefix`), which
    makes the limit arithmetic a no-op and this identical to a bare
    `split_for_telegram`."""
    prefix = _prefix(account)
    chunks = split_for_telegram(html, limit=TELEGRAM_MESSAGE_LIMIT - len(prefix))
    if not chunks:
        return []
    return [prefix + chunks[0], *chunks[1:]]


# sendMessage/sendChatAction are single, quick calls -- 10s matches the
# timeout ntfy.py already uses for the same shape of one-call HTTP POST.
_SEND_TIMEOUT_SECONDS = 10
# getUpdates is a long poll: Telegram holds the connection open for up to
# `timeout_s` seconds server-side before answering with an empty result, so
# the socket timeout must be strictly longer than that or every long poll
# would look like a network failure. +5s of slack for the response to
# actually arrive after Telegram's own wait completes -- with the default
# timeout_s=50 this is exactly the 55s the design spec calls for.
_GETUPDATES_TIMEOUT_MARGIN_SECONDS = 5

# The exact set of tags `web/markdown.py`'s `to_telegram_html` can ever
# produce (see its ALLOWED_TAGS-equivalent docstring there) -- this is the
# tag whitelist the plain-text fallback below strips, kept here rather than
# imported so this module's never-raises transport half has zero import-time
# dependency on the formatting module (Task 2 lands both in one PR, but they
# stay decoupled: a future change to markdown.py's tag set can't newly break
# this file's fallback without a test in *this* file catching it too).
_HTML_TAG_RE = re.compile(r"</?(?:b|code|pre)>")


def strip_telegram_html_to_plain(text: str) -> str:
    """Fallback for `send_message`'s HTML -> plain-text retry: Telegram
    rejected the HTML as an entity-parse failure, so send the same content
    back with the b/code/pre tags removed and the entities `to_telegram_html`
    escaped (`&amp;`, `&lt;`, ...) unescaped back to their literal
    characters -- otherwise the fallback message would read as literal
    "&lt;b&gt;" noise instead of the original text."""
    return _html.unescape(_HTML_TAG_RE.sub("", text))


# A file download is up to MAX_IMAGE_BYTES of body over a possibly-slow
# mobile uplink -- longer than a one-shot sendMessage, far shorter than the
# long poll.
_DOWNLOAD_TIMEOUT_SECONDS = 30


class FileTooLarge(Exception):
    """`download_file` found more bytes than `max_bytes` allowed.

    A dedicated exception rather than `None` because the two outcomes need
    DIFFERENT user-facing copy: a failed download is "try again" (transient,
    the user can just resend), an oversize one is "Image too large (max 5
    MB)." (permanent for that file, resending changes nothing). Collapsing
    both into `None` would make the poller tell a user with a 12 MB photo to
    keep retrying forever."""


class TelegramAPI:
    """Thin wrapper over three Telegram Bot API methods. `urlopen` is
    injectable (defaults to `urllib.request.urlopen`) so tests never touch
    api.telegram.org -- see `notify/ntfy.py`'s tests for the same pattern
    this module's tests follow."""

    def __init__(self, token: str, urlopen: Callable[..., Any] = urllib.request.urlopen) -> None:
        self.token = token
        self._urlopen = urlopen

    def _scrub(self, text: str) -> str:
        """Mask every occurrence of the bot token in `text`. A no-op on an
        empty token (nothing to leak) rather than replacing "" with "***"
        everywhere, which would otherwise corrupt any message when the
        token happens to be unset."""
        if not self.token:
            return text
        return text.replace(self.token, _TOKEN_MASK)

    def _request(self, method: str, payload: dict[str, Any]) -> urllib.request.Request:
        return urllib.request.Request(
            _API_URL.format(token=self.token, method=method),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def get_updates(self, offset: int, timeout_s: int = 50) -> list[dict[str, Any]] | None:
        """Long-poll `getUpdates`. Returns the `result` array on success
        (`[]` when Telegram genuinely has nothing new -- a normal long-poll
        timeout, not a failure), or `None` -- after exactly one scrubbed
        stderr line -- for ANY failure: a network/HTTP error, a response
        body that isn't valid JSON, a well-formed-but-`ok:false` body, or a
        body whose `result` isn't a list.

        The `[]`/`None` distinction is load-bearing for the poller
        (`TelegramPoller.poll_once`/`run_forever`): without it, a network
        outage and an empty long poll look identical, so the retry backoff
        never engages and the poller hot-loops against a dead transport
        instead of backing off."""
        req = self._request("getUpdates", {"offset": offset, "timeout": timeout_s})
        timeout = timeout_s + _GETUPDATES_TIMEOUT_MARGIN_SECONDS
        try:
            with self._urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            data = json.loads(body)
        except Exception as exc:  # noqa: BLE001 — transport must never raise
            print(f"[telegram] getUpdates failed: {self._scrub(str(exc))}", file=sys.stderr)
            return None
        if not isinstance(data, dict) or not data.get("ok"):
            print(f"[telegram] getUpdates failed: {self._scrub(str(data))}", file=sys.stderr)
            return None
        result = data.get("result")
        if not isinstance(result, list):
            print("[telegram] getUpdates failed: unexpected response shape", file=sys.stderr)
            return None
        return result

    def _send_once(self, chat_id: str, text: str, parse_mode: str | None,
                   reply_markup: dict[str, Any] | None = None) -> str:
        """One sendMessage attempt. Returns "ok", "http400" (so the caller
        can retry as plain text -- a 400 from Telegram's sendMessage is how
        an entity-parse failure surfaces), or "fail" for anything else.
        Always prints at most one scrubbed stderr line on failure."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        req = self._request("sendMessage", payload)
        try:
            with self._urlopen(req, timeout=_SEND_TIMEOUT_SECONDS) as resp:
                body = resp.read()
            data = json.loads(body)
        except urllib.error.HTTPError as exc:
            print(f"[telegram] sendMessage failed: {self._scrub(str(exc))}", file=sys.stderr)
            return "http400" if exc.code == 400 else "fail"
        except Exception as exc:  # noqa: BLE001 — transport must never raise
            print(f"[telegram] sendMessage failed: {self._scrub(str(exc))}", file=sys.stderr)
            return "fail"
        if not isinstance(data, dict) or not data.get("ok"):
            print(f"[telegram] sendMessage failed: {self._scrub(str(data))}", file=sys.stderr)
            return "fail"
        return "ok"

    def send_message(self, chat_id: str, html: str,
                     reply_markup: dict[str, Any] | None = None) -> bool:
        """Send `html` with `parse_mode=HTML`. On a Telegram 400 (entity
        parse failure -- e.g. an unbalanced tag slipped past
        `to_telegram_html`), retries EXACTLY ONCE as plain text via
        `strip_telegram_html_to_plain`. Never raises; returns whether the
        message was ultimately delivered.

        `reply_markup` (Approve/Reject inline keyboards on a queued-review
        push, see notify/dispatch.py) is passed through verbatim on BOTH the
        HTML attempt and the plain-text fallback -- the 400 that triggers
        the fallback is about the HTML entities in `text`, never about
        `reply_markup`, so there is no reason to drop the buttons on retry."""
        status = self._send_once(chat_id, html, "HTML", reply_markup)
        if status == "ok":
            return True
        if status == "http400":
            plain = strip_telegram_html_to_plain(html)
            return self._send_once(chat_id, plain, None, reply_markup) == "ok"
        return False

    def send_typing(self, chat_id: str) -> None:
        """Best-effort `sendChatAction(typing)`. Swallows everything --
        including a malformed response body -- because a missing typing
        indicator is never worth surfacing, let alone worth risking the
        poller loop on."""
        try:
            req = self._request("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            with self._urlopen(req, timeout=_SEND_TIMEOUT_SECONDS) as resp:
                resp.read()
        except Exception:  # noqa: BLE001, S110 — best-effort, swallow everything
            pass

    def get_file(self, file_id: str) -> str | None:
        """`getFile` -> `result.file_path`, the relative path the file
        endpoint serves the bytes under. Returns `None` -- after exactly one
        scrubbed stderr line -- for every failure mode, same never-raises
        contract as `get_updates`: a photo the poller can't resolve must
        become a "couldn't download that" reply, never an exception on the
        poller thread."""
        req = self._request("getFile", {"file_id": file_id})
        try:
            with self._urlopen(req, timeout=_SEND_TIMEOUT_SECONDS) as resp:
                body = resp.read()
            data = json.loads(body)
        except Exception as exc:  # noqa: BLE001 — transport must never raise
            print(f"[telegram] getFile failed: {self._scrub(str(exc))}", file=sys.stderr)
            return None
        if not isinstance(data, dict) or not data.get("ok"):
            print(f"[telegram] getFile failed: {self._scrub(str(data))}", file=sys.stderr)
            return None
        result = data.get("result")
        path = result.get("file_path") if isinstance(result, dict) else None
        if not isinstance(path, str) or not path:
            print("[telegram] getFile failed: unexpected response shape", file=sys.stderr)
            return None
        return path

    def download_file(self, file_path: str, max_bytes: int) -> bytes | None:
        """GET the file endpoint and return at most `max_bytes` of body.

        Raises `FileTooLarge` when the file is bigger than that, returns
        `None` (one scrubbed stderr line) for any other failure, and never
        raises anything else -- see `FileTooLarge` for why those two are
        distinct outcomes rather than one.

        The body is read as `read(max_bytes + 1)`, never unbounded: the one
        extra byte is exactly enough to tell "at the limit" from "over it"
        without materializing the overflow, so a 2 GB `file_path` costs 5 MB
        of memory here rather than 2 GB (the same bounded-read shape
        `web/routes/chat.py`'s upload handler uses). The bytes are returned
        to the caller and never written to disk or logged.

        `file_path` comes back from `getFile`, i.e. from Telegram -- but it
        is interpolated into a URL whose earlier path segment is the bot
        token, so a path that escaped its segment could aim this GET at
        another endpoint entirely. Rejected outright rather than trusted."""
        if (not file_path or file_path.startswith("/") or ".." in file_path
                or "://" in file_path or "\\" in file_path):
            print("[telegram] file download failed: unusable file_path", file=sys.stderr)
            return None
        url = _FILE_URL.format(token=self.token, path=file_path)
        req = urllib.request.Request(url, method="GET")
        try:
            with self._urlopen(req, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as resp:
                data = resp.read(max_bytes + 1)
        except Exception as exc:  # noqa: BLE001 — transport must never raise
            print(f"[telegram] file download failed: {self._scrub(str(exc))}",
                  file=sys.stderr)
            return None
        if not isinstance(data, bytes):
            print("[telegram] file download failed: unexpected body", file=sys.stderr)
            return None
        if len(data) > max_bytes:
            raise FileTooLarge(f"file exceeds {max_bytes} bytes")
        return data

    def delete_message(self, chat_id: str, message_id: int) -> None:
        """Best-effort `deleteMessage`. Used right after a successful `/start
        <token>` pairing to scrub the token out of the chat's visible
        history -- swallows everything, same as `send_typing`, because a
        failed delete (missing permission, message already gone, message
        too old for the bot to delete) is never worth surfacing, let alone
        worth risking the poller loop on."""
        try:
            req = self._request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
            with self._urlopen(req, timeout=_SEND_TIMEOUT_SECONDS) as resp:
                resp.read()
        except Exception:  # noqa: BLE001, S110 — best-effort, swallow everything
            pass

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        """Best-effort `answerCallbackQuery` -- clears the button's loading
        spinner and, when `text` is non-empty, shows Telegram's small toast
        popup over the chat. Used after a Approve/Reject inline-button tap
        (`TelegramPoller._handle_callback_query`) to give the presser
        immediate feedback. Swallows everything, same as `send_typing`/
        `delete_message`: a missing toast is never worth risking the poller
        loop on."""
        try:
            payload: dict[str, Any] = {"callback_query_id": callback_query_id}
            if text:
                payload["text"] = text
            req = self._request("answerCallbackQuery", payload)
            with self._urlopen(req, timeout=_SEND_TIMEOUT_SECONDS) as resp:
                resp.read()
        except Exception:  # noqa: BLE001, S110 — best-effort, swallow everything
            pass

    def edit_message_reply_markup(self, chat_id: str, message_id: int,
                                  reply_markup: dict[str, Any] | None = None) -> None:
        """Best-effort `editMessageReplyMarkup`. Used right after a
        review's Approve/Reject callback is resolved to remove the buttons
        from the original message -- a resolved review must not still show
        tappable buttons that would just fail (or double-resolve) on a
        second tap. `reply_markup=None` (the default) explicitly sends an
        empty inline keyboard rather than omitting the field, so this always
        removes buttons rather than leaving Telegram's own "no change"
        default ambiguous. Swallows everything, same as every other
        best-effort call in this class."""
        try:
            req = self._request("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": message_id,
                "reply_markup": reply_markup or {"inline_keyboard": []},
            })
            with self._urlopen(req, timeout=_SEND_TIMEOUT_SECONDS) as resp:
                resp.read()
        except Exception:  # noqa: BLE001, S110 — best-effort, swallow everything
            pass


# Backoff for `TelegramPoller.run_forever`'s retry loop: 5s to start, doubling
# on each consecutive failure, capped at 60s -- reset to the base the moment a
# poll succeeds again. Values match the design spec verbatim.
_BACKOFF_BASE_SECONDS = 5
_BACKOFF_CAP_SECONDS = 60

# Every poller stderr line is truncated to this many characters after
# scrubbing (see `TelegramPoller._log_error`) -- `chat_service.send` can
# raise with arbitrary user-supplied text embedded in the exception message,
# and an unbounded line is a way for a user to flood or obscure the log.
_STDERR_TRUNCATE_LENGTH = 200

# How often `_handle_chat_text`'s keepalive thread re-sends the typing
# indicator while `chat_service.send` is still running (Finding 6) --
# comfortably inside Telegram's own few-second expiry for a single
# `sendChatAction` call.
_TYPING_RESEND_SECONDS = 4

# `rv:<approve|reject>:<review_id>:<nonce>` -- the exact `callback_data`
# shape notify/dispatch.py's `push_telegram_review_queued` mints. `nonce` is
# always exactly 16 lowercase hex characters (the first 16 of a sha256
# hexdigest, see that module) -- requiring the exact length here means a
# truncated/garbled callback_data simply fails to match rather than being
# compared against a wrong-length slice of the stored hash.
_CALLBACK_RE = re.compile(r"^rv:(approve|reject):(\d+):([0-9a-f]{16})\Z")

# `acct:<paper|shadow>` -- the `/account` command's own inline-button
# callback_data (shadow-dual-active T5). No nonce: unlike an approve/reject
# button (which authorizes a real trading action and so needs the
# single-use-token belt described on `_resolve_review_callback`), switching
# which account THIS chat talks to is not itself a sensitive action -- the
# chat/user binding check every callback already goes through
# (`_handle_callback_query`) is the only gate this needs.
_ACCOUNT_CALLBACK_RE = re.compile(r"^acct:(paper|shadow)\Z")

# The account a fresh (or pre-T5) pairing's chat talks to before anyone has
# ever run `/account` -- the spec's chosen default for the Telegram surface,
# independent of the web UI's own default (`store.accounts.DEFAULT_ACCOUNT`,
# "paper" -- spec: "手机和电脑各自有上下文").
_DEFAULT_TELEGRAM_ACCOUNT = "shadow"

# setup-wizard T7. The third rejection reply the Telegram surface needs and
# the web one doesn't: a browser upload arrives with the request, a Telegram
# photo has to be fetched back out of Telegram in two more round-trips
# (`getFile` then the file endpoint), either of which can just fail.
# Deliberately "try again": unlike the size/type refusals, resending the
# same photo genuinely can work.
DOWNLOAD_FAILED_MESSAGE = "Couldn't download that image — try again."

# `message.photo` carries no filename (Telegram re-encodes camera photos and
# throws the original name away), so this is what lands in the transcript
# placeholder -- "[image: photo, 812 KB]". No extension: the real type comes
# from `sniff_mime`, and inventing ".jpg" here would be a second, possibly
# wrong claim about the same bytes. Image *documents* keep their own
# `file_name`.
_PHOTO_NAME = "photo"


def _image_ref(message: dict[str, Any]) -> tuple[str, str] | None:
    """`(file_id, name)` for a message carrying ONE importable image, else
    `None` (every other message shape -- text, sticker, voice, video, a
    non-image document -- which the poller ignores exactly as it always
    has).

    Two accepted shapes, both from the spec:

      * `photo`: a list of `PhotoSize`s for the SAME image in ascending size
        order. The last one is the original-resolution version; the earlier
        entries are Telegram's own thumbnails, and downloading one of those
        would hand the model a 90px blur of the chart the user meant to
        show. Entries without a usable `file_id` are skipped rather than
        crashing the batch.
      * `document`: an image sent "as a file" (which is how a screenshot
        keeps its pixels intact -- Telegram recompresses `photo`). Filtered
        on the DECLARED `mime_type` here only as a cheap pre-filter for
        which documents are worth fetching at all; the authoritative check
        is `validate_images`' magic-byte sniff after the bytes arrive, since
        `mime_type` is entirely sender-controlled.
    """
    photo = message.get("photo")
    if isinstance(photo, list):
        sizes = [p for p in photo
                 if isinstance(p, dict) and isinstance(p.get("file_id"), str) and p["file_id"]]
        return (sizes[-1]["file_id"], _PHOTO_NAME) if sizes else None
    document = message.get("document")
    if isinstance(document, dict) and document.get("mime_type") in ALLOWED_MIMES:
        file_id = document.get("file_id")
        if isinstance(file_id, str) and file_id:
            name = document.get("file_name")
            return file_id, name if isinstance(name, str) else _PHOTO_NAME
    return None


def _album_key(chat_id: Any, from_id: Any, group_id: Any) -> tuple[str, str, str] | None:
    """The album bucket a message belongs to, or `None` when it belongs to
    no album (no usable `media_group_id`, or an origin this poller could not
    identify).

    Keyed on `(chat_id, from_id, media_group_id)`, NOT on `media_group_id`
    alone (round-1 Important). `media_group_id` is scoped to the sender that
    minted it, so two different chats can present the same value in one
    batch -- and only ONE member of a group is pairing-checked (the one that
    triggers dispatch). Keying on the id alone therefore let a batch of
    `[stranger's photo(mg1), paired user's photo(mg1)]` splice the
    stranger's image into the paired user's turn -- the stranger's bytes
    downloaded and handed to the model, past a gate they never cleared --
    and, with the order reversed, made the paired user's own album vanish
    with no reply and no turn (dispatch fell to the stranger's message,
    which the gate then dropped). Both halves close the moment a group can
    only ever contain messages from one chat AND one sender."""
    if chat_id is None or from_id is None:
        return None
    if not isinstance(group_id, str) or not group_id:
        return None
    return str(chat_id), str(from_id), group_id


def _index_albums(updates: list[dict[str, Any]]) -> dict[tuple[str, str, str],
                                                         list[dict[str, Any]]]:
    """`{(chat_id, from_id, media_group_id): [message, ...]}` for the
    image-bearing messages of this ONE `getUpdates` batch, in arrival order.
    See `_album_key` for why the key is a triple.

    Telegram delivers an album as N separate updates that share a
    `media_group_id`; they have to be collected before the turn can run, or
    a 3-photo album becomes three separate agent turns each seeing one
    photo. Building the index up front (rather than buffering across calls)
    keeps `_handle_update` a pure function of the batch and means no
    half-collected album can ever be left in memory: the group is processed
    when its LAST message in the batch is reached.

    ORDERING: because an album is dispatched at its LAST member, a plain
    text message sitting between an album's first and last part in the same
    batch runs BEFORE the album does, even though the user sent the photos
    first. Accepted -- the alternative is buffering the whole batch and
    re-ordering it, and both turns still happen, in one poll, serialized by
    `ChatService._turn_lock`.

    KNOWN LIMIT (accepted, spec-level): an album SPLIT across two
    `getUpdates` batches -- possible when Telegram delivers the parts across
    a poll boundary -- becomes two turns. Buffering across polls would mean
    holding image bytes and a timer between polls, which is a materially
    bigger change than the failure mode is worth."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for update in updates:
        # This pre-pass runs OUTSIDE `poll_once`'s per-update try/except, so
        # a malformed batch entry (Telegram sending something that isn't an
        # object, or a test's deliberately-broken update) must be skipped
        # here rather than taking the whole batch down before any of it is
        # handled.
        if not isinstance(update, dict):
            continue
        message = update.get("message")
        if not isinstance(message, dict) or _image_ref(message) is None:
            continue
        chat = message.get("chat")
        from_ = message.get("from")
        key = _album_key(chat.get("id") if isinstance(chat, dict) else None,
                         from_.get("id") if isinstance(from_, dict) else None,
                         message.get("media_group_id"))
        if key is not None:
            groups.setdefault(key, []).append(message)
    return groups


class TelegramPoller:
    """Long-polls Telegram's `getUpdates`, pairs a single chat via `/start
    <web_token>`, and forwards paired-chat text into `chat_service` --
    replying with the agent's turn.

    `poll_once` is the unit tests drive directly: one `getUpdates` call, one
    pass over whatever updates came back. `run_forever` is the thin loop
    `serve` runs on a daemon thread -- call `poll_once` forever, backing off
    on repeated failure, until `stop` is set.

    `chat_service` (shadow-dual-active T5) is duck-typed as `send(text: str,
    source: str = "web") -> str` -- that `source` keyword is `ChatService`'s
    (Task 4 in the design plan); this class only ever needs the interface,
    not the concrete type, so it has no import-time dependency on
    `chat_service.py`. As of T5 the constructor also accepts a `{account:
    chat_service}` MAPPING -- production (`web/app.py`'s `_start_telegram`)
    always passes `app.state.chat_services`, routing paired-chat text to
    whichever account this chat is currently talking to (the `/account`
    command, defaulting to shadow -- see `_telegram_account`). A bare
    single object (every pre-T5 test's `FakeChatService()`) is wrapped
    transparently into `{"paper": obj, "shadow": obj}` by `__init__` -- both
    "accounts" resolve to the exact same object, so single-account test
    behavior is completely unchanged; only real, dict-based multi-account
    callers see any different routing.

    `holder` is duck-typed as `get() -> object` where the returned object
    exposes `.app_state` and `.settings.web_token` (the shape
    `web.deps.ComponentHolder.get()` -- a `Components` instance -- already
    has). Both are read fresh via `holder.get()` at the point of use, NEVER
    captured once in `__init__`, for two review findings this class used to
    get wrong:

      * (Finding 2, security) A snapshotted `web_token` never picked up a
        `/settings/reset-token` save -- the poller kept validating `/start`
        against the OLD token forever (until process restart), so a leaked
        old token could still pair via Telegram after the user believed
        they'd revoked it, and the freshly reset token was rejected. Reading
        `holder.get().settings.web_token` at compare time fixes both halves
        at once: the old token stops working and the new one starts working
        the instant the reset takes effect, no restart required.
      * (Finding 5) A snapshotted `app_state` object outlives a `db_path`
        change from `ComponentHolder.rebuild()` -- `_current_offset` and
        every other read/write in this class would keep hitting the OLD
        (possibly-closed) sqlite connection forever, a permanent error loop
        indistinguishable from a dead network from `run_forever`'s point of
        view. Reading `holder.get().app_state` at each use closes this the
        same way."""

    def __init__(self, api: TelegramAPI, chat_service: Any, holder: Any,
                 stop: threading.Event) -> None:
        self.api = api
        self._chat_services: dict[str, Any] = (
            chat_service if isinstance(chat_service, dict)
            else {"paper": chat_service, "shadow": chat_service})
        self.holder = holder
        self.stop = stop

    @property
    def chat_service(self) -> Any:
        """The ChatService for whichever account THIS chat is currently
        talking to (`_telegram_account`) -- see `__init__`'s docstring for
        why this is a property (account-routed) rather than the plain
        attribute it used to be. Falls back to whatever `"paper"` maps to
        if the current account key is somehow missing from the dict (should
        never happen in production -- `_chat_services` is always seeded
        with both `store.accounts.ACCOUNTS` entries -- but a partial/custom
        test double is not required to provide both keys)."""
        services = self._chat_services
        return services.get(self._telegram_account(), services.get(DEFAULT_ACCOUNT))

    def _telegram_account(self) -> str:
        """Which account (`paper`/`shadow`) THIS paired chat currently
        talks to -- independent of the web UI's own `account` cookie (spec:
        "手机和电脑各自有上下文"). Unset (a fresh pairing, or a pre-T5
        pairing predating `TELEGRAM_ACCOUNT_KEY`) or an unrecognized stored
        value both read as `_DEFAULT_TELEGRAM_ACCOUNT` ("shadow") -- same
        "never resolve to an unscoped/third partition, degrade to a known
        value instead" posture as `web/account_ctx.py`'s `current_account`."""
        raw = self.app_state.get(TELEGRAM_ACCOUNT_KEY)
        return raw if raw and is_valid_account(raw) else _DEFAULT_TELEGRAM_ACCOUNT

    @property
    def app_state(self) -> Any:
        """Always the CURRENT `AppState` -- see the class docstring's
        Finding 5 note. A `@property` (not a plain attribute) so every one
        of this class's many `self.app_state.get/set(...)` call sites below
        stays unchanged; only `__init__` and this accessor needed to
        change."""
        return self.holder.get().app_state

    def _web_token(self) -> str:
        """Always the CURRENT web token -- see the class docstring's
        Finding 2 note. A method, not a `@property`, to make the "read at
        compare time" intent explicit at the one call site that uses it
        (`_handle_pairing`)."""
        return self.holder.get().settings.web_token

    # -- single poll step -----------------------------------------------

    def poll_once(self) -> str:
        """One `getUpdates` call plus one pass over its results. Every
        per-update failure (a malformed update, an exception raised while
        handling it) is caught right there so it can never take the rest of
        the batch down with it -- see `_handle_update`'s per-update
        try/except below.

        Returns `"failed"` when `run_forever` should back off, `"ok"`
        otherwise:

          * `get_updates` returning `None` (transport failure, see its own
            docstring) is always `"failed"`.
          * A non-empty batch where NOT ONE update advanced the offset is
            also `"failed"`, even though `get_updates` itself succeeded --
            e.g. every update in the batch raised while being processed
            (a disk-full `app_state.set`), or Telegram sent update_ids this
            code can't parse as `int`. Without this, the next poll would
            fetch the exact same batch again (offset never moved) and do so
            in a tight loop with no backoff -- a hot-loop that looks
            identical to genuine progress from `run_forever`'s point of
            view unless it's told otherwise.
        """
        offset = self._current_offset()
        updates = self.api.get_updates(offset=offset, timeout_s=50)
        if updates is None:
            return "failed"
        dropped = 0
        advanced_any = False
        # Built once per batch, before anything is handled: an album's
        # parts arrive as separate updates and have to be seen together
        # (see `_index_albums`).
        albums = _index_albums(updates)
        for update in updates:
            try:
                # Offset is persisted the instant an update is received --
                # BEFORE any processing of it. This is deliberate at-most-once
                # semantics (verbatim from the design spec): a mid-turn crash
                # must drop the message (visible: the user just resends it)
                # rather than replay it on restart (invisible: a duplicate
                # order proposal re-enters the pending queue). This tradeoff
                # is fixed, not configurable.
                if self._advance_offset(update):
                    advanced_any = True
                if self._handle_update(update, albums) == "dropped":
                    dropped += 1
            except Exception as exc:  # noqa: BLE001 — one bad update must not kill the batch
                self._log_error(f"failed to process update: {exc}")
        if dropped:
            # At most one stderr line per batch, however many stranger
            # messages or failed pairing attempts (wrong/non-ASCII token,
            # non-private /start) showed up in it -- a burst must not spam
            # the log one line per message, but the count still gives an
            # operator visibility into a brute-force attempt without ever
            # replying to the attacker.
            self._log_error(f"dropped {dropped} message(s) from unpaired chat(s) "
                             "or failed pairing attempt(s)")
        if updates and not advanced_any:
            return "failed"
        return "ok"

    def _log_error(self, message: str) -> None:
        """Every poller stderr line goes through this: scrubbed (the token
        can end up embedded in an exception's message the same way it can
        in the transport, see `TelegramAPI._scrub`'s docstring) and
        truncated -- `chat_service.send` can raise with the user's own
        message text inside the exception, and an unbounded line is a log
        an attacker can use to flood or hide behind."""
        scrubbed = self.api._scrub(message)
        print(f"[telegram] {scrubbed[:_STDERR_TRUNCATE_LENGTH]}", file=sys.stderr)

    def _current_offset(self) -> int:
        raw = self.app_state.get(TELEGRAM_OFFSET_KEY)
        try:
            return int(raw) if raw is not None else 0
        except ValueError:
            return 0

    def _advance_offset(self, update: dict[str, Any]) -> bool:
        """Returns whether the offset was actually advanced -- `False` for
        an update whose `update_id` isn't an `int` (malformed/adversarial
        payload), which `poll_once` needs to know about: see its own
        docstring for why a batch that never advances the offset must be
        treated as a failure."""
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            self.app_state.set(TELEGRAM_OFFSET_KEY, str(update_id + 1))
            return True
        return False

    def _handle_update(
            self, update: dict[str, Any],
            albums: dict[tuple[str, str, str], list[dict[str, Any]]] | None = None,
    ) -> str | None:
        """Returns `"dropped"` when the update was a text or image message
        (or an Approve/Reject button tap) from an unpaired/unmatched sender,
        or a failed `/start` pairing attempt (so `poll_once` can count it
        for its one stderr line), `None` otherwise -- including every other
        update shape (stickers, voice notes, non-image documents,
        `edited_message`, ...), which are simply ignored: this bot
        understands plain text, importable images (setup-wizard T7), and its
        own review-approval callback buttons.

        `albums` is the batch's `_index_albums` map; omitted (tests calling
        this directly) means every image message is treated as a singleton."""
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            return None if self._handle_callback_query(callback) else "dropped"

        message = update.get("message")
        if not isinstance(message, dict):
            return None
        # An image message's text is its `caption` (Telegram never sets both
        # `text` and `photo`/`document`), and an absent caption is a
        # legitimate images-only message, not a reason to ignore the update.
        image = _image_ref(message)
        if image is None:
            text = message.get("text")
            if not isinstance(text, str):
                return None
        else:
            caption = message.get("caption")
            text = caption if isinstance(caption, str) else ""
        chat = message.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        if chat_id is None:
            return None
        chat_id = str(chat_id)
        chat_type = chat.get("type") if isinstance(chat, dict) else None
        from_ = message.get("from")
        from_id = from_.get("id") if isinstance(from_, dict) else None

        # Exact command match only: "/start" is the pairing command,
        # "/starting a position..." (or any other text that merely begins
        # with the same five characters) is ordinary chat text and must
        # flow to chat_service like anything else. The "@botname" suffix
        # (how a command reads when it's @-mentioned in a group) is
        # stripped before comparing.
        # Commands are only ever read off a TEXT message. A caption is
        # something the user wrote *about* the attached image -- treating
        # "/account" under a photo as the account command would pop the
        # switcher and silently swallow the image the user actually sent.
        parts = text.split(maxsplit=1) if image is None else []
        command = parts[0].split("@", 1)[0] if parts else ""
        if command == "/start":
            token = parts[1].strip() if len(parts) > 1 else ""
            message_id = message.get("message_id")
            paired = self._handle_pairing(chat_id, chat_type, from_id, token, message_id)
            return None if paired else "dropped"

        paired_chat_id = self.app_state.get(TELEGRAM_CHAT_ID_KEY)
        paired_user_id = self.app_state.get(TELEGRAM_USER_ID_KEY)
        # Both the chat id AND the sender's user id (recorded at pairing
        # time) must match. Belt-and-suspenders on top of the chat id check:
        # even inside the one paired chat, a forwarded message or an
        # anonymous-admin post can carry a `from.id` that isn't the person
        # who paired -- this fails that closed rather than trusting the
        # chat id alone.
        if (paired_chat_id is None or chat_id != paired_chat_id
                or paired_user_id is None or from_id is None
                or str(from_id) != paired_user_id):
            # No reply of any kind, not even to confirm the bot is alive.
            # Counted by the caller, not logged per-message.
            return "dropped"

        # shadow-dual-active T5: "/account" (paired chats only -- checked
        # above, same gate every other paired-chat command goes through)
        # offers the inline Paper/Shadow switcher. Exact command match, same
        # "@botname"-suffix stripping as "/start" above.
        if command == "/account":
            self._handle_account_command(chat_id)
            return None

        if image is not None:
            # `chat_id`/`from_id` here are this message's own, already
            # checked against the pairing above -- so the group this looks
            # up can only ever hold messages from the same cleared
            # chat+sender (see `_album_key`).
            key = _album_key(chat_id, from_id, message.get("media_group_id"))
            group = (albums or {}).get(key) if key is not None else None
            if group is None:
                self._handle_image_message(chat_id, [message])
            elif message is group[-1]:
                # The whole album runs once, on its last part in this batch;
                # the earlier parts were already gathered by `_index_albums`.
                self._handle_image_message(chat_id, group)
            return None

        self._handle_chat_text(chat_id, text)
        return None

    def _handle_image_message(self, chat_id: str, messages: list[dict[str, Any]]) -> None:
        """One image message, or one album, becoming one chat turn.

        Every rejection is answered with the SAME fixed copy the web upload
        path uses (`agent/attachments.py`) and runs no turn at all -- the
        transcript is untouched, and the user can just resend. The count
        check comes first, before any network call: refusing a 9-photo album
        must not cost nine downloads to discover.

        Bytes live in this frame and in the `ImageAttachment`s handed to
        `ChatService.send` -- never written to disk, never logged, never
        stored (see `attachments.py`'s module docstring for where they go
        from there)."""
        refs = [ref for m in messages if (ref := _image_ref(m)) is not None]
        if not refs:
            return
        if len(refs) > MAX_IMAGES:
            self.api.send_message(chat_id, TOO_MANY_MESSAGE)
            return
        # The album's caption: Telegram puts it on whichever part the sender
        # attached it to, so the first non-empty one is the message.
        text = ""
        for m in messages:
            caption = m.get("caption")
            if isinstance(caption, str) and caption.strip():
                text = caption
                break

        # Downloading up to four 5 MB files over Telegram's file endpoint
        # takes real seconds BEFORE `_handle_chat_text`'s own indicator
        # starts -- without this the chat looks dead for that whole stretch.
        self.api.send_typing(chat_id)
        items: list[tuple[bytes, str]] = []
        for file_id, name in refs:
            file_path = self.api.get_file(file_id)
            if file_path is None:
                self.api.send_message(chat_id, DOWNLOAD_FAILED_MESSAGE)
                return
            try:
                data = self.api.download_file(file_path, MAX_IMAGE_BYTES)
            except FileTooLarge:
                self.api.send_message(chat_id, TOO_LARGE_MESSAGE)
                return
            if data is None:
                self.api.send_message(chat_id, DOWNLOAD_FAILED_MESSAGE)
                return
            items.append((data, name))

        try:
            images = validate_images(items)
        except AttachmentError as exc:
            # `str(exc)` is the same user-facing copy the web form shows --
            # one source of truth for the wording, not a second hand-typed
            # set here.
            self.api.send_message(chat_id, str(exc))
            return

        # Spec ③: images alone are a legitimate message; the model gets a
        # real sentence rather than an empty user turn, the same default the
        # web route applies.
        self._handle_chat_text(chat_id, text or IMAGES_ONLY_TEXT, images=images)

    def _handle_account_command(self, chat_id: str) -> None:
        """`/account` reply: an inline Paper/Shadow keyboard
        (`callback_data` `acct:<name>`, handled by `_handle_callback_query`
        below). Tells the user which account this chat is on right now, not
        just offering the choice blind."""
        current = self._telegram_account()
        keyboard = {"inline_keyboard": [[
            {"text": "Paper", "callback_data": "acct:paper"},
            {"text": "Shadow", "callback_data": "acct:shadow"},
        ]]}
        self.api.send_message(
            chat_id,
            f"This chat is currently on <b>{current.capitalize()}</b>. "
            "Choose which account it talks to:",
            reply_markup=keyboard)

    def _handle_pairing(self, chat_id: str, chat_type: Any, from_id: Any,
                         token: str, message_id: Any) -> bool:
        """`/start <token>` pairing. Returns whether pairing succeeded.

        Requires ALL of: a private chat (`chat.type == "private"` --
        pairing from a group/supergroup/channel is rejected outright, since
        this bot is single-user-DM only by design and a group /start would
        pair the bot to a shared chat everyone in it can then talk through),
        a known sender id, and a correct token (constant-time compare, both
        sides encoded utf-8 first -- see `hmac.compare_digest`'s own
        restriction against comparing `str`s that contain non-ASCII
        characters, and `web/auth.py`'s `_authorized` for the same pattern
        already established in this codebase).

        On success: stores the chat id AND the pairing user's id, replies
        once, and best-effort deletes the `/start` message (it has the web
        token in it). On failure (wrong/missing token, non-private chat):
        NO reply of any kind -- confirming the bot is alive to a guesser is
        itself information leakage. Re-pairing (a second correct `/start`
        from a different private chat) simply overwrites the stored chat id
        and user id; this is single-chat-only by design."""
        if chat_type != "private":
            return False
        if from_id is None:
            return False
        web_token = self._web_token()
        if not token or not web_token:
            return False
        if not hmac.compare_digest(token.encode("utf-8"), web_token.encode("utf-8")):
            return False
        self.app_state.set(TELEGRAM_CHAT_ID_KEY, chat_id)
        self.app_state.set(TELEGRAM_USER_ID_KEY, str(from_id))
        self.api.send_message(
            chat_id,
            "Paired. This chat now talks to your AllPath agent. "
            "(I tried to delete your /start message — if it is still "
            "visible, delete it: it contains your web token.)",
        )
        if isinstance(message_id, int):
            self.api.delete_message(chat_id, message_id)
        return True

    # -- Approve/Reject inline-button callbacks --------------------------

    def _handle_callback_query(self, callback: dict[str, Any]) -> bool:
        """A tap on one of the Approve/Reject buttons
        `notify/dispatch.py`'s `push_telegram_review_queued` attaches to a
        queued-review push. Returns whether the callback was handled (i.e.
        came from the paired chat+user) -- `False` means `poll_once` counts
        it toward the batch's dropped total, same silent-drop policy the
        text-message branch above already follows: a stranger's tap gets NO
        reply of any kind, not even Telegram's own loading-spinner toast.

        The chat/user binding check is IDENTICAL to the text-message path
        (`_handle_update` above): both the chat id (read off
        `callback.message.chat.id` -- the chat the ORIGINAL button message
        lives in) and the presser's own id (`callback.from.id`) must match
        what `/start` recorded. This is the actual authorization boundary --
        `callback_data`'s nonce (checked once a callback clears this gate,
        see `_resolve_review_callback`) is only a belt on top of it, not a
        substitute for it."""
        callback_id = callback.get("id")
        message = callback.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        message_id = message.get("message_id") if isinstance(message, dict) else None
        from_ = callback.get("from")
        from_id = from_.get("id") if isinstance(from_, dict) else None
        data = callback.get("data")

        if chat_id is None or from_id is None or not isinstance(data, str):
            return False
        chat_id = str(chat_id)
        paired_chat_id = self.app_state.get(TELEGRAM_CHAT_ID_KEY)
        paired_user_id = self.app_state.get(TELEGRAM_USER_ID_KEY)
        if (paired_chat_id is None or chat_id != paired_chat_id
                or paired_user_id is None or str(from_id) != paired_user_id):
            return False

        account_match = _ACCOUNT_CALLBACK_RE.match(data)
        if account_match:
            self._handle_account_callback(
                account_match.group(1), chat_id, message_id, callback_id)
            return True

        match = _CALLBACK_RE.match(data)
        if not match:
            # Malformed/unrecognized callback_data from an otherwise
            # legitimately-paired sender (e.g. a stale button from before a
            # format change) -- answer so the spinner clears, but this is
            # still a HANDLED update, not a dropped one: the sender is who
            # they say they are, there's just nothing to act on.
            if isinstance(callback_id, str):
                self.api.answer_callback_query(callback_id, "Unrecognized action.")
            return True

        action, review_id_raw, nonce = match.groups()
        outcome_line, toast, acted = self._resolve_review_callback(
            int(review_id_raw), nonce, action)
        if isinstance(callback_id, str):
            self.api.answer_callback_query(callback_id, toast)
        if not acted:
            # Nothing changed (bad/expired nonce, already resolved
            # elsewhere, unknown review): leave the original message and its
            # buttons exactly as they were -- there is nothing to report and
            # removing live buttons here would be actively wrong for the
            # "already resolved elsewhere" case, where a SECOND tap should
            # still surface the same "already resolved" toast rather than
            # silently doing nothing with no buttons left to explain why.
            return True
        if isinstance(message_id, int):
            self.api.edit_message_reply_markup(chat_id, message_id, reply_markup=None)
        self.api.send_message(chat_id, to_telegram_html(outcome_line))
        return True

    def _handle_account_callback(self, account: str, chat_id: str,
                                 message_id: Any, callback_id: Any) -> None:
        """A tap on the `/account` command's Paper/Shadow inline button.
        Persists the choice (`TELEGRAM_ACCOUNT_KEY`), removes the buttons
        (a resolved choice, same "no live buttons on a settled action"
        pattern the review Approve/Reject buttons follow), and confirms in
        chat -- so switching is never silent."""
        self.app_state.set(TELEGRAM_ACCOUNT_KEY, account)
        if isinstance(callback_id, str):
            # Prefixed like every other callback toast, and from the same
            # `events._prefix` -- an invalid account (already gated by
            # `_ACCOUNT_CALLBACK_RE` before reaching here) degrades to no
            # prefix instead of inventing a label for it.
            toast = f"Switched to {account.capitalize()}"
            self.api.answer_callback_query(callback_id, toast[:TELEGRAM_TOAST_LIMIT])
        if isinstance(message_id, int):
            self.api.edit_message_reply_markup(chat_id, message_id, reply_markup=None)
        self.api.send_message(
            chat_id,
            f"Switched to <b>{account.capitalize()}</b>. "
            f"This chat now talks to your {account} account.")

    def _resolve_review_callback(self, review_id: int, nonce: str,
                                  action: str) -> tuple[str, str, bool]:
        """Approve/reject `review_id` through the exact same
        `ReviewQueue.approve()`/`reject()` the web reviews page uses (see
        `web/routes/reviews.py`'s `approve`/`reject` handlers, which this
        mirrors kind-for-kind: `strategy_revision` vs `order`,
        `RevisionValidationError` left pending, `ExecutionError` claimed-but-
        failed). Returns `(outcome_line, toast, acted)`:

          * `outcome_line` is sent back to Telegram as a follow-up message
            once the original message's buttons are removed, AND fed to
            `ChatService.note_resolution` (for `source == "chat"` rows
            only, matching `web/routes/reviews.py`'s own `_echo_resolution`
            gate -- a sentinel-triggered row has no live conversation to
            report back into) so the web chat shows the same receipt.
          * `toast` is the short string `answerCallbackQuery` pops up.
          * `acted` is whether the row was actually touched (a real
            approve/reject attempt ran, even one that failed) -- `False` for
            the "nothing to do" cases (missing row, wrong/expired nonce,
            already resolved), which `_handle_callback_query` uses to decide
            whether to remove the buttons/send a follow-up message at all.

        The nonce check happens here, AFTER the chat/user binding
        `_handle_callback_query` already verified -- see that method's
        docstring for why the binding, not this nonce, is the real
        authorization boundary.

        shadow-dual-active T5 (row-bound, CARRIED from T4's review: "until
        this task's row-bound lookup lands, shadow's... Telegram approval
        buttons resolve through the paper queue"): resolves `review_id` to
        its OWN account via `ReviewQueue.locate` FIRST, then acts through
        THAT account's own bundle -- never `self.holder.get()`'s bare
        `.queue`/`.strategies` (the paper-only legacy alias), regardless of
        which account this Telegram chat is currently on. `outcome_line` is
        prefixed `[Paper]`/`[Shadow]` for the ROW's account -- the account
        concerned, which may differ from `self._telegram_account()`. So is
        the `toast`: with one paired chat serving both accounts, a bare
        "Approved" pop-up on a tap made from the other account's view says
        nothing about WHICH account just moved. `_toast` caps it at
        Telegram's own `answerCallbackQuery` ceiling, which is far shorter
        than a message's -- an over-long toast is dropped entirely."""
        c = self.holder.get()
        located = c.queue.locate(review_id)
        if located is None:
            # No row, so no account to name -- the toast stays bare.
            return "That review no longer exists.", "Not found", False
        account = located[0]
        bundle = c.accounts[account] if account != DEFAULT_ACCOUNT else c
        queue = bundle.queue
        # `events._prefix` is the ONE place the bracket/capitalization shape
        # of an account label is derived, here as everywhere else -- an
        # unknown account degrades to no prefix rather than inventing one.
        prefix = _prefix(account)

        def _toast(text: str) -> str:
            return (prefix + text)[:TELEGRAM_TOAST_LIMIT]

        try:
            row = queue.get(review_id)
        except ReviewError:
            return "That review no longer exists.", _toast("Not found"), False

        stored_nonce = (row["approval_token_hash"] or "")[:16]
        if row["status"] != "pending" or not hmac.compare_digest(stored_nonce, nonce):
            message = "That item is no longer pending, or this button has expired."
            return message, _toast("Already resolved"), False

        row_source = row["source"]

        def _echo(summary: str) -> None:
            if row_source == "chat":
                # source="telegram": this resolution originated from a
                # button tap in THIS chat, which already gets its own
                # immediate outcome message right after `_resolve_review_
                # callback` returns (see `_handle_callback_query`'s
                # `send_message(chat_id, ...)` call). `note_resolution`'s
                # mirror hook defaults to source="web" (the web reviews
                # flow); passing "telegram" here tells `_mirror_to_telegram`
                # not to push a second, redundant copy of the same outcome
                # back into this chat -- the web conversation still gets the
                # receipt row either way, since that append happens before
                # the mirror decision.
                #
                # Row-bound: the receipt goes into the ROW's OWN account's
                # ChatService (`account`), not `self.chat_service` (the
                # CURRENT telegram account's) -- a shadow row resolved while
                # this chat happens to be on paper must echo into shadow's
                # own conversation, not paper's.
                service = self._chat_services.get(account)
                if service is not None:
                    # shadow-dual-active T6: best-effort, same reasoning as
                    # web/routes/reviews.py's `_echo_resolution` -- rebuilding
                    # a stale/absent session needs an LLM configured, and the
                    # approval/reject this echoes has already succeeded by
                    # the time this runs. A raised LLMConfigError here must
                    # not skip `_handle_callback_query`'s own
                    # `answer_callback_query` call below (a stuck loading
                    # spinner on a tap that actually worked).
                    try:
                        service.note_resolution(
                            f"You resolved #{review_id}. Result: {summary}",
                            source="telegram")
                    except Exception as exc:  # noqa: BLE001 — see comment above
                        print(f"[telegram] chat echo failed: {self.api._scrub(str(exc))}",
                             file=sys.stderr)

        if action == "reject":
            try:
                queue.reject(review_id, note="rejected via Telegram")
            except ReviewError as exc:
                return f"{prefix}Not processed: {exc}", _toast("Not processed"), True
            _echo("rejected (rejected via Telegram)")
            return f"{prefix}❌ Rejected #{review_id}.", _toast("Rejected"), True

        # action == "approve"
        try:
            result = queue.approve(review_id)
        except RevisionValidationError as exc:
            # M3: `apply_shadow_edit_factory` raises this exact exception
            # too (same rollback-to-pending contract) -- a shadow_edit row
            # failing this must say "Ledger change", not "Revision".
            noun = "Ledger change" if row["kind"] == "shadow_edit" else "Revision"
            _echo(f"{noun.lower()} left pending: re-validation failed ({exc})")
            # `acted=True` here already makes `_handle_callback_query` strip
            # this message's buttons -- but they can't just be left off
            # silently: a re-validation failure burns `consume_token`'s
            # single-use nonce (see that function's own docstring) on the
            # way to raising this, so the row is back to "pending" with no
            # way to re-arm a fresh pair of buttons on THIS message. Saying
            # so explicitly, rather than just "left pending", is the honest
            # version -- a bare "left pending" with no live buttons and no
            # explanation reads as a stuck bot, not a stuck review.
            message = (f"{prefix}⚠️ {noun} #{review_id} failed re-validation "
                      f"({exc}) and stayed pending — reopen from the app.")
            return message, _toast("Left pending: re-validation failed"), True
        except ReviewError as exc:
            return f"{prefix}Not processed: {exc}", _toast("Not processed"), True
        except ExecutionError as exc:
            _echo(f"execution failed: {exc}")
            message = f"{prefix}⚠️ Review #{review_id} claimed, but execution failed: {exc}"
            return message, _toast("Execution failed"), True

        if row["kind"] == "strategy_revision":
            message = (f"Revision applied to {row['strategy_id']}."
                      + bundle.strategies.rearm_warning(row['strategy_id']))
            _echo(f"revision applied to {row['strategy_id']}")
            return f"{prefix}✅ Approved #{review_id} — {message}", _toast("Approved"), True

        if row["kind"] == "shadow_edit":
            # shadow-dual-active T6: mirrors the strategy_revision branch
            # above -- approve() returns None here too, just the ledger
            # write the applier already made.
            _echo(f"{row['action']} applied")
            message = f"{prefix}✅ Approved #{review_id} — {row['action']} applied to the shadow ledger."
            return message, _toast("Approved"), True

        if not result.submitted:
            reasons = "; ".join(result.decision.reasons)
            _echo(f"blocked by the risk gate ({reasons})")
            return (f"{prefix}⚠️ #{review_id} rejected by the risk gate: {reasons}",
                    _toast("Blocked by risk gate"), True)
        if account == "shadow":
            # C3: approving a shadow order writes a ledger row -- nothing was
            # routed to a brokerage (broker/shadow.py has none behind it), so
            # the tap has left the user with an order they still have to
            # place by hand. Both the chat message and the receipt `_echo`
            # feeds back to the agent have to say that, not "submitted".
            summary = ("order recorded in your shadow ledger — place it in "
                       "your brokerage now")
            _echo(summary)
            message = (f"{prefix}✅ Approved #{review_id} — order recorded in "
                       f"your shadow ledger. Place it in your brokerage now.")
            return message, _toast("Recorded"), True
        _echo("order submitted")
        return (f"{prefix}✅ Approved #{review_id} — order submitted.",
                _toast("Approved"), True)

    def _handle_chat_text(self, chat_id: str, text: str,
                          images: list[ImageAttachment] | None = None) -> None:
        # Captured once, up front: this chat's account decides BOTH which
        # ChatService the text is routed to (`self.chat_service`, a
        # property reading the same thing) AND which prefix the reply
        # carries -- reading it twice (once here, once implicitly inside
        # the property) could theoretically disagree if `/account` raced
        # this turn, which prefixing off this one captured value rules out.
        account = self._telegram_account()
        stop_typing = threading.Event()

        def _keep_typing() -> None:
            # Telegram's typing indicator expires a few seconds after each
            # `sendChatAction` call; a single call at the start of a long
            # agent turn (tool calls, LLM round-trips, reflection) reads
            # "typing..." for a moment and then silently goes stale for the
            # rest of the turn (Finding 6). Re-sending on a short interval
            # keeps it alive until `chat_service.send` returns. Runs on its
            # own thread so it never blocks the turn itself; `send_typing`
            # is already best-effort/never-raises (see `TelegramAPI`'s own
            # docstring), so nothing here needs its own try/except.
            while not stop_typing.wait(_TYPING_RESEND_SECONDS):
                self.api.send_typing(chat_id)

        self.api.send_typing(chat_id)
        ticker = threading.Thread(target=_keep_typing, daemon=True,
                                  name="telegram-typing")
        ticker.start()
        # The captured `account`'s own service, not the `self.chat_service`
        # property (which would re-read `_telegram_account()` fresh) -- see
        # the comment above `account = self._telegram_account()`.
        service = self._chat_services.get(account, self._chat_services.get(DEFAULT_ACCOUNT))
        try:
            # `images` is passed only when there ARE images: `chat_service`
            # is duck-typed here (see the class docstring), and a text-only
            # turn keeping the exact pre-T7 call shape means an
            # implementation without the keyword stays valid.
            reply = (service.send(text, source="telegram", images=images) if images
                     else service.send(text, source="telegram"))
        finally:
            stop_typing.set()
            ticker.join(timeout=1)

        # shadow-dual-active T5: every bot message concerning one account
        # carries its `[Paper]`/`[Shadow]` prefix -- the account concerned is
        # whichever this turn was routed to. I2: `prefixed_chunks` budgets
        # for that prefix inside the split rather than gluing it on after,
        # so a reply packed to Telegram's ceiling can't be pushed over it.
        chunks = prefixed_chunks(account, to_telegram_html(reply))
        if not chunks:
            # `split_for_telegram` returns `[]` for empty input -- an empty
            # or whitespace-only agent reply must still tell the user
            # *something* happened, not leave them staring at a typing
            # indicator that silently vanishes.
            chunks = prefixed_chunks(account, "(empty reply)")
        if len(chunks) > MAX_TELEGRAM_REPLY_CHUNKS:
            # Defensive belt (Finding 1): `split_for_telegram` is now proven
            # correct for the corpus this codebase's tests exercise, but if
            # some future input shape still produces a pathological
            # explosion, truncate and log rather than firing hundreds of
            # `sendMessage` calls for one reply.
            self._log_error(
                f"reply split into {len(chunks)} chunks (cap "
                f"{MAX_TELEGRAM_REPLY_CHUNKS}); truncating")
            chunks = [*chunks[:MAX_TELEGRAM_REPLY_CHUNKS], "(reply truncated: too long for Telegram)"]
        for chunk in chunks:
            self.api.send_message(chat_id, chunk)

    # -- daemon loop -------------------------------------------------------

    def run_forever(self) -> None:
        """Runs `poll_once` in a loop until `stop` is set. Any exception
        `poll_once` itself lets through (it shouldn't, normally -- see its
        own per-update try/except -- but a scripted-failing transport or a
        storage hiccup on `_advance_offset`/`_handle_pairing` still can)
        is caught here at the top level, because nothing else watches this
        daemon thread for an uncaught exception; letting one through would
        silently kill Telegram delivery for the rest of the process's life,
        and counts as a failure for backoff purposes same as `poll_once`
        returning `"failed"` explicitly.

        Backs off 5s -> doubling -> capped at 60s on consecutive failure,
        reset to 5s the moment a poll succeeds again. The backoff sleep is
        `self.stop.wait(delay)`, not `time.sleep(delay)` -- so a shutdown
        requested mid-backoff (up to 60s) wakes the loop immediately instead
        of making it wait out the rest of the delay first."""
        delay = _BACKOFF_BASE_SECONDS
        while not self.stop.is_set():
            try:
                status = self.poll_once()
            except Exception as exc:  # noqa: BLE001 — must never kill this thread
                self._log_error(f"poll_once failed: {exc}")
                status = "failed"
            if status == "failed":
                self.stop.wait(delay)
                delay = min(delay * 2, _BACKOFF_CAP_SECONDS)
            else:
                delay = _BACKOFF_BASE_SECONDS
