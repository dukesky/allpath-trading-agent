import pytest

from allpath_trade.config import Settings
from allpath_trade.llm.anthropic_client import AnthropicClient
from allpath_trade.llm.factory import LLMConfigError, build_llm
from allpath_trade.llm.openai_compat import OpenAICompatClient


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
