import pytest

from allpath_trade.config import Settings
from allpath_trade.llm.anthropic_client import AnthropicClient
from allpath_trade.llm.base import LLMResponse
from allpath_trade.llm.factory import LLMConfigError, _RecordingClient, build_llm
from allpath_trade.llm.openai_compat import OpenAICompatClient
from allpath_trade.store.db import connect
from allpath_trade.store.llm_usage import LLMUsage


def settings(**over):
    base = {"llm_provider": "openrouter", "openrouter_api_key": "k",
            "chat_model": "m-chat", "review_model": "m-review",
            "memory_model": "m-memory"}
    base.update(over)
    return Settings(_env_file=None, **base)


def test_openrouter_builds_openai_compat_with_base_url():
    llm = build_llm(settings(), tier="chat")
    assert isinstance(llm, OpenAICompatClient) and llm.model == "m-chat"


def test_review_tier_uses_review_model():
    assert build_llm(settings(), tier="review").model == "m-review"


def test_memory_tier_uses_memory_model():
    assert build_llm(settings(), tier="memory").model == "m-memory"


def test_unknown_tier_raises_instead_of_silently_downgrading():
    with pytest.raises(LLMConfigError) as ei:
        build_llm(settings(), tier="reviewe")
    assert "tier" in str(ei.value)


def test_anthropic_provider():
    llm = build_llm(settings(llm_provider="anthropic", anthropic_api_key="k"))
    assert isinstance(llm, AnthropicClient)


def test_missing_key_raises_named_error():
    with pytest.raises(LLMConfigError) as ei:
        build_llm(settings(openrouter_api_key=""))
    assert "OPENROUTER_API_KEY" in str(ei.value)


def test_unknown_provider_raises():
    with pytest.raises(LLMConfigError):
        build_llm(settings(llm_provider="frontier"))


# -- Ops-hardening: every tier's client must get an explicit request
# timeout, since neither SDK's own default (anthropic ~10min with retries;
# openai similar) is short enough to keep a genuinely hung call from
# blocking the after-close chain. Spy on the real SDK constructors rather
# than the client wrapper classes -- the thing that actually matters is
# what reaches `anthropic.Anthropic(...)` / `openai.OpenAI(...)`.

def test_openrouter_client_is_built_with_configured_timeout(monkeypatch):
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("allpath_trade.llm.openai_compat.OpenAI", FakeOpenAI)
    build_llm(settings(llm_timeout_seconds=42), tier="chat")
    assert captured["timeout"] == 42


def test_openai_client_is_built_with_configured_timeout(monkeypatch):
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("allpath_trade.llm.openai_compat.OpenAI", FakeOpenAI)
    build_llm(settings(llm_provider="openai", openai_api_key="k",
                       llm_timeout_seconds=42))
    assert captured["timeout"] == 42


def test_anthropic_client_is_built_with_configured_timeout(monkeypatch):
    captured = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("allpath_trade.llm.anthropic_client.anthropic.Anthropic", FakeAnthropic)
    build_llm(settings(llm_provider="anthropic", anthropic_api_key="k",
                       llm_timeout_seconds=42))
    assert captured["timeout"] == 42


def test_llm_timeout_defaults_to_180_seconds():
    assert settings().llm_timeout_seconds == 180


# ---------------------------------------------------------------------------
# usage_store / _RecordingClient -- the LLM Usage panel's one choke point.
# ---------------------------------------------------------------------------

def test_no_usage_store_returns_the_plain_client_unwrapped():
    llm = build_llm(settings(), tier="chat")
    # isinstance, not just behavior -- no usage_store passed must mean zero
    # change from before this feature existed, unwrapped client and all.
    assert isinstance(llm, OpenAICompatClient)
    assert not isinstance(llm, _RecordingClient)


def test_usage_store_wraps_the_client_in_a_recording_decorator(tmp_path):
    usage = LLMUsage(connect(tmp_path / "t.db"))
    llm = build_llm(settings(), tier="chat", usage_store=usage)
    assert isinstance(llm, _RecordingClient)
    assert llm.model == "m-chat"  # proxied through to the wrapped client


class ScriptedInner:
    model = "m-chat"

    def __init__(self, response):
        self._response = response
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        return self._response


def test_recording_client_records_usage_and_returns_response_unchanged(tmp_path):
    conn = connect(tmp_path / "t.db")
    usage = LLMUsage(conn)
    inner = ScriptedInner(LLMResponse(text="hi", input_tokens=100, output_tokens=20))
    client = _RecordingClient(inner, "chat", usage)

    out = client.complete([{"role": "user", "content": "hello"}])

    assert out.text == "hi"
    [row] = usage.summary(1)
    assert row["tier"] == "chat" and row["model"] == "m-chat"
    assert row["input_tokens"] == 100 and row["output_tokens"] == 20
    assert row["calls"] == 1


def test_recording_client_purpose_matches_tier(tmp_path):
    conn = connect(tmp_path / "t.db")
    usage = LLMUsage(conn)
    inner = ScriptedInner(LLMResponse(text="ok"))
    client = _RecordingClient(inner, "memory", usage)

    client.complete([{"role": "user", "content": "x"}])

    row = conn.execute("SELECT purpose FROM llm_usage").fetchone()
    assert row["purpose"] == "memory"


def test_recording_client_swallows_a_broken_usage_store(tmp_path):
    class BoomUsage:
        def record(self, **kwargs):
            raise RuntimeError("disk full")

    inner = ScriptedInner(LLMResponse(text="hi"))
    client = _RecordingClient(inner, "chat", BoomUsage())

    out = client.complete([{"role": "user", "content": "x"}])  # must not raise

    assert out.text == "hi"
    assert inner.calls == 1
