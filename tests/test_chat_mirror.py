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
