from __future__ import annotations

import anthropic

from tradewind.llm.base import LLMClient, LLMError, LLMResponse, ToolCall, ToolSpec


class AnthropicClient(LLMClient):
    def __init__(self, api_key: str, model: str, client: object | None = None,
                 max_tokens: int = 4096) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = client or anthropic.Anthropic(api_key=api_key)

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
            raise LLMError(f"llm request failed: {exc}") from exc

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
        return LLMResponse(text="".join(text_parts) or None, tool_calls=calls,
                           stop_reason=stop)

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
            else:
                out.append({"role": role, "content": m["content"]})
        return "\n\n".join(system_parts), out
