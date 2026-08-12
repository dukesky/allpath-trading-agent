"""Telegram Bot API transport (`TelegramAPI`) -- stdlib `urllib.request`
only, no new dependency, mirroring the precedent `notify/ntfy.py` already
set for this codebase's "one HTTP call, no SDK" shape.

This module owns the HTTP boundary only. `get_updates`/`send_message`/
`send_typing` never raise -- every failure mode (network error, HTTP error,
malformed JSON, a well-formed `{"ok": false, ...}` body) is caught here and
turned into a return value plus at most one `stderr` line, because the
caller (Task 3's `TelegramPoller`, running on a daemon thread with nothing
watching for an uncaught exception) must never die from a flaky Telegram API
or a slow network. The poller class living alongside this transport lands in
Task 3.

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

import html as _html
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

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
