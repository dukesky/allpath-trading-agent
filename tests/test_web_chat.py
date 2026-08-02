from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.llm.base import LLMClient, LLMResponse
from allpath_trade.web.app import create_app
from tests.test_agent_loop import ScriptedLLM, tool_response
from tests.test_sentinel import FakeBroker


def make_client(tmp_path, monkeypatch, responses):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret",
                        openrouter_api_key="k")
    llm = ScriptedLLM(responses)
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat": llm)
    client = TestClient(create_app(settings, broker=FakeBroker()))
    client.post("/login", data={"token": "secret"})
    return client


def test_message_round_trip(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="hello there")])
    r = client.post("/chat/send", data={"message": "hi"})
    assert "hello there" in r.text


def test_history_survives_a_reload(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [LLMResponse(text="remembered")])
    client.post("/chat/send", data={"message": "hi"})
    assert "remembered" in client.get("/chat").text


def test_no_session_controls_are_offered(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [])
    body = client.get("/chat").text.lower()
    assert "new conversation" not in body
    assert "sessions" not in body


def test_proposed_order_becomes_a_pending_review(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [
        tool_response("propose_order", {"ticker": "AAPL", "side": "buy",
                                        "notional": "500", "reason": "add"}),
        LLMResponse(text="queued it for you"),
    ])
    client.post("/chat/send", data={"message": "buy some apple"})
    rows = client.app.state.holder.get().queue.list("pending")
    assert len(rows) == 1
    assert rows[0]["source"] == "chat"


def test_empty_message_is_ignored(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [])
    r = client.post("/chat/send", data={"message": "   "})
    assert r.status_code == 200


def test_approval_is_echoed_into_the_conversation(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [
        tool_response("propose_order", {"ticker": "AAPL", "side": "buy",
                                        "notional": "500", "reason": "add"}),
        LLMResponse(text="queued"),
    ])
    client.post("/chat/send", data={"message": "buy apple"})
    rid = client.app.state.holder.get().queue.list("pending")[0]["id"]
    client.post(f"/reviews/{rid}/approve")
    history = client.app.state.chat.messages()
    assert any("[system]" in str(m.get("content", "")) for m in history)


def test_assistant_html_is_escaped_not_rendered(tmp_path, monkeypatch):
    # The model's text lands in the page as data, not markup: a prompt
    # injection or an accidental HTML-looking reply must never execute.
    client = make_client(tmp_path, monkeypatch,
                         [LLMResponse(text="<script>alert(1)</script>")])
    r = client.post("/chat/send", data={"message": "hi"})
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


class BlockingLLM(LLMClient):
    """Blocks on its first `complete()` call until released, so a test can
    force two turns to overlap and observe how ChatService orders them."""

    model = "blocking"

    def __init__(self, responses: list[LLMResponse],
                 release: threading.Event, started: threading.Event) -> None:
        self.responses = list(responses)
        self.release = release
        self.started = started
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            self.release.wait(timeout=5)
        return self.responses.pop(0)


def test_a_second_send_waits_for_the_first_turn_to_finish(tmp_path, monkeypatch):
    # ChatService is process-wide state shared by every request. Without
    # serializing turns, two concurrent POST /chat/send calls would both
    # mutate the same AgentSession.history and ChatService.activity list at
    # once -- interleaved user/assistant messages, or a turn's activity
    # trail clobbered by the other request's reset. This proves the second
    # send blocks until the first is completely done, not just started.
    started = threading.Event()
    release = threading.Event()
    llm = BlockingLLM([LLMResponse(text="first reply"),
                       LLMResponse(text="second reply")], release, started)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret",
                        openrouter_api_key="k")
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat": llm)
    client = TestClient(create_app(settings, broker=FakeBroker()))
    client.post("/login", data={"token": "secret"})

    results: dict[str, object] = {}

    def send_first() -> None:
        results["first"] = client.post("/chat/send", data={"message": "first"})

    t1 = threading.Thread(target=send_first)
    t1.start()
    assert started.wait(timeout=5), "first turn never reached the LLM call"

    def send_second() -> None:
        results["second"] = client.post("/chat/send", data={"message": "second"})

    t2 = threading.Thread(target=send_second)
    t2.start()
    # Give the second request every chance to run ahead if sends were not
    # serialized -- it would not block on anything inside BlockingLLM.
    time.sleep(0.3)
    mid_contents = [m.get("content") for m in client.app.state.chat.messages()]
    assert "second" not in mid_contents

    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    contents = [m.get("content") for m in client.app.state.chat.messages()]
    assert contents == ["first", "first reply", "second", "second reply"]
