from types import SimpleNamespace

from tradewind.llm.anthropic_client import AnthropicClient
from tradewind.llm.base import ToolSpec

TOOL = ToolSpec(name="get_quote", description="quote",
                parameters={"type": "object", "properties": {}})


class StubAnthropic:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_block(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def make(responses):
    stub = StubAnthropic(responses)
    return AnthropicClient("k", "claude-x", client=stub), stub


def test_text_and_system_extraction():
    c, stub = make([SimpleNamespace(content=[_text_block("hi")], stop_reason="end_turn")])
    out = c.complete([{"role": "system", "content": "you are X"},
                      {"role": "user", "content": "hello"}])
    assert out.text == "hi"
    assert stub.calls[0]["system"] == "you are X"
    assert stub.calls[0]["messages"] == [{"role": "user", "content": "hello"}]


def test_tool_use_response():
    c, _ = make([SimpleNamespace(
        content=[_tool_block("t1", "get_quote", {"ticker": "AAPL"})],
        stop_reason="tool_use")])
    out = c.complete([{"role": "user", "content": "x"}], tools=[TOOL])
    [call] = out.tool_calls
    assert call.id == "t1" and call.arguments == {"ticker": "AAPL"}
    assert out.stop_reason == "tool_use"


def test_history_conversion_tool_use_and_result():
    c, stub = make([SimpleNamespace(content=[_text_block("done")], stop_reason="end_turn")])
    messages = [
        {"role": "user", "content": "price?"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1", "name": "get_quote", "arguments": {"ticker": "A"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "200"},
    ]
    c.complete(messages)
    sent = stub.calls[0]["messages"]
    assert sent[1]["role"] == "assistant"
    assert sent[1]["content"][0]["type"] == "tool_use"
    assert sent[2]["role"] == "user"
    assert sent[2]["content"][0]["type"] == "tool_result"
    assert sent[2]["content"][0]["tool_use_id"] == "t1"
