"""Tests for Task 4: ChatService source tagging + the mirror hook.

`ChatService.send` gains a `source` parameter that lands on the appended
user-message dict, and `ChatService.set_mirror` registers a hook fired
after a turn completes (send) or an out-of-band note is appended
(note_resolution). Direction policy (push to Telegram only for web-sourced
turns) is Task 5's concern -- these tests only prove the hook mechanics:
what it's called with, when, and that ChatService is immune to it.

Reuses tests/test_web_chat.py's `make_client` (real create_app + a
ScriptedLLM swapped in via monkeypatch) rather than constructing ChatService
directly, since the real object always needs a working `holder` -- see
allpath_trade/web/app.py's ChatService(app.state.holder).
"""

from __future__ import annotations

from allpath_trade.llm.base import LLMResponse
from tests.test_agent_loop import ScriptedLLM
from tests.test_web_chat import make_client


def test_send_tags_the_user_message_with_source_web_by_default(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="hi there")])
    client.app.state.chat_service.send("hello")
    messages = client.app.state.chat_service.messages()
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["source"] == "web"


def test_send_tags_the_user_message_with_the_given_source(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="hi there")])
    client.app.state.chat_service.send("hello from telegram", source="telegram")
    messages = client.app.state.chat_service.messages()
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["source"] == "telegram"


def test_source_key_never_reaches_the_llm(tmp_path, monkeypatch):
    # `source` is a presentation-extra key like note_resolution's `kind`/
    # `display` -- _PROTOCOL_KEYS in agent/loop.py projects it away before
    # any messages list reaches LLMClient.complete.
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="hi there")])
    client.app.state.chat_service.send("hello", source="telegram")
    llm = client.app.state.chat_service.session().llm
    assert isinstance(llm, ScriptedLLM)
    sent = llm.seen[-1]
    user_msgs = [m for m in sent if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert "source" not in user_msgs[0]


def test_web_route_still_defaults_to_source_web(tmp_path, monkeypatch):
    # Constraint: the web route keeps calling send(text) with no source arg.
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="hi there")])
    client.post("/chat/send", data={"message": "hi"})
    messages = client.app.state.chat_service.messages()
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert user_msgs[0]["source"] == "web"


def test_no_mirror_registered_is_zero_behavior_change(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="hi there")])
    reply = client.app.state.chat_service.send("hello")
    assert reply == "hi there"


def test_mirror_is_called_after_the_turn_completes(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="agent reply")])
    calls = []
    client.app.state.chat_service.set_mirror(
        lambda source, text, reply: calls.append((source, text, reply)))

    reply = client.app.state.chat_service.send("hello", source="telegram")

    assert reply == "agent reply"
    assert calls == [("telegram", "hello", "agent reply")]


def test_mirror_default_source_is_web(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="agent reply")])
    calls = []
    client.app.state.chat_service.set_mirror(
        lambda source, text, reply: calls.append((source, text, reply)))

    client.app.state.chat_service.send("hello")

    assert calls == [("web", "hello", "agent reply")]


def test_mirror_is_called_outside_the_turn_lock(tmp_path, monkeypatch):
    # If the mirror ran while `_turn_lock` was still held, a mirror fn that
    # (like Task 5's real one, indirectly) touches the chat service again
    # would deadlock -- threading.Lock is not reentrant. Registering a
    # mirror that calls note_resolution (which also acquires `_turn_lock`)
    # is a direct proof the lock was already released by the time the
    # mirror fires.
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="agent reply")])
    service = client.app.state.chat_service
    calls = []

    def mirror(source, text, reply):
        calls.append((source, text, reply))
        # Would hang forever if _turn_lock were still held here.
        service._turn_lock.acquire(timeout=2)
        service._turn_lock.release()

    service.set_mirror(mirror)
    reply = service.send("hello")

    assert reply == "agent reply"
    assert calls == [("web", "hello", "agent reply")]


def test_mirror_exception_does_not_break_the_turn(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="agent reply")])
    service = client.app.state.chat_service

    def boom(source, text, reply):
        raise RuntimeError("mirror blew up")

    service.set_mirror(boom)
    reply = service.send("hello")

    assert reply == "agent reply"


def test_note_resolution_triggers_the_mirror_with_source_web(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [])
    service = client.app.state.chat_service
    calls = []
    service.set_mirror(lambda source, text, reply: calls.append((source, text, reply)))

    service.note_resolution("You resolved #1. Result: order submitted")

    assert calls == [("web", "You resolved #1. Result: order submitted", "")]


def test_note_resolution_without_a_mirror_is_zero_behavior_change(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [])
    service = client.app.state.chat_service
    service.note_resolution("You resolved #1. Result: order submitted")
    messages = service.messages()
    assert any(m.get("kind") == "system_note" for m in messages)


# ---------------------------------------------------------------------------
# I3: the web->Telegram mirror must convert Markdown to HTML FIRST and only
# then prefix.
#
# Prefixing the raw Markdown moved every block construct off the start of its
# line, so `to_telegram_html` stopped recognizing it: a fenced code block
# mirrored as literal backticks followed by an empty `<pre></pre>`, a heading
# lost its `<b>`, and a table was mangled into a column of its own prefix.
# ---------------------------------------------------------------------------

