from __future__ import annotations

import json

from openai import OpenAI

from tradewind.llm.base import LLMClient, LLMError, LLMResponse, ToolCall, ToolSpec


class OpenAICompatClient(LLMClient):
    """OpenAI-compatible chat completions — covers OpenAI and OpenRouter."""

    def __init__(self, api_key: str, model: str, base_url: str | None = None,
                 client: object | None = None) -> None:
        self.model = model
        self._client = client or OpenAI(api_key=api_key, base_url=base_url)

    def complete(self, messages: list[dict],
                 tools: list[ToolSpec] | None = None) -> LLMResponse:
        kwargs: dict = {"model": self.model, "messages": self._to_openai(messages)}
        if tools:
            kwargs["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description,
                              "parameters": t.parameters}}
                for t in tools
            ]
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # SDK/network errors become LLMError
            raise LLMError(f"llm request failed: {exc}") from exc

        if not getattr(resp, "choices", None):
            raise LLMError("llm returned no choices")
        choice = resp.choices[0]
        msg = choice.message
        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise LLMError(
                    f"malformed tool arguments for {tc.function.name}") from exc
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        stop = "tool_use" if calls else (
            "length" if choice.finish_reason == "length" else "end")
        return LLMResponse(text=msg.content, tool_calls=calls, stop_reason=stop)

    @staticmethod
    def _to_openai(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                out.append({
                    "role": "assistant",
                    "content": m.get("content"),
                    "tool_calls": [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"],
                                      "arguments": json.dumps(c["arguments"])}}
                        for c in m["tool_calls"]
                    ],
                })
            else:
                out.append(dict(m))
        return out
