"""Keyless chat in `allpath-trade demo`: the scripted stand-in answers a few
sample questions through the real tool loop, exists only in demo mode, and
steps aside the moment an LLM key is configured."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from allpath_trade import cli
from allpath_trade.config import Settings
from allpath_trade.demo import OUTBOUND_ENV
from allpath_trade.demo_chat import (
    LABEL,
    PROMPT_DRAFT,
    PROMPT_RISK,
    PROMPT_WHY,
    SUGGESTED_PROMPTS,
    UNSCRIPTED_REPLY,
    DemoLLM,
    build_demo_llm,
)
from allpath_trade.llm.base import ToolSpec
from allpath_trade.llm.factory import LLMConfigError
from allpath_trade.llm.openai_compat import OpenAICompatClient
from allpath_trade.web.account_ctx import ACCOUNT_COOKIE
from allpath_trade.web.app import create_app
from tests.helpers import assert_english_only, dismiss_setup
from tests.test_sentinel import FakeBroker

LLM_ENV = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
           "LLM_PROVIDER")


def _clean_env(monkeypatch):
    for key in (*LLM_ENV, *(k.upper() for k in OUTBOUND_ENV)):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A demo with no LLM key anywhere."""
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # restored at teardown, after prepare_demo's chdir
    app, token = cli.prepare_demo(tmp_path / "demo", "127.0.0.1")
    with TestClient(app) as client:
        client.post("/login", data={"token": token})
        yield client


def _send(client, text):
    r = client.post("/chat/send", data={"message": text})
    assert r.status_code == 200
    assert_english_only(r.text)
    return r.text


def _last_reply(client):
    return client.app.state.chat_service.messages()[-1]["content"]


# -- the page --


def test_keyless_demo_chat_opens_on_a_sample_conversation_and_prompts(demo):
    html = demo.get("/chat").text
    assert "Chat needs an LLM key" not in html
    assert "What happened in my account overnight?" in html
    assert "Scripted demo answer" in html
    assert 'id="demo-prompts"' in html
    for prompt in SUGGESTED_PROMPTS:
        assert f'data-fill="{prompt}"'.replace("'", "&#39;") in html
    assert 'id="chat-form"' in html
    assert_english_only(html)


def test_demo_prompts_are_not_offered_on_the_shadow_account(demo):
    demo.cookies.set(ACCOUNT_COOKIE, "shadow")
    assert 'id="demo-prompts"' not in demo.get("/chat").text


# -- scripted prompts run the real tools --


def test_why_prompt_reads_the_pending_order_through_the_tools(demo):
    html = _send(demo, PROMPT_WHY)
    reply = _last_reply(demo)
    assert reply.startswith(LABEL)
    assert "add-on-pullback" in reply and "nvda-ai-infra" in reply
    assert "212.40" in reply and "buy $2000" in reply
    assert demo.app.state.chat_service.activity == [
        "list_pending_reviews", "get_quote", "get_portfolio"]
    assert "list_pending_reviews" in html  # the activity line


def test_why_prompt_is_honest_once_the_order_is_resolved(demo):
    c = demo.app.state.holder.get()
    order = next(r["id"] for r in c.queue.list() if r["kind"] == "order")
    c.queue.reject(order, "not now")
    _send(demo, PROMPT_WHY)
    assert "no pending NVDA order" in _last_reply(demo)


def test_risk_prompt_ranks_the_sample_positions(demo):
    _send(demo, PROMPT_RISK)
    reply = _last_reply(demo)
    assert "NVDA is the riskiest position" in reply
    assert "8.4% of equity" in reply and "AAPL" in reply
    assert demo.app.state.chat_service.activity == ["get_portfolio"]


def test_draft_prompt_queues_a_real_strategy_review(demo):
    c = demo.app.state.holder.get()
    before = {r["id"] for r in c.queue.list()}
    html = _send(demo, PROMPT_DRAFT)
    new = [r for r in c.queue.list() if r["id"] not in before]
    assert len(new) == 1
    row = new[0]
    assert row["kind"] == "strategy_revision" and row["source"] == "chat"
    assert row["strategy_id"] == "amzn-pullback-entry" and row["ticker"] == "AMZN"
    assert f"queued it as #{row['id']}" in _last_reply(demo)
    assert demo.app.state.chat_service.activity == ["get_quote", "draft_strategy"]
    # The chat card points at Pending, where the draft shows as a diff.
    assert f"Strategy draft #{row['id']}" in html
    assert "amzn-pullback-entry" in demo.get("/reviews").text
    # Approving writes the YAML inside the demo's own directory.
    c.queue.approve(row["id"])
    saved = c.strategies.directory / "amzn-pullback-entry.yaml"
    assert "price < 220" in saved.read_text()


