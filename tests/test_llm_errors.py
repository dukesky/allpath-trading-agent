"""`llm/base.py::wrap_request_error` -- the one place a provider exception
becomes ours, and the only thing standing between a rate-limit error and the
fixed "this model can't read images" reply.

Whole-branch review (Important 2): the old rule was "the turn carried images
AND the message mentions image/vision/modality/multimodal anywhere", which
misfires on every 429/402/5xx whose text merely names the model -- and model
slugs routinely contain "vision" (`llama-3.2-90b-vision-instruct`). Two
guards now: a transport/quota status code is never an image complaint, and
the text match is phrase-level rather than word-level.
"""

from __future__ import annotations

import pytest

from allpath_trade.llm.base import (
    LLMError,
    LLMImageUnsupported,
    wrap_request_error,
)


class _ProviderError(RuntimeError):
    """What both SDKs raise: an exception carrying the HTTP status."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


# The three misfires from the review: every one of them carries a status
# code that means "try again / pay up / provider is down", and every one of
# them mentions a vision-capable model by name.
MISFIRES = [
    _ProviderError("Rate limit exceeded for "
                   "meta-llama/llama-3.2-90b-vision-instruct", 429),
    _ProviderError("402 Insufficient credits to run "
                   "qwen/qwen-2-vl-72b-instruct (image input)", 402),
    _ProviderError("502 Bad gateway from upstream provider for "
                   "gpt-4o (image, vision, multimodal)", 502),
]


@pytest.mark.parametrize("exc", MISFIRES, ids=lambda e: str(e)[:24])
def test_a_transport_or_quota_status_is_never_an_image_complaint(exc):
    wrapped = wrap_request_error(exc, had_images=True)
    assert isinstance(wrapped, LLMError)
    assert not isinstance(wrapped, LLMImageUnsupported)


@pytest.mark.parametrize("status", [401, 402, 403, 408, 429, 500, 503, 599])
def test_every_retryable_status_stays_a_plain_error(status):
    exc = _ProviderError("this model does not support image input", status)
    assert not isinstance(wrap_request_error(exc, had_images=True),
                          LLMImageUnsupported)


def test_a_real_refusal_is_still_recognized():
    exc = _ProviderError("This model does not support image input", 400)
    assert isinstance(wrap_request_error(exc, had_images=True),
                      LLMImageUnsupported)


def test_a_media_type_rejection_is_recognized():
    exc = _ProviderError("400 Invalid media_type for this model", 400)
    assert isinstance(wrap_request_error(exc, had_images=True),
                      LLMImageUnsupported)


@pytest.mark.parametrize("message", [
    "messages.0.content.0: image input is not supported by claude-x",
    "The selected model does not support vision",
    "unsupported modality: image",
    "This deployment has no vision capability",
    "the model does not accept images",
])
def test_the_phrases_providers_actually_use_are_recognized(message):
    # No status_code at all (a bare RuntimeError from an older SDK, or a
    # transport library's own exception) -- the phrase has to carry it.
    assert isinstance(wrap_request_error(RuntimeError(message), had_images=True),
                      LLMImageUnsupported)


@pytest.mark.parametrize("message", [
    "Rate limit exceeded for llama-3.2-90b-vision-instruct",
    "context length exceeded: 200000 tokens (image tokens included)",
    "upstream timeout while streaming from gpt-4o-vision",
])
def test_a_model_slug_mentioning_vision_is_not_a_refusal(message):
    # Even without a status code to lean on: naming a modality is not the
    # same as saying it is unsupported.
    wrapped = wrap_request_error(RuntimeError(message), had_images=True)
    assert not isinstance(wrapped, LLMImageUnsupported)


def test_a_refusal_on_a_text_only_turn_is_still_a_plain_error():
    exc = _ProviderError("This model does not support image input", 400)
    assert not isinstance(wrap_request_error(exc, had_images=False),
                          LLMImageUnsupported)


def test_a_non_integer_status_code_is_ignored_rather_than_trusted():
    # `status_code` is whatever the SDK put there; a string (or a Mock)
    # must not crash the wrapper, and must not be read as "transport".
    exc = RuntimeError("This model does not support image input")
    exc.status_code = "429"  # type: ignore[attr-defined]
    assert isinstance(wrap_request_error(exc, had_images=True),
                      LLMImageUnsupported)