from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, AppState
from allpath_trade.store.db import connect
from allpath_trade.web.app import _mirror_to_telegram


class _ImmediateQueue:
    def submit(self, fn, *args):
        fn(*args)


class _FakeMirrorAPI:
    token = "t"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_message(self, chat_id: str, html: str, reply_markup=None) -> bool:
        self.sent.append((chat_id, html))
        return True

    def _scrub(self, text: str) -> str:
        return text


def _paired(tmp_path):
    app_state = AppState(connect(tmp_path / "mirror.db"))
    app_state.set(TELEGRAM_CHAT_ID_KEY, "555")
    return app_state


def _mirror_reply(tmp_path, reply, account="shadow"):
    api = _FakeMirrorAPI()
    _mirror_to_telegram("web", "ask", reply, api=api, app_state=_paired(tmp_path),
                        mirror_queue=_ImmediateQueue(), account=account)
    # [0] is the "You (web): ..." echo; [1] is the reply under test.
    return [html for _cid, html in api.sent]


def test_mirrored_fenced_code_block_keeps_its_pre_tag_behind_the_prefix(tmp_path):
    sent = _mirror_reply(tmp_path, "```python\nx = 1\n```")
    assert sent[-1] == "[Shadow] <pre>x = 1</pre>"


def test_mirrored_heading_is_still_converted(tmp_path):
    sent = _mirror_reply(tmp_path, "## Heading\n\ntext")
    assert sent[-1] == "[Shadow] <b>Heading</b>\n\ntext"


def test_mirrored_table_is_not_mangled_by_the_prefix(tmp_path):
    sent = _mirror_reply(tmp_path, "| a | b |\n| --- | --- |\n| 1 | 2 |")
    assert sent[-1] == "[Shadow] <pre>a | b\n--+--\n1 | 2</pre>"


def test_mirrored_plain_reply_wording_is_unchanged(tmp_path):
    # Regression guard for the existing shape: a prefix, then the text.
    sent = _mirror_reply(tmp_path, "noted", account="paper")
    assert sent == ["[Paper] You (web): ask", "[Paper] noted"]


def test_mirrored_reply_at_the_telegram_limit_still_fits_after_prefixing(tmp_path):
    # I2's fix is shared with the poller: the prefix is budgeted for inside
    # the split, so no chunk can exceed Telegram's ceiling.
    sent = _mirror_reply(tmp_path, "x" * 4096)
    reply_chunks = sent[1:]
    assert all(len(html) <= 4096 for html in sent)
    assert reply_chunks[0].startswith("[Shadow] ")
    assert sum(html.count("x") for html in reply_chunks) == 4096


# ---------------------------------------------------------------------------
# setup-wizard T6: an image upload mirrors the placeholder, never the bytes.
# ---------------------------------------------------------------------------

def test_a_web_upload_mirrors_the_placeholder_text_and_never_bytes(
        tmp_path, monkeypatch):
    from tests.test_web_chat import PNG_BYTES

    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="two positions")])
    seen = []
    client.app.state.chat_service.set_mirror(
        lambda source, text, reply: seen.append((source, text, reply)))

    client.post("/chat/send", data={"message": "what is this"},
                files=[("images", ("positions.png", PNG_BYTES, "image/png"))])

    [(source, text, reply)] = seen
    assert source == "web"
    assert isinstance(text, str)
    assert text == "[image: positions.png, 2 KB] what is this"
    assert reply == "two positions"
    # Nothing anywhere in the mirrored tuple is bytes -- the mirror hook is
    # a Telegram sendMessage, and a stray `bytes` here would either crash it
    # or push the raw screenshot into the paired chat.
    assert not any(isinstance(v, bytes) for v in (source, text, reply))


# ---------------------------------------------------------------------------
# setup-wizard T7: the Telegram-sourced image turn.
# ---------------------------------------------------------------------------

def test_a_telegram_image_turn_mirrors_placeholders_only_and_is_not_pushed_back(
        tmp_path, monkeypatch):
    # The poller calls `send(..., source="telegram", images=[...])` and
    # replies in-channel itself; `_mirror_to_telegram`'s direction policy
    # (tests/test_web_app_telegram.py) then no-ops on that source. What this
    # proves is what the hook is HANDED for such a turn: the same
    # placeholder text the transcript shows, never the bytes.
    from allpath_trade.agent.attachments import validate_images
    from tests.test_web_chat import PNG_BYTES

    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="two positions")])
    seen = []
    client.app.state.chat_service.set_mirror(
        lambda source, text, reply: seen.append((source, text, reply)))

    images = validate_images([(PNG_BYTES, "screenshot.png")])
    client.app.state.chat_service.send("what is this", source="telegram", images=images)

    [(source, text, reply)] = seen
    assert source == "telegram"
    assert text == "[image: screenshot.png, 2 KB] what is this"
    assert reply == "two positions"
    assert not any(isinstance(v, bytes) for v in (source, text, reply))
    # And nothing durable holds the bytes either.
    history = client.app.state.chat_service.messages()
    assert not any(isinstance(v, bytes) for m in history for v in m.values())
