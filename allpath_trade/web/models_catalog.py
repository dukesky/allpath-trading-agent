"""LLM model catalog for the settings page's model dropdowns.

The settings GET renders this on every request, so the OpenRouter fetch must
never be able to hang or 500 that page: a 5-second timeout plus an
any-failure fallback to a static curated list keep the page usable no matter
what openrouter.ai is doing. A module-level (timestamp, list) cache makes a
successful fetch cheap to reuse across the TTL window instead of hitting the
network on every render.

`anthropic` and `openai` skip the network entirely -- there is no public,
keyless "list models" endpoint for either the way OpenRouter has one, and
their model lineups change rarely enough that a curated static list is the
right tradeoff (an authenticated call on every settings render, just to
populate a dropdown, is not).

Uses stdlib `urllib.request` rather than `httpx`: httpx is currently a dev-only
dependency (pulled in for the FastAPI test client), and this module makes
exactly one GET with no need for anything httpx adds over the stdlib for that
-- promoting it to a runtime dependency for one request isn't justified.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_TIMEOUT_SECONDS = 5
_CACHE_TTL_SECONDS = 3600

# Current, sensible slugs as of this writing. These are what the dropdown
# (and any code path that never reaches the network) shows -- they are not
# meant to stay perfectly current forever, only to be a reasonable default
# and a safe landing spot when the live catalog can't be fetched.
FALLBACK_MODELS: dict[str, list[str]] = {
    "openrouter": [
        "anthropic/claude-haiku-4.5",
        "anthropic/claude-opus-5",
        "anthropic/claude-sonnet-5",
        "google/gemini-3.1-pro-preview",
        "google/gemini-3.6-flash",
        "meta-llama/llama-4-maverick",
        "openai/gpt-5.2",
        "openai/gpt-5.2-pro",
    ],
    "anthropic": [
        "claude-haiku-4-5",
        "claude-opus-5",
        "claude-sonnet-5",
    ],
    "openai": [
        "gpt-5.2",
        "gpt-5.2-mini",
        "gpt-5.2-nano",
    ],
}

# None until the first successful OpenRouter fetch; then (fetched_at, models).
# A failed fetch deliberately leaves this alone -- it neither seeds a bad
# cache entry nor evicts a still-good one, so a transient outage between two
# renders doesn't cost a page that was working a moment ago.
_cache: tuple[float, list[str]] | None = None


def _is_chat_model(entry: dict[str, Any]) -> bool:
    """A model belongs in a chat-model dropdown only if its output is text --
    this is what excludes text-to-image models (`text->image`) and embedding
    endpoints (`text->embedding`), while still keeping multimodal chat models
    that accept images alongside text (`text+image->text`). Going by output
    modality rather than a hand-maintained id blocklist means the next
    provider's embedding model is dropped too, without a code change.
    """
    architecture = entry.get("architecture") or {}
    modality = architecture.get("modality") or ""
    return modality.endswith("->text")


def _fetch_openrouter_models() -> list[str]:
    request = urllib.request.Request(
        _OPENROUTER_MODELS_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read())
    entries = payload["data"]
    ids = {entry["id"] for entry in entries if _is_chat_model(entry) and entry.get("id")}
    return sorted(ids)


def list_models(provider: str) -> list[str]:
    """The model slugs to offer for `provider`. Never raises and never
    blocks past `_TIMEOUT_SECONDS` -- callers (the settings page) can call
    this unconditionally on every render.
    """
    global _cache

    if provider != "openrouter":
        return list(FALLBACK_MODELS.get(provider, []))

    now = time.time()
    if _cache is not None and now - _cache[0] < _CACHE_TTL_SECONDS:
        return _cache[1]

    try:
        models = _fetch_openrouter_models()
    except Exception:  # noqa: BLE001 — any failure (network, timeout, bad
        # JSON, unexpected schema) must degrade to the fallback list rather
        # than take the settings page down with it; see module docstring.
        return list(FALLBACK_MODELS["openrouter"])

    if not models:
        # An empty-but-well-formed response is as useless to the dropdown as
        # a failure -- fall back rather than caching (and then serving) a
        # blank catalog for the next hour.
        return list(FALLBACK_MODELS["openrouter"])

    _cache = (now, models)
    return models
