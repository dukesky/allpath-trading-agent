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

import concurrent.futures
import json
import time
import urllib.request
from typing import Any

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_TIMEOUT_SECONDS = 5
_CACHE_TTL_SECONDS = 3600

# `urlopen(..., timeout=_TIMEOUT_SECONDS)` alone does not bound wall-clock
# time: CPython's socket.create_connection() resolves DNS via
# getaddrinfo() *before* the socket timeout takes effect, and a redirect
# chain gets a fresh `timeout` window per hop. A firewalled/sandboxed
# deployment with a stalled resolver for openrouter.ai can therefore hang
# well past 5 seconds, contradicting the "never blocks" contract above.
#
# Running the fetch on a worker thread and bounding the wait with
# `future.result(timeout=...)` enforces a true wall-clock deadline no
# matter where inside the call it's stuck. This mirrors
# `allpath_trade.web.routes.dashboard`'s `_with_timeout` / `_broker_pool`
# pattern exactly (submit-then-bounded-`.result()`); it isn't reused
# directly because that pool is sized and named for dashboard broker
# polling, and importing it here would couple two independently-owned
# route modules together for one GET elsewhere. A single-worker local pool
# gets the same guarantee without the cross-module coupling.
_fetch_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="models-catalog-fetch")

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

# setup-wizard T6: model id -> the `architecture.input_modalities` list the
# last successful OpenRouter fetch reported for it, lowercased. Populated as
# a side effect of `_fetch_openrouter_models` (the dropdown's fetch), read
# by `cached_input_modalities` for the chat page's vision hint -- which must
# never fetch on its own: the hint is informational, and /chat is not
# allowed to grow a network call on every render the way /settings has one.
# Only entries that actually DECLARE input_modalities are recorded; see
# `_record_input_modalities`.
_input_modalities: dict[str, list[str]] = {}


def _record_input_modalities(entries: list[dict[str, Any]]) -> None:
    """Replace `_input_modalities` from a freshly fetched catalog.

    An entry with no `architecture.input_modalities` key is deliberately
    NOT recorded: "the catalog doesn't say" and "the catalog says text
    only" are different answers, and collapsing the first into `[]` would
    let a missing field render as a confident "this model can't read
    images" warning. Rebuilt wholesale rather than merged so a model
    dropped upstream doesn't linger with stale capabilities.
    """
    global _input_modalities
    fresh: dict[str, list[str]] = {}
    for entry in entries:
        model_id = entry.get("id")
        architecture = entry.get("architecture") or {}
        modalities = architecture.get("input_modalities")
        if not model_id or not isinstance(modalities, list) or not modalities:
            continue
        fresh[model_id] = [str(m).lower() for m in modalities]
    _input_modalities = fresh


def cached_input_modalities(provider: str, model: str) -> list[str] | None:
    """What `model` accepts as input, per the last successful catalog fetch
    -- or None when that is simply not known.

    Never fetches, never raises, never blocks. None means "no opinion" and
    covers every uncertain case at once: a provider with no live catalog
    (`anthropic`/`openai` are curated static lists -- see FALLBACK_MODELS),
    a process that has not rendered /settings yet, a slug the catalog
    doesn't list, and an entry that declares no input modalities. Callers
    must treat None as "assume it works", not as "no image support".
    """
    if provider != "openrouter":
        return None
    modalities = _input_modalities.get(model)
    return list(modalities) if modalities else None


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
    # Recorded off the FULL entry list, not just the chat-model subset the
    # dropdown keeps -- `cached_input_modalities` answers about whatever
    # slug is configured as CHAT_MODEL, which need not still be in `ids`.
    _record_input_modalities(entries)
    return sorted(ids)


def _fetch_and_cache_openrouter_models() -> list[str]:
    """The unit of work submitted to `_fetch_pool`. Writes a successful
    result straight to `_cache` itself, rather than leaving that to the
    caller in `list_models`.

    That matters because the caller waits for this with a bounded
    `future.result(timeout=...)` (see `list_models`) -- if the deadline
    expires first, Python has no way to cancel a thread that's already
    running, so this function keeps executing in the background after
    `list_models` has already returned the fallback list. If it goes on to
    succeed, writing `_cache` here means that late result still isn't
    wasted: the *next* call to `list_models` finds a warm cache instead of
    hitting the network again. A late write racing a fresh one from a
    subsequent call is harmless -- both are valid successful fetches, and
    whichever lands last just becomes the current cache entry.
    """
    models = _fetch_openrouter_models()
    if models:
        global _cache
        _cache = (time.time(), models)
    return models


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
        # `.result(timeout=...)` is what actually enforces the wall-clock
        # deadline -- it raises concurrent.futures.TimeoutError as soon as
        # _TIMEOUT_SECONDS elapses, regardless of whether the submitted
        # call has even started yet (queued behind an earlier hung fetch)
        # or is stuck inside DNS resolution, connect, read, or a redirect.
        # Reading `_TIMEOUT_SECONDS` here rather than capturing it as a
        # default argument keeps it monkeypatchable by tests.
        models = _fetch_pool.submit(_fetch_and_cache_openrouter_models).result(
            timeout=_TIMEOUT_SECONDS)
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
