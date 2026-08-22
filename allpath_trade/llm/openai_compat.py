from __future__ import annotations

import base64
import json

from openai import OpenAI

from allpath_trade.llm.base import (
    LLMClient,
    LLMError,
    LLMResponse,
    ToolCall,
    ToolSpec,
    has_image_parts,
    wrap_request_error,
)


class OpenAICompatClient(LLMClient):
    """OpenAI-compatible chat completions — covers OpenAI and OpenRouter."""

    def __init__(self, api_key: str, model: str, base_url: str | None = None,
                 client: object | None = None, timeout: float = 180.0) -> None:
        self.model = model
        # See AnthropicClient.__init__: `timeout` only applies when we build
        # the real SDK client, and exists to bound a genuinely hung call
        # (config.py's llm_timeout_seconds) rather than trust the openai
        # SDK's own default.
        self._client = client or OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

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
            raise wrap_request_error(
                exc, had_images=has_image_parts(messages)) from exc

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
        # `resp.usage` is present on every real OpenAI-compatible response
        # (OpenAI, OpenRouter), but degrades to 0 via `getattr` for a test
        # double or a provider that omits it -- see LLMResponse's own
        # docstring on why usage is never load-bearing.
        usage = getattr(resp, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        return LLMResponse(text=msg.content, tool_calls=calls, stop_reason=stop,
                           input_tokens=input_tokens, output_tokens=output_tokens)

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
            elif isinstance(m.get("content"), list):
                # Unified image parts (agent/attachments.py) -> the
                # chat-completions multimodal shape. Bytes become a data:
                # URL here, at the last possible moment, and are never
                # stored on the message.
                out.append({"role": m["role"],
                            "content": [_part(p) for p in m["content"]]})
            else:
                out.append(dict(m))
        return out


def _part(part: dict) -> dict:
    if part.get("type") == "image":
        data = base64.b64encode(part["data"]).decode()
        return {"type": "image_url",
                "image_url": {"url": f"data:{part['mime']};base64,{data}"}}
    return {"type": "text", "text": part.get("text", "")}
