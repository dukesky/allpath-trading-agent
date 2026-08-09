from __future__ import annotations

import json
import urllib.error
from typing import Self

import pytest

from allpath_trade.web import models_catalog


@pytest.fixture(autouse=True)
def _cold_cache():
    """Every test starts from -- and leaves -- a cold cache. The module-level
    (timestamp, list) cache is process-global; without resetting it, whichever
    test happens to run first would seed it for every test after, making the
    "hits the network once" and "falls back on failure" assertions depend on
    test order instead of on the behavior under test.
    """
    models_catalog._cache = None
    yield
    models_catalog._cache = None


# A canned OpenRouter /models response, not the real API: a text chat model,
# a multimodal (vision-in, text-out) chat model, a text embedding model, and
# a text-to-image model. The filter must keep the first two and drop the
# last two -- proving it goes by output modality, not by a provider-id
# allowlist that would miss the next provider's embedding model.
_CANNED_RESPONSE = {
    "data": [
        {"id": "anthropic/claude-sonnet-5", "architecture": {"modality": "text->text"}},
        {"id": "openai/gpt-5.2", "architecture": {"modality": "text->text"}},
        {"id": "google/gemini-3-pro-vision", "architecture": {"modality": "text+image->text"}},
        {"id": "openai/text-embedding-3-small", "architecture": {"modality": "text->embedding"}},
        {"id": "stability/stable-diffusion-3", "architecture": {"modality": "text->image"}},
    ]
}


class _FakeHTTPResponse:
    """Minimal stand-in for the object urllib.request.urlopen() returns --
    just enough (a context manager whose .read() gives JSON bytes) for
    models_catalog's fetch code to work with, without opening a socket."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def test_filter_keeps_text_chat_models_and_drops_embeddings_and_image_models(monkeypatch):
    monkeypatch.setattr(
        models_catalog.urllib.request, "urlopen",
        lambda *a, **k: _FakeHTTPResponse(_CANNED_RESPONSE))

    models = models_catalog.list_models("openrouter")

    assert models == sorted([
        "anthropic/claude-sonnet-5",
        "openai/gpt-5.2",
        "google/gemini-3-pro-vision",
    ])
    assert "openai/text-embedding-3-small" not in models
    assert "stability/stable-diffusion-3" not in models


def test_network_failure_falls_back_to_the_curated_list(monkeypatch):
    def _raise(*a, **k):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(models_catalog.urllib.request, "urlopen", _raise)

    assert models_catalog.list_models("openrouter") == models_catalog.FALLBACK_MODELS["openrouter"]


def test_malformed_response_falls_back_to_the_curated_list(monkeypatch):
    # Not just transport failures: a 200 with a body that isn't the expected
    # shape (OpenRouter changes its schema, or serves an HTML error page)
    # must fall back too, rather than raising out of the settings page.
    monkeypatch.setattr(
        models_catalog.urllib.request, "urlopen",
        lambda *a, **k: _FakeHTTPResponse({"unexpected": "shape"}))

    assert models_catalog.list_models("openrouter") == models_catalog.FALLBACK_MODELS["openrouter"]


def test_successful_fetch_is_cached_so_a_second_call_does_not_hit_the_network(monkeypatch):
    calls = []

    def _fake_urlopen(*a, **k):
        calls.append(1)
        return _FakeHTTPResponse(_CANNED_RESPONSE)

    monkeypatch.setattr(models_catalog.urllib.request, "urlopen", _fake_urlopen)

    first = models_catalog.list_models("openrouter")
    second = models_catalog.list_models("openrouter")

    assert first == second
    assert len(calls) == 1


def test_cache_expires_after_its_ttl(monkeypatch):
    calls = []

    def _fake_urlopen(*a, **k):
        calls.append(1)
        return _FakeHTTPResponse(_CANNED_RESPONSE)

    monkeypatch.setattr(models_catalog.urllib.request, "urlopen", _fake_urlopen)

    models_catalog.list_models("openrouter")
    # Age the cache past its TTL without a real sleep in the test.
    timestamp, cached = models_catalog._cache
    models_catalog._cache = (timestamp - models_catalog._CACHE_TTL_SECONDS - 1, cached)

    models_catalog.list_models("openrouter")

    assert len(calls) == 2


def test_anthropic_and_openai_return_the_static_list_and_never_touch_the_network(monkeypatch):
    def _raise(*a, **k):
        raise AssertionError("anthropic/openai must never hit the network")

    monkeypatch.setattr(models_catalog.urllib.request, "urlopen", _raise)

    assert models_catalog.list_models("anthropic") == models_catalog.FALLBACK_MODELS["anthropic"]
    assert models_catalog.list_models("openai") == models_catalog.FALLBACK_MODELS["openai"]
