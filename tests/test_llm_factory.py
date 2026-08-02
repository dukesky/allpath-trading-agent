import pytest

from tradewind.config import Settings
from tradewind.llm.anthropic_client import AnthropicClient
from tradewind.llm.factory import LLMConfigError, build_llm
from tradewind.llm.openai_compat import OpenAICompatClient


def settings(**over):
    base = {"llm_provider": "openrouter", "openrouter_api_key": "k",
            "chat_model": "m-chat", "review_model": "m-review"}
    base.update(over)
    return Settings(_env_file=None, **base)


def test_openrouter_builds_openai_compat_with_base_url():
    llm = build_llm(settings(), tier="chat")
    assert isinstance(llm, OpenAICompatClient) and llm.model == "m-chat"


def test_review_tier_uses_review_model():
    assert build_llm(settings(), tier="review").model == "m-review"


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
