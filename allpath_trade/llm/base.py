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
_IMAGE_COMPLAINT = re.compile(r"image|vision|modality|multimodal", re.IGNORECASE)


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
    if had_images and _IMAGE_COMPLAINT.search(str(exc)):
        error = LLMImageUnsupported
    return error(f"llm request failed: {exc}")


class LLMClient(ABC):
    model: str = ""

    @abstractmethod
    def complete(self, messages: list[dict],
                 tools: list[ToolSpec] | None = None) -> LLMResponse: ...
