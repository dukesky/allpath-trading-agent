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

Token scrubbing: every Telegram API URL is shaped `.../bot<token>/<method>`,
so the token is embedded in the URL string that shows up inside
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
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, TELEGRAM_OFFSET_KEY, AppState
from allpath_trade.web.markdown import split_for_telegram, to_telegram_html

_API_URL = "https://api.telegram.org/bot{token}/{method}"
_TOKEN_MASK = "***"

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

    def get_updates(self, offset: int, timeout_s: int = 50) -> list[dict[str, Any]]:
        """Long-poll `getUpdates`. Returns the `result` array on success, or
        `[]` -- after exactly one scrubbed stderr line -- for ANY failure:
        a network/HTTP error, a response body that isn't valid JSON, a
        well-formed-but-`ok:false` body, or a body whose `result` isn't a
        list. The poller (Task 3) treats `[]` as "nothing to do this pass",
        never as a reason to stop polling."""
        req = self._request("getUpdates", {"offset": offset, "timeout": timeout_s})
        timeout = timeout_s + _GETUPDATES_TIMEOUT_MARGIN_SECONDS
        try:
            with self._urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            data = json.loads(body)
        except Exception as exc:  # noqa: BLE001 — transport must never raise
            print(f"[telegram] getUpdates failed: {self._scrub(str(exc))}", file=sys.stderr)
            return []
        if not isinstance(data, dict) or not data.get("ok"):
            print(f"[telegram] getUpdates failed: {self._scrub(str(data))}", file=sys.stderr)
            return []
        result = data.get("result")
        if not isinstance(result, list):
            print("[telegram] getUpdates failed: unexpected response shape", file=sys.stderr)
            return []
        return result

    def _send_once(self, chat_id: str, text: str, parse_mode: str | None) -> str:
        """One sendMessage attempt. Returns "ok", "http400" (so the caller
        can retry as plain text -- a 400 from Telegram's sendMessage is how
        an entity-parse failure surfaces), or "fail" for anything else.
        Always prints at most one scrubbed stderr line on failure."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
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

    def send_message(self, chat_id: str, html: str) -> bool:
        """Send `html` with `parse_mode=HTML`. On a Telegram 400 (entity
        parse failure -- e.g. an unbalanced tag slipped past
        `to_telegram_html`), retries EXACTLY ONCE as plain text via
        `strip_telegram_html_to_plain`. Never raises; returns whether the
        message was ultimately delivered."""
        status = self._send_once(chat_id, html, "HTML")
        if status == "ok":
            return True
        if status == "http400":
            plain = strip_telegram_html_to_plain(html)
            return self._send_once(chat_id, plain, None) == "ok"
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


# Backoff for `TelegramPoller.run_forever`'s retry loop: 5s to start, doubling
# on each consecutive failure, capped at 60s -- reset to the base the moment a
# poll succeeds again. Values match the design spec verbatim.
_BACKOFF_BASE_SECONDS = 5
_BACKOFF_CAP_SECONDS = 60


class TelegramPoller:
    """Long-polls Telegram's `getUpdates`, pairs a single chat via `/start
    <web_token>`, and forwards paired-chat text into `chat_service` --
    replying with the agent's turn.

    `poll_once` is the unit tests drive directly: one `getUpdates` call, one
    pass over whatever updates came back. `run_forever` is the thin loop
    `serve` runs on a daemon thread -- call `poll_once` forever, backing off
    on repeated failure, until `stop` is set.

    `chat_service` is duck-typed as `send(text: str, source: str = "web") ->
    str` -- that `source` keyword is `ChatService`'s (Task 4 in the design
    plan); this class only ever needs the interface, not the concrete type,
    so it has no import-time dependency on `chat_service.py`.
    """

    def __init__(self, api: TelegramAPI, chat_service: Any, app_state: AppState,
                 web_token: str, stop: threading.Event) -> None:
        self.api = api
        self.chat_service = chat_service
        self.app_state = app_state
        self.web_token = web_token
        self.stop = stop

    # -- single poll step -----------------------------------------------

    def poll_once(self) -> None:
        """One `getUpdates` call plus one pass over its results. Every
        per-update failure (a malformed update, an exception raised while
        handling it) is caught right there so it can never take the rest of
        the batch down with it -- see `_handle_update`'s per-update
        try/except below."""
        offset = self._current_offset()
        updates = self.api.get_updates(offset=offset, timeout_s=50)
        dropped = 0
        for update in updates:
            try:
                # Offset is persisted the instant an update is received --
                # BEFORE any processing of it. This is deliberate at-most-once
                # semantics (verbatim from the design spec): a mid-turn crash
                # must drop the message (visible: the user just resends it)
                # rather than replay it on restart (invisible: a duplicate
                # order proposal re-enters the pending queue). This tradeoff
                # is fixed, not configurable.
                self._advance_offset(update)
                if self._handle_update(update) == "dropped":
                    dropped += 1
            except Exception as exc:  # noqa: BLE001 — one bad update must not kill the batch
                print(f"[telegram] failed to process update: {exc}", file=sys.stderr)
        if dropped:
            # At most one stderr line per batch, however many stranger
            # messages showed up in it -- a burst from an unpaired chat must
            # not spam the log one line per message.
            print(f"[telegram] dropped {dropped} message(s) from unpaired chat(s)",
                  file=sys.stderr)

    def _current_offset(self) -> int:
        raw = self.app_state.get(TELEGRAM_OFFSET_KEY)
        try:
            return int(raw) if raw is not None else 0
        except ValueError:
            return 0

    def _advance_offset(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            self.app_state.set(TELEGRAM_OFFSET_KEY, str(update_id + 1))

    def _handle_update(self, update: dict[str, Any]) -> str | None:
        """Returns `"dropped"` when the update was a text message from an
        unpaired chat (so `poll_once` can count it for its one stderr line),
        `None` otherwise -- including every non-text update shape (photos,
        stickers, `edited_message`, `callback_query`, ...), which are simply
        ignored: this bot only ever understands plain text."""
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        text = message.get("text")
        if not isinstance(text, str):
            return None
        chat = message.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        if chat_id is None:
            return None
        chat_id = str(chat_id)

        if text.startswith("/start"):
            self._handle_pairing(chat_id, text)
            return None

        paired_chat_id = self.app_state.get(TELEGRAM_CHAT_ID_KEY)
        if paired_chat_id is None or chat_id != paired_chat_id:
            # A stranger: no reply of any kind, not even to confirm the bot
            # is alive. Counted by the caller, not logged per-message.
            return "dropped"

        self._handle_chat_text(chat_id, text)
        return None

    def _handle_pairing(self, chat_id: str, text: str) -> None:
        """`/start <token>` pairing. Correct token (constant-time compare)
        -> store the chat id, reply once. Wrong or missing token -> NO
        reply of any kind -- confirming the bot is alive to a guesser is
        itself information leakage. Re-pairing (a second correct `/start`
        from a different chat) simply overwrites the stored chat id; this
        is single-chat-only by design."""
        parts = text.split(maxsplit=1)
        token = parts[1].strip() if len(parts) > 1 else ""
        if not token or not self.web_token or not hmac.compare_digest(token, self.web_token):
            return
        self.app_state.set(TELEGRAM_CHAT_ID_KEY, chat_id)
        self.api.send_message(chat_id, "Paired. This chat now talks to your AllPath agent.")

    def _handle_chat_text(self, chat_id: str, text: str) -> None:
        self.api.send_typing(chat_id)
        reply = self.chat_service.send(text, source="telegram")
        html = to_telegram_html(reply)
        for chunk in split_for_telegram(html):
            self.api.send_message(chat_id, chunk)

    # -- daemon loop -------------------------------------------------------

    def run_forever(self) -> None:
        """Runs `poll_once` in a loop until `stop` is set. Any exception
        `poll_once` itself lets through (it shouldn't, normally -- see its
        own per-update try/except -- but a scripted-failing transport or a
        storage hiccup on `_advance_offset`/`_handle_pairing` still can)
        is caught here at the top level, because nothing else watches this
        daemon thread for an uncaught exception; letting one through would
        silently kill Telegram delivery for the rest of the process's life.
        Backs off 5s -> doubling -> capped at 60s on consecutive failure,
        reset to 5s the moment a poll succeeds again."""
        delay = _BACKOFF_BASE_SECONDS
        while not self.stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 — must never kill this thread
                print(f"[telegram] poll_once failed: {exc}", file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, _BACKOFF_CAP_SECONDS)
                continue
            delay = _BACKOFF_BASE_SECONDS
