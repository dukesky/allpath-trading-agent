from __future__ import annotations

import json
import threading
import time
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

    `_input_modalities` (setup-wizard T6) is the same kind of process-global
    fetch residue and is reset alongside it, so a modality test can't be
    handed another test's canned catalog.
    """
    models_catalog._cache = None
    models_catalog._input_modalities = {}
    yield
    models_catalog._cache = None
    models_catalog._input_modalities = {}


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


def test_stalled_fetch_returns_fallback_within_a_bounded_wall_clock_deadline(monkeypatch):
    # Regression test: `urlopen(..., timeout=N)` alone does not bound
    # wall-clock time -- CPython resolves DNS via getaddrinfo() *before*
    # the socket timeout takes effect, so a stalled resolver (a
    # firewalled/sandboxed deployment with no route to openrouter.ai) can
    # hang past `_TIMEOUT_SECONDS`. `_fetch_openrouter_models` sleeping well
    # past the deadline stands in for that stall; `list_models` must still
    # return the fallback within a bounded time by enforcing the deadline
    # itself (via `_fetch_pool` / `future.result(timeout=...)`), not by
    # trusting `urlopen`'s own timeout to cover every failure mode.
    #
    # A short, injected deadline (not the real 5s production one) keeps
    # this test fast rather than sleeping out a real production timeout.
    deadline = 0.2
    monkeypatch.setattr(models_catalog, "_TIMEOUT_SECONDS", deadline)

    finished = threading.Event()

    def _stalled(*a, **k):
        time.sleep(deadline * 5)
        finished.set()
        return ["late-result-should-not-affect-this-test"]

    monkeypatch.setattr(models_catalog, "_fetch_openrouter_models", _stalled)

    started = time.monotonic()
    models = models_catalog.list_models("openrouter")
    elapsed = time.monotonic() - started

    assert models == models_catalog.FALLBACK_MODELS["openrouter"]
    # Generous bound (2x the deadline) to avoid flakiness under CI
    # scheduling jitter, while still proving the wait is bounded rather
    # than open-ended -- the stalled fetch itself sleeps 5x the deadline,
    # so this can only pass if `.result(timeout=...)` actually cut the
    # wait short instead of riding along with the stalled call.
    assert elapsed < deadline * 2

    # Let the abandoned background fetch actually finish (and write its
    # late result to `_cache`, per `_fetch_and_cache_openrouter_models`'s
    # docstring) before this test returns -- otherwise its thread could
    # still be running when the next test's autouse `_cold_cache` fixture
    # resets `_cache`, and then clobber that reset out from under it.
    assert finished.wait(timeout=deadline * 20)


def test_anthropic_and_openai_return_the_static_list_and_never_touch_the_network(monkeypatch):
    def _raise(*a, **k):
        raise AssertionError("anthropic/openai must never hit the network")

    monkeypatch.setattr(models_catalog.urllib.request, "urlopen", _raise)

    assert models_catalog.list_models("anthropic") == models_catalog.FALLBACK_MODELS["anthropic"]
    assert models_catalog.list_models("openai") == models_catalog.FALLBACK_MODELS["openai"]


# ---------------------------------------------------------------------------
# setup-wizard T6: input modalities, for the chat page's vision hint.
# ---------------------------------------------------------------------------

_MODALITY_RESPONSE = {
    "data": [
        {"id": "text/only", "architecture": {"modality": "text->text",
                                             "input_modalities": ["text"]}},
        {"id": "sees/images", "architecture": {"modality": "text+image->text",
                                               "input_modalities": ["text", "image"]}},
        {"id": "no/declaration", "architecture": {"modality": "text->text"}},
    ]
}


def test_input_modalities_is_unknown_until_a_catalog_fetch_has_succeeded(monkeypatch):
    def _raise(*a, **k):
        raise AssertionError("the vision hint must never trigger a fetch")

    monkeypatch.setattr(models_catalog.urllib.request, "urlopen", _raise)

    assert models_catalog.cached_input_modalities("openrouter", "sees/images") is None


def test_input_modalities_come_from_the_last_successful_fetch(monkeypatch):
    monkeypatch.setattr(
        models_catalog.urllib.request, "urlopen",
        lambda *a, **k: _FakeHTTPResponse(_MODALITY_RESPONSE))

    models_catalog.list_models("openrouter")

    assert models_catalog.cached_input_modalities("openrouter", "sees/images") == [
        "text", "image"]
    assert models_catalog.cached_input_modalities("openrouter", "text/only") == ["text"]
    # A model the catalog lists WITHOUT an `input_modalities` key is unknown,
    # not "no image support" -- recording `[]` for it would turn a missing
    # field into a confident (and wrong) "this model can't see" hint.
    assert models_catalog.cached_input_modalities("openrouter", "no/declaration") is None
    # And a slug the catalog doesn't list at all is unknown too.
    assert models_catalog.cached_input_modalities("openrouter", "who/knows") is None


def test_input_modalities_are_only_meaningful_for_openrouter(monkeypatch):
    monkeypatch.setattr(
        models_catalog.urllib.request, "urlopen",
        lambda *a, **k: _FakeHTTPResponse(_MODALITY_RESPONSE))
    models_catalog.list_models("openrouter")

    # anthropic/openai never fetch a catalog, so nothing is ever known about
    # their slugs -- the hint stays silent rather than guessing.
    assert models_catalog.cached_input_modalities("anthropic", "sees/images") is None
