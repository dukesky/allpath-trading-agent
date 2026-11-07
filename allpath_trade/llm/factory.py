from __future__ import annotations

from allpath_trade.config import Settings, normalize_llm_provider
from allpath_trade.llm.anthropic_client import AnthropicClient
from allpath_trade.llm.base import LLMClient, LLMError, LLMResponse, ToolSpec
from allpath_trade.llm.health import classify_llm_error
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

    `purpose` defaults to the tier name. TDSC R5 (2026-11): callers that need
    cost per role and account -- reflection vs consolidation, paper vs shadow,
    both on the memory tier -- relabel it with `tag_purpose` (e.g.
    "reflection:paper"). `tier` stays the tier, so the Usage panel's
    per-tier grouping is unchanged.

    Recording is best-effort: a broken usage table must never break the LLM
    call that triggered it, so any exception `LLMUsage.record` raises is
    swallowed here rather than propagated -- the caller already got (or is
    about to get) a perfectly good `LLMResponse` either way."""

    def __init__(self, inner: LLMClient, tier: str, usage_store: LLMUsage,
                 purpose: str | None = None) -> None:
        self._inner = inner
        self._tier = tier
        self._usage_store = usage_store
        self.purpose = purpose or tier

    @property
    def model(self) -> str:
        return self._inner.model

    def complete(self, messages: list[dict],
                 tools: list[ToolSpec] | None = None) -> LLMResponse:
        try:
            response = self._inner.complete(messages, tools)
        except LLMError as exc:
            self._record_failure(exc)
            raise
        try:
            self._usage_store.record(
                tier=self._tier, model=self._inner.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens, purpose=self.purpose)
        except Exception:  # noqa: BLE001, S110 — recording must never break a call
            pass
        return response

    def _record_failure(self, exc: LLMError) -> None:
        """Ops 2026-11-06: a week of 402s went unnoticed because failures
        left no trace at all. Record the failure and hand it to the store's
        outage alerter, if any. Both best-effort: the caller must still see
        the original LLMError, never a recording error."""
        error_class = classify_llm_error(exc)
        detail = str(exc)
        try:
            self._usage_store.record_failure(
                tier=self._tier, model=self._inner.model,
                error_class=error_class, detail=detail)
        except Exception:  # noqa: BLE001, S110 — recording must never break a call
            pass
        alerter = getattr(self._usage_store, "alerter", None)
        if alerter is None:
            return
        try:
            alerter.on_failure(error_class, self._tier, self._inner.model, detail)
        except Exception:  # noqa: BLE001, S110 — alerting must never break a call
            pass


def tag_purpose(client: LLMClient, purpose: str) -> LLMClient:
    """Label the `llm_usage` rows a recording client writes (TDSC R5), e.g.
    "reflection:paper". A no-op on any other client (no usage_store, or a
    test double), so call sites never need to know which one they got."""
    if isinstance(client, _RecordingClient):
        client.purpose = purpose
    return client


def build_llm(settings: Settings, tier: str = "chat",
              usage_store: LLMUsage | None = None) -> LLMClient:
    models = {"chat": settings.chat_model, "review": settings.review_model,
              "memory": settings.memory_model}
    if tier not in models:
        raise LLMConfigError(f"unknown LLM tier: {tier!r}")
    model = models[tier]
    provider = normalize_llm_provider(settings.llm_provider)
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
