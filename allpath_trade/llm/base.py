from __future__ import annotations

import re
from abc import ABC, abstractmethod

from pydantic import BaseModel


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict  # JSON schema


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict


class LLMResponse(BaseModel):
    text: str | None = None
    tool_calls: list[ToolCall] = []
    stop_reason: str = "end"  # end | tool_use | length | other
    # Token usage for the LLM Usage panel (store/llm_usage.py). Both clients
    # read these off the SDK response's own usage object; a missing/absent
    # usage field (an older SDK, an unusual response shape) degrades to 0
    # rather than raising -- usage is accounting, never load-bearing for the
    # response itself.
    input_tokens: int = 0
    output_tokens: int = 0


class LLMError(Exception):
    pass


class LLMImageUnsupported(LLMError):
    """The request carried image parts and the provider rejected it for
    that reason -- i.e. the configured chat model has no vision input.

    A subclass of LLMError so every existing `except LLMError` (the CLI,
    the reflector, the consolidator) keeps degrading exactly as before;
    only the callers that actually send images (ChatService, via
    AgentSession.run_turn) single it out, to answer with the fixed
    "switch CHAT_MODEL" reply instead of a raw provider string."""


# Providers word this differently and none of them give a machine-readable
# code for it: Anthropic says "image input is not supported", OpenAI/
# OpenRouter say "does not support image input"/"modality", others say
# "vision". Matching the message is the only signal available -- so it is
# deliberately paired with `has_image_parts` below, never used alone: a
# rate-limit error that merely mentions "vision-preview" on a text-only
# turn must stay an ordinary LLMError.
#
# Whole-branch review (Important 2): matching the bare WORDS
# image/vision/modality was far too wide even on an image turn. Model slugs
# routinely carry them ("meta-llama/llama-3.2-90b-vision-instruct",
# "gpt-4o"), and a provider names the model in its 429/402/502 text -- so a
# transient rate limit on a screenshot turn answered with the fixed "this
# model can't read images, switch CHAT_MODEL" reply, sending the user off to
# change a setting that was never the problem. Phrases only: something in
# the message has to actually SAY the modality is refused.
_IMAGE_COMPLAINT = re.compile(
    r"does not support image"
    r"|image input.{0,40}not supported"
    r"|not support.{0,40}(image|vision)"
    r"|unsupported.{0,40}(image|modality)"
    r"|no vision"
    r"|does not accept images"
    r"|media_type",
    re.IGNORECASE)

# Statuses that describe the CONNECTION or the ACCOUNT, never the request's
# content: auth, payment, timeout, rate limit, and every 5xx. A provider
# returning one of these has not judged the modality at all, whatever its
# prose happens to mention -- so these are decided before the text is even
# looked at. A refusal for lack of vision is a 400 (or arrives with no
# status at all, from an SDK or transport layer that doesn't expose one).
_NOT_ABOUT_CONTENT = frozenset({401, 402, 403, 408, 429})


def _transport_status(exc: Exception) -> bool:
    """Whether the provider exception carries a status code that rules out
    "the model can't read images" as the reason. Both SDKs put an int
    `status_code` on their error types; anything else (absent, a string, a
    test double) reads as "no information" rather than as a status."""
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int) or isinstance(status, bool):
        return False
    return status in _NOT_ABOUT_CONTENT or status >= 500


def has_image_parts(messages: list[dict]) -> bool:
    """True when any message uses the unified list-content shape with an
    `image` part (agent/loop.py's `_protocol_message`)."""
    for m in messages:
        content = m.get("content")
        if isinstance(content, list) and any(
                isinstance(p, dict) and p.get("type") == "image" for p in content):
            return True
    return False


def wrap_request_error(exc: Exception, *, had_images: bool) -> LLMError:
    """The single place both clients turn a provider exception into ours."""
    error = LLMError
    if (had_images and not _transport_status(exc)
            and _IMAGE_COMPLAINT.search(str(exc))):
        error = LLMImageUnsupported
    return error(f"llm request failed: {exc}")


class LLMClient(ABC):
    model: str = ""

    @abstractmethod
    def complete(self, messages: list[dict],
                 tools: list[ToolSpec] | None = None) -> LLMResponse: ...
