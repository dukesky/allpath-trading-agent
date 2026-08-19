from __future__ import annotations

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


class LLMClient(ABC):
    model: str = ""

    @abstractmethod
    def complete(self, messages: list[dict],
                 tools: list[ToolSpec] | None = None) -> LLMResponse: ...
