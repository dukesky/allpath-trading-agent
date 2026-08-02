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


class LLMError(Exception):
    pass


class LLMClient(ABC):
    model: str = ""

    @abstractmethod
    def complete(self, messages: list[dict],
                 tools: list[ToolSpec] | None = None) -> LLMResponse: ...
