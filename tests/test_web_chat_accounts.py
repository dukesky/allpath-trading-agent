"""Tests for shadow-dual-active Task 5: per-account ChatService wiring.

Two web-side ChatService instances, one per `store.accounts.ACCOUNTS` entry
(`app.state.chat_services`), each reading its own account's queue/
strategies/memory/conversations -- the `/chat` route routes to whichever
one the current `account` cookie selects. The shared LLM-usage/personal-
profile-memory story is covered separately (test_memory_store.py already
proves profile.md is shared across `MemoryStore(account=...)` instances);
this file is about the ChatService/web-routing seam specifically.
"""

from __future__ import annotations

from typing import ClassVar

from allpath_trade.llm.base import LLMResponse
from allpath_trade.web.account_ctx import ACCOUNT_COOKIE
from tests.helpers import CONFIGURED_KEYS
from tests.test_agent_loop import ScriptedLLM
from tests.test_web_chat import make_client


def test_profile_memory_is_shared_across_both_accounts_chat_services(tmp_path, monkeypatch):
    # Spec §③: profile stays shared while strategy/stock/lesson layers split
    # per account -- proven here at the ChatService seam (both accounts'
    # own MemoryStore.apply/read against "profile") rather than only at
    # MemoryStore's own unit-test layer.
    client = make_client(tmp_path, monkeypatch, [])
    comp = client.app.state.holder.get()
    comp.accounts["paper"].memory.apply("profile", None, "add", text="prefers dividends")
    assert "prefers dividends" in comp.accounts["shadow"].memory.read("profile")


def test_paper_and_shadow_have_distinct_chat_service_instances(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, [])
    services = client.app.state.chat_services
    assert set(services) == {"paper", "shadow"}
    assert services["paper"] is not services["shadow"]
    assert services["paper"].account == "paper"
    assert services["shadow"].account == "shadow"
    # The legacy singular alias still points at paper's own instance.
    assert client.app.state.chat_service is services["paper"]
    assert client.app.state.chat is services["paper"]


def test_chat_route_uses_the_current_account_cookies_chat_service(tmp_path, monkeypatch):
    client = make_client(
        tmp_path, monkeypatch,
        [LLMResponse(text="paper reply"), LLMResponse(text="shadow reply")])

    client.cookies.set(ACCOUNT_COOKIE, "paper")
    client.post("/chat/send", data={"message": "hello from paper"})
    client.cookies.set(ACCOUNT_COOKIE, "shadow")
    client.post("/chat/send", data={"message": "hello from shadow"})

    paper_msgs = client.app.state.chat_services["paper"].messages()
    shadow_msgs = client.app.state.chat_services["shadow"].messages()

    paper_texts = [m.get("content") for m in paper_msgs if m.get("role") == "user"]
    shadow_texts = [m.get("content") for m in shadow_msgs if m.get("role") == "user"]

    assert "hello from paper" in paper_texts
    assert "hello from paper" not in shadow_texts
    assert "hello from shadow" in shadow_texts
    assert "hello from shadow" not in paper_texts


def test_each_account_chat_service_has_its_own_conversation_id(tmp_path, monkeypatch):
    client = make_client(
        tmp_path, monkeypatch, [LLMResponse(text="p"), LLMResponse(text="s")])

    client.cookies.set(ACCOUNT_COOKIE, "paper")
    client.post("/chat/send", data={"message": "hi"})
    client.cookies.set(ACCOUNT_COOKIE, "shadow")
    client.post("/chat/send", data={"message": "hi"})

    paper_session = client.app.state.chat_services["paper"].session()
    shadow_session = client.app.state.chat_services["shadow"].session()
    assert paper_session.conversation_id != shadow_session.conversation_id


class SpyLLM(ScriptedLLM):
    """Records every `.complete()` call's messages -- used to inspect the
    system prompt each account's ChatService builds, without needing a
    separate scripted-LLM instance per account (both are monkeypatched onto
    the SAME `build_llm` stand-in in this file's tests, mirroring
    `test_web_chat.py`'s own `make_client` pattern)."""

    instances: ClassVar[list[SpyLLM]] = []

    def __init__(self, responses):
        super().__init__(responses)
        SpyLLM.instances.append(self)


def test_system_prompt_names_the_correct_account_for_each_chat_service(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from allpath_trade.config import Settings
    from allpath_trade.web.app import create_app
    from tests.test_sentinel import FakeBroker

    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret",
                        **CONFIGURED_KEYS)

    SpyLLM.instances = []
    monkeypatch.setattr(
        "allpath_trade.llm.factory.build_llm",
        lambda settings, tier="chat", usage_store=None: SpyLLM(
            [LLMResponse(text="ok"), LLMResponse(text="ok"), LLMResponse(text="ok")]))

    client = TestClient(create_app(settings, broker=FakeBroker()))
    client.post("/login", data={"token": "secret"})

    # `_build()` re-resolves build_llm per session -- both accounts share
    # ONE stub factory here (same as make_client's own pattern), so each
    # `.session()` call mints its OWN SpyLLM instance; the system prompt is
    # visible on `.system_prompt` directly (no need for a live turn).
    paper_prompt = client.app.state.chat_services["paper"].session().system_prompt
    shadow_prompt = client.app.state.chat_services["shadow"].session().system_prompt

    assert "ACCOUNT: paper" in paper_prompt
    assert "Alpaca paper sandbox" in paper_prompt
    assert "ACCOUNT: shadow" in shadow_prompt
    assert "LOCAL LEDGER" in shadow_prompt
