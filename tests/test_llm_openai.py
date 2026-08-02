from types import SimpleNamespace

import pytest

from tradewind.llm.base import LLMError, ToolSpec
from tradewind.llm.openai_compat import OpenAICompatClient

TOOL = ToolSpec(name="get_quote", description="quote",
                parameters={"type": "object", "properties": {"ticker": {"type": "string"}},
                            "required": ["ticker"]})


def _resp(content=None, tool_calls=None, finish="stop"):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)])


def _tc(id_, name, args_json):
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=args_json))


class StubOpenAI:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def make(responses):
    stub = StubOpenAI(responses)
    return OpenAICompatClient("k", "test-model", client=stub), stub


def test_text_response():
    c, stub = make([_resp(content="hello")])
    out = c.complete([{"role": "user", "content": "hi"}])
    assert out.text == "hello" and out.tool_calls == []
    assert stub.calls[0]["model"] == "test-model"
    assert "tools" not in stub.calls[0] or not stub.calls[0].get("tools")


def test_tool_call_response_parses_arguments():
    c, _ = make([_resp(tool_calls=[_tc("c1", "get_quote", '{"ticker": "AAPL"}')],
                       finish="tool_calls")])
    out = c.complete([{"role": "user", "content": "price?"}], tools=[TOOL])
    [call] = out.tool_calls
    assert call.id == "c1" and call.name == "get_quote"
    assert call.arguments == {"ticker": "AAPL"}
    assert out.stop_reason == "tool_use"


def test_tools_are_converted_to_openai_format():
    c, stub = make([_resp(content="ok")])
    c.complete([{"role": "user", "content": "x"}], tools=[TOOL])
    [t] = stub.calls[0]["tools"]
    assert t["type"] == "function" and t["function"]["name"] == "get_quote"
    assert t["function"]["parameters"]["required"] == ["ticker"]


def test_assistant_tool_history_roundtrip():
    c, stub = make([_resp(content="done")])
    messages = [
        {"role": "user", "content": "price?"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "name": "get_quote", "arguments": {"ticker": "AAPL"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "200.5"},
    ]
    c.complete(messages)
    sent = stub.calls[0]["messages"]
    assert sent[1]["tool_calls"][0]["function"]["arguments"] == '{"ticker": "AAPL"}'
    assert sent[2] == {"role": "tool", "tool_call_id": "c1", "content": "200.5"}


def test_malformed_tool_arguments_raise_llm_error():
    c, _ = make([_resp(tool_calls=[_tc("c1", "get_quote", "{not json")], finish="tool_calls")])
    with pytest.raises(LLMError):
        c.complete([{"role": "user", "content": "x"}])


def test_empty_choices_raises_llm_error():
    c, _ = make([SimpleNamespace(choices=[])])
    with pytest.raises(LLMError):
        c.complete([{"role": "user", "content": "x"}])


def test_none_choices_raises_llm_error():
    c, _ = make([SimpleNamespace(choices=None)])
    with pytest.raises(LLMError):
        c.complete([{"role": "user", "content": "x"}])
