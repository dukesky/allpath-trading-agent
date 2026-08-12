"""Tests for allpath_trade/telegram.py's transport half (`TelegramAPI`).
Never touches api.telegram.org -- `urlopen` is injected as a fake, mirroring
tests/test_notify_ntfy.py's pattern for the same "stdlib urllib, one HTTP
call" shape (see telegram.py's own module docstring)."""

from __future__ import annotations

import json
import urllib.error

import pytest

from allpath_trade.telegram import _HTML_TAG_RE, TelegramAPI, strip_telegram_html_to_plain
from allpath_trade.web.markdown import TELEGRAM_ALLOWED_TAGS

TOKEN = "123456:AAEsecret-bot-token-value"


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def _ok_body(result) -> bytes:
    return json.dumps({"ok": True, "result": result}).encode("utf-8")


def _fail_body(description: str = "Bad Request") -> bytes:
    return json.dumps({"ok": False, "description": description}).encode("utf-8")


# ---------------------------------------------------------------------------
# get_updates
# ---------------------------------------------------------------------------

def test_get_updates_parses_result_and_hits_correct_url_and_timeout():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["content_type"] = req.get_header("Content-type")
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        return _FakeResponse(_ok_body([{"update_id": 1}, {"update_id": 2}]))

    api = TelegramAPI(TOKEN, urlopen=fake_urlopen)
    result = api.get_updates(offset=5, timeout_s=50)

    assert result == [{"update_id": 1}, {"update_id": 2}]
    assert captured["url"] == f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    assert captured["method"] == "POST"
    assert captured["content_type"] == "application/json"
    assert captured["body"] == {"offset": 5, "timeout": 50}
    # Socket timeout is strictly longer than the long-poll timeout Telegram
    # was asked to hold the connection for -- 55 for the documented default.
    assert captured["timeout"] == 55


def test_get_updates_default_timeout_s_is_50():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        return _FakeResponse(_ok_body([]))

    api = TelegramAPI(TOKEN, urlopen=fake_urlopen)
    api.get_updates(offset=0)
    assert captured["body"]["timeout"] == 50
    assert captured["timeout"] == 55


def test_get_updates_http_error_returns_empty_list_and_one_stderr_line(capsys):
    def raise_http_error(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, None)

    api = TelegramAPI(TOKEN, urlopen=raise_http_error)
    assert api.get_updates(offset=0) == []
    err_lines = capsys.readouterr().err.strip().splitlines()
    assert len(err_lines) == 1
    assert "telegram" in err_lines[0]


def test_get_updates_timeout_returns_empty_list_and_never_raises(capsys):
    def raise_timeout(req, timeout=None):
        raise TimeoutError("timed out")  # what a real socket timeout raises

    api = TelegramAPI(TOKEN, urlopen=raise_timeout)
    assert api.get_updates(offset=0) == []
    err_lines = capsys.readouterr().err.strip().splitlines()
    assert len(err_lines) == 1


def test_get_updates_garbage_json_returns_empty_list(capsys):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(b"not json at all {{{")

    api = TelegramAPI(TOKEN, urlopen=fake_urlopen)
    assert api.get_updates(offset=0) == []
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_get_updates_ok_false_returns_empty_list(capsys):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(_fail_body("Unauthorized"))

    api = TelegramAPI(TOKEN, urlopen=fake_urlopen)
    assert api.get_updates(offset=0) == []
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_get_updates_non_list_result_returns_empty_list(capsys):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(json.dumps({"ok": True, "result": "not-a-list"}).encode())

    api = TelegramAPI(TOKEN, urlopen=fake_urlopen)
    assert api.get_updates(offset=0) == []
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_get_updates_never_raises_on_any_exception_type():
    def raise_value_error(req, timeout=None):
        raise ValueError("boom")

    api = TelegramAPI(TOKEN, urlopen=raise_value_error)
    assert api.get_updates(offset=0) == []


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------

def test_send_message_html_success_returns_true_single_call():
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(json.loads(req.data))
        return _FakeResponse(_ok_body({"message_id": 1}))

    api = TelegramAPI(TOKEN, urlopen=fake_urlopen)
    assert api.send_message("42", "<b>hi</b>") is True
    assert len(calls) == 1
    assert calls[0]["parse_mode"] == "HTML"
    assert calls[0]["chat_id"] == "42"
    assert calls[0]["text"] == "<b>hi</b>"


def test_send_message_400_retries_once_as_plain_text_and_succeeds():
    calls = []

    def fake_urlopen(req, timeout=None):
        payload = json.loads(req.data)
        calls.append(payload)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)
        return _FakeResponse(_ok_body({"message_id": 2}))

    api = TelegramAPI(TOKEN, urlopen=fake_urlopen)
    ok = api.send_message("42", "<b>bold</b> &amp; <code>x</code>")

    assert ok is True
    assert len(calls) == 2
    assert calls[0]["parse_mode"] == "HTML"
    assert "parse_mode" not in calls[1]
    # Tags stripped, entities unescaped for the plain-text fallback.
    assert calls[1]["text"] == "bold & x"


