from __future__ import annotations

from allpath_trade.config import Settings
from allpath_trade.llm.anthropic_client import AnthropicClient
from allpath_trade.llm.base import LLMClient
from allpath_trade.llm.openai_compat import OpenAICompatClient

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class LLMConfigError(Exception):
    pass


def build_llm(settings: Settings, tier: str = "chat") -> LLMClient:
    models = {"chat": settings.chat_model, "review": settings.review_model,
              "memory": settings.memory_model}
    if tier not in models:
        raise LLMConfigError(f"unknown LLM tier: {tier!r}")
    model = models[tier]
    provider = settings.llm_provider.lower()
    # Every tier's client gets the same explicit request timeout (ops-
    # hardening: see config.py's llm_timeout_seconds) -- there's no per-tier
    # override because a hung call is a hung call regardless of which model
    # made it, and this is the one place all three tiers funnel through.
    if provider == "openrouter":
        if not settings.openrouter_api_key:
            raise LLMConfigError("OPENROUTER_API_KEY is not set")
        return OpenAICompatClient(settings.openrouter_api_key, model,
                                  base_url=OPENROUTER_BASE_URL,
                                  timeout=settings.llm_timeout_seconds)
    if provider == "openai":
        if not settings.openai_api_key:
            raise LLMConfigError("OPENAI_API_KEY is not set")
        return OpenAICompatClient(settings.openai_api_key, model,
                                  timeout=settings.llm_timeout_seconds)
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMConfigError("ANTHROPIC_API_KEY is not set")
        return AnthropicClient(settings.anthropic_api_key, model,
                               timeout=settings.llm_timeout_seconds)
    raise LLMConfigError(f"unknown LLM_PROVIDER: {settings.llm_provider!r}")