def test_draft_prompt_for_an_unknown_ticker_says_so(demo):
    c = demo.app.state.holder.get()
    before = len(c.queue.list())
    _send(demo, "Draft a pullback strategy for ZZZZ.")
    assert "could not get a sample price for ZZZZ" in _last_reply(demo)
    assert len(c.queue.list()) == before


def test_unscripted_question_gets_the_honest_keyless_reply(demo):
    _send(demo, "Should I buy TSLA?")
    assert _last_reply(demo) == UNSCRIPTED_REPLY
    assert "keyless demo" in UNSCRIPTED_REPLY and "LLM key" in UNSCRIPTED_REPLY
    assert demo.app.state.chat_service.activity == []


def test_scripted_turns_never_leave_the_demo_directory(demo, tmp_path):
    for prompt in SUGGESTED_PROMPTS:
        _send(demo, prompt)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["demo"]


# -- the stub is unit-testable without a server --


def test_stub_without_tools_never_calls_one():
    llm = DemoLLM()
    resp = llm.complete([{"role": "user", "content": PROMPT_RISK}], tools=[])
    assert resp.tool_calls == [] and resp.text
    spec = ToolSpec(name="web_search", description="", parameters={})
    resp = llm.complete([{"role": "user", "content": PROMPT_RISK}], tools=[spec])
    assert resp.tool_calls == [] and "no open positions" in resp.text


def test_stub_tool_call_ids_are_unique_across_turns(demo):
    _send(demo, PROMPT_RISK)
    _send(demo, PROMPT_RISK)
    ids = [c["id"] for m in demo.app.state.chat_service.messages()
           for c in m.get("tool_calls") or []]
    assert len(ids) == 2 and len(set(ids)) == 2


# -- only in demo mode; a real key wins --


def test_serve_without_a_key_has_no_stub(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret",
                        alpaca_api_key="k", alpaca_secret_key="s")
    client = TestClient(create_app(settings, broker=FakeBroker()))
    client.post("/login", data={"token": "secret"})
    dismiss_setup(client)
    service = client.app.state.chat_service
    assert service._llm_builder is None
    with pytest.raises(LLMConfigError):
        service.session()
    html = client.get("/chat", follow_redirects=True).text
    assert "Chat needs an LLM key" in html
    assert 'id="demo-prompts"' not in html and "Scripted demo answer" not in html
    r = client.post("/chat/send", data={"message": PROMPT_RISK})
    assert "Chat needs an LLM key" in r.text


def test_demo_with_a_key_builds_the_real_client_and_seeds_no_chat(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    monkeypatch.chdir(tmp_path)
    app, token = cli.prepare_demo(tmp_path / "demo", "127.0.0.1")
    with TestClient(app) as client:
        client.post("/login", data={"token": token})
        service = client.app.state.chat_service
        session = service.session()
        assert isinstance(session.llm._inner, OpenAICompatClient)
        assert not isinstance(session.compactor.llm, DemoLLM)
        assert session.history == []
        assert service.demo_prompts() == ()
        html = client.get("/chat").text
        assert 'id="demo-prompts"' not in html
        assert "Scripted demo answer" not in html
        assert "Ask me anything about the market" in html  # usual onboarding


def test_build_demo_llm_falls_back_only_when_no_key(monkeypatch):
    _clean_env(monkeypatch)
    assert isinstance(build_demo_llm(Settings(_env_file=None)), DemoLLM)
    keyed = Settings(_env_file=None, openrouter_api_key="sk-test-not-real")
    assert isinstance(build_demo_llm(keyed), OpenAICompatClient)


def test_sample_chat_is_seeded_once(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    workdir = tmp_path / "demo"
    cli.prepare_demo(workdir, "127.0.0.1")
    app, _ = cli.prepare_demo(workdir, "127.0.0.1")
    assert len(app.state.chat_service.messages()) == 2
