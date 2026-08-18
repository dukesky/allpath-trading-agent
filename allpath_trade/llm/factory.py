from __future__ import annotations

from allpath_trade.config import Settings
from allpath_trade.llm.anthropic_client import AnthropicClient
from allpath_trade.llm.base import LLMClient, LLMResponse, ToolSpec
from allpath_trade.llm.openai_compat import OpenAICompatClient
from allpath_trade.store.llm_usage import LLMUsage

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class LLMConfigError(Exception):
    pass


class _RecordingClient(LLMClient):
    """Thin decorator around a real `LLMClient` that records every
    `complete()` call's token usage to `LLMUsage` before returning the
    response unchanged. `build_llm` returns one whenever a caller passes
    `usage_store`, so this is the ONE choke point every tier's calls funnel
    through -- chat, review, memory (and reflection, which uses the memory
    tier) -- rather than each call site recording its own.

    `purpose` is the tier name: the design intentionally doesn't distinguish
    "memory" (consolidation) from "reflection" (also memory-tier) usage --
    both are labelled by the tier they ran under, which is enough to
    attribute cost without adding a second dimension nothing downstream
    reads yet.

    Recording is best-effort: a broken usage table must never break the LLM
    call that triggered it, so any exception `LLMUsage.record` raises is
    swallowed here rather than propagated -- the caller already got (or is
    about to get) a perfectly good `LLMResponse` either way."""

    def __init__(self, inner: LLMClient, tier: str, usage_store: LLMUsage) -> None:
        self._inner = inner
        self._tier = tier
        self._usage_store = usage_store

    @property
    def model(self) -> str:
        return self._inner.model

    def complete(self, messages: list[dict],
                 tools: list[ToolSpec] | None = None) -> LLMResponse:
        response = self._inner.complete(messages, tools)
        try:
            self._usage_store.record(
                tier=self._tier, model=self._inner.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens, purpose=self._tier)
        except Exception:  # noqa: BLE001, S110 — recording must never break a call
            pass
        return response


def build_llm(settings: Settings, tier: str = "chat",
              usage_store: LLMUsage | None = None) -> LLMClient:
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
        client: LLMClient = OpenAICompatClient(
            settings.openrouter_api_key, model, base_url=OPENROUTER_BASE_URL,
            timeout=settings.llm_timeout_seconds)
    elif provider == "openai":
        if not settings.openai_api_key:
            raise LLMConfigError("OPENAI_API_KEY is not set")
        client = OpenAICompatClient(settings.openai_api_key, model,
                                    timeout=settings.llm_timeout_seconds)
    elif provider == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMConfigError("ANTHROPIC_API_KEY is not set")
        client = AnthropicClient(settings.anthropic_api_key, model,
                                 timeout=settings.llm_timeout_seconds)
    else:
        raise LLMConfigError(f"unknown LLM_PROVIDER: {settings.llm_provider!r}")
    # Wrapping is opt-in (usage_store defaults to None): every existing
    # caller that doesn't pass one gets back the exact same client type as
    # before this feature existed (isinstance checks in tests/test_llm_
    # factory.py depend on that).
    if usage_store is not None:
        return _RecordingClient(client, tier, usage_store)
    return client
