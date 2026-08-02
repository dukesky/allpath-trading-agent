from __future__ import annotations

from tradewind.config import Settings
from tradewind.llm.anthropic_client import AnthropicClient
from tradewind.llm.base import LLMClient
from tradewind.llm.openai_compat import OpenAICompatClient

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class LLMConfigError(Exception):
    pass


def build_llm(settings: Settings, tier: str = "chat") -> LLMClient:
    model = settings.chat_model if tier == "chat" else settings.review_model
    provider = settings.llm_provider.lower()
    if provider == "openrouter":
        if not settings.openrouter_api_key:
            raise LLMConfigError("OPENROUTER_API_KEY is not set")
        return OpenAICompatClient(settings.openrouter_api_key, model,
                                  base_url=OPENROUTER_BASE_URL)
    if provider == "openai":
        if not settings.openai_api_key:
            raise LLMConfigError("OPENAI_API_KEY is not set")
        return OpenAICompatClient(settings.openai_api_key, model)
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMConfigError("ANTHROPIC_API_KEY is not set")
        return AnthropicClient(settings.anthropic_api_key, model)
    raise LLMConfigError(f"unknown LLM_PROVIDER: {settings.llm_provider!r}")