def test_send_message_400_then_plain_retry_also_fails_returns_false_exactly_two_calls():
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)

    api = TelegramAPI(TOKEN, urlopen=fake_urlopen)
    assert api.send_message("42", "<b>hi</b>") is False
    assert len(calls) == 2


def test_send_message_non_400_failure_does_not_retry():
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, None)

    api = TelegramAPI(TOKEN, urlopen=fake_urlopen)
    assert api.send_message("42", "<b>hi</b>") is False
    assert len(calls) == 1


def test_send_message_network_error_returns_false_never_raises():
    def raise_error(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    api = TelegramAPI(TOKEN, urlopen=raise_error)
    assert api.send_message("42", "<b>hi</b>") is False


def test_send_message_ok_false_body_returns_false():
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(_fail_body())

    api = TelegramAPI(TOKEN, urlopen=fake_urlopen)
    assert api.send_message("42", "<b>hi</b>") is False


# ---------------------------------------------------------------------------
# send_typing
# ---------------------------------------------------------------------------

def test_send_typing_success_hits_sendchataction():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return _FakeResponse(_ok_body(True))

    api = TelegramAPI(TOKEN, urlopen=fake_urlopen)
    assert api.send_typing("42") is None
    assert captured["url"] == f"https://api.telegram.org/bot{TOKEN}/sendChatAction"
    assert captured["body"] == {"chat_id": "42", "action": "typing"}


def test_send_typing_swallows_every_failure_mode(capsys):
    def raise_error(req, timeout=None):
        raise urllib.error.URLError("nope")

    api = TelegramAPI(TOKEN, urlopen=raise_error)
    assert api.send_typing("42") is None
    # Best-effort: no stderr noise expected either.
    assert capsys.readouterr().err == ""


def test_send_typing_never_raises_on_garbage_response():
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(b"garbage")

    api = TelegramAPI(TOKEN, urlopen=fake_urlopen)
    assert api.send_typing("42") is None


# ---------------------------------------------------------------------------
# Token scrub -- the token must never appear in stderr, no matter which
# method fails or what the underlying exception's message happens to embed.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method_call", ["get_updates", "send_message"])
def test_token_never_appears_in_stderr_even_when_exception_message_embeds_the_url(
        capsys, method_call):
    url = f"https://api.telegram.org/bot{TOKEN}/whatever"

    def raise_error_with_url_in_message(req, timeout=None):
        raise urllib.error.URLError(f"Failed to reach {url}: [Errno -2] Name or service not known")

    api = TelegramAPI(TOKEN, urlopen=raise_error_with_url_in_message)
    if method_call == "get_updates":
        api.get_updates(offset=0)
    else:
        api.send_message("42", "<b>hi</b>")

    err = capsys.readouterr().err
    assert TOKEN not in err
    assert "***" in err


def test_scrub_is_a_noop_when_token_is_empty():
    api = TelegramAPI("", urlopen=lambda req, timeout=None: _FakeResponse(_ok_body([])))
    assert api._scrub("hello world") == "hello world"


# ---------------------------------------------------------------------------
# strip_telegram_html_to_plain -- the module helper send_message's fallback
# uses, tested directly too since it's part of the public transport module.
# ---------------------------------------------------------------------------

def test_strip_telegram_html_to_plain_strips_tags_and_unescapes_entities():
    html = "<b>Bold</b> and <code>&lt;tag&gt;</code> inside <pre>a &amp; b</pre>"
    assert strip_telegram_html_to_plain(html) == "Bold and <tag> inside a & b"


def test_strip_telegram_html_to_plain_leaves_plain_text_untouched():
    assert strip_telegram_html_to_plain("just plain text") == "just plain text"


# ---------------------------------------------------------------------------
# Cross-file drift test: _HTML_TAG_RE must match exactly TELEGRAM_ALLOWED_TAGS
# (Finding 2: ensure the tag set in this file stays in sync with markdown.py's
# TELEGRAM_ALLOWED_TAGS without requiring manual test maintenance).
# ---------------------------------------------------------------------------

def test_html_tag_re_matches_exactly_telegram_allowed_tags():
    """The _HTML_TAG_RE regex must strip exactly the tags in TELEGRAM_ALLOWED_TAGS.
    This test fails if either set changes without the other being updated."""
    # Extract tag names from the regex pattern.
    # The pattern is r"</?(?:b|code|pre)>" -- extract the tag names.
    pattern_str = _HTML_TAG_RE.pattern
    # Parse the pattern to extract tag names (between | separators inside the group).
    # Pattern: </?(?:TAG1|TAG2|...)>
    import re as _re_module
    match = _re_module.search(r"\(\?:([^)]+)\)", pattern_str)
    assert match, f"Could not parse tag names from _HTML_TAG_RE pattern: {pattern_str}"
    tags_in_regex = set(match.group(1).split("|"))

    # Verify the regex tags exactly match TELEGRAM_ALLOWED_TAGS.
    assert tags_in_regex == TELEGRAM_ALLOWED_TAGS, (
        f"Tag mismatch: _HTML_TAG_RE has {tags_in_regex}, "
        f"but TELEGRAM_ALLOWED_TAGS has {TELEGRAM_ALLOWED_TAGS}. "
        "Update both to stay in sync."
    )
