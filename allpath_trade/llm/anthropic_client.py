from __future__ import annotations

import base64

import anthropic

from allpath_trade.llm.base import (
    LLMClient,
    LLMResponse,
    ToolCall,
    ToolSpec,
    has_image_parts,
    wrap_request_error,
)


class AnthropicClient(LLMClient):
    def __init__(self, api_key: str, model: str, client: object | None = None,
                 max_tokens: int = 4096, timeout: float = 180.0) -> None:
        self.model = model
        self.max_tokens = max_tokens
        # `timeout` only applies when we build the real SDK client -- an
        # injected `client` (tests, or a caller with its own setup) owns its
        # own timeout already. See config.py's llm_timeout_seconds for why
        # this needs to be explicit at all: the anthropic SDK's own default
        # is generous enough (~10min with retries) to hang the entire
        # after-close chain on a stuck call.
        self._client = client or anthropic.Anthropic(api_key=api_key, timeout=timeout)

    def complete(self, messages: list[dict],
                 tools: list[ToolSpec] | None = None) -> LLMResponse:
        system, converted = self._convert(messages)
        kwargs: dict = {"model": self.model, "max_tokens": self.max_tokens,
                        "messages": converted}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description,
                 "input_schema": t.parameters}
                for t in tools
            ]
        try:
            resp = self._client.messages.create(**kwargs)
        except Exception as exc:
            raise wrap_request_error(
                exc, had_images=has_image_parts(messages)) from exc

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name,
                                      arguments=dict(block.input)))
        stop = "tool_use" if calls else (
            "length" if resp.stop_reason == "max_tokens" else "end")
        # `resp.usage` is present on every real Anthropic SDK response, but
        # `getattr(..., None)` degrades gracefully for a test double or an
        # unexpected future shape rather than raising -- see LLMResponse's
        # own docstring on why usage is never load-bearing.
        usage = getattr(resp, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        return LLMResponse(text="".join(text_parts) or None, tool_calls=calls,
                           stop_reason=stop, input_tokens=input_tokens,
                           output_tokens=output_tokens)

    @staticmethod
    def _convert(messages: list[dict]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        out: list[dict] = []
        for m in messages:
            role = m["role"]
            if role == "system":
                system_parts.append(m["content"])
            elif role == "assistant" and m.get("tool_calls"):
                blocks: list[dict] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                blocks.extend(
                    {"type": "tool_use", "id": c["id"], "name": c["name"],
                     "input": c["arguments"]}
                    for c in m["tool_calls"])
                out.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"],
                         "content": m["content"]}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                        and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif isinstance(m["content"], list):
                # Unified image parts (agent/attachments.py) -> Anthropic
                # content blocks. Bytes are base64'd here, at the last
                # possible moment, and never stored on the message.
                out.append({"role": role,
                            "content": [AnthropicClient._part(p)
                                        for p in m["content"]]})
            else:
                out.append({"role": role, "content": m["content"]})
        return "\n\n".join(system_parts), out

    @staticmethod
    def _part(part: dict) -> dict:
        if part.get("type") == "image":
            return {"type": "image",
                    "source": {"type": "base64", "media_type": part["mime"],
                               "data": base64.b64encode(part["data"]).decode()}}
        return {"type": "text", "text": part.get("text", "")}
