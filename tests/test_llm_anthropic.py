import base64
from types import SimpleNamespace

import pytest

from allpath_trade.llm.anthropic_client import AnthropicClient
from allpath_trade.llm.base import LLMError, LLMImageUnsupported, ToolSpec

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


def test_parallel_tool_calls_merge_into_single_user_message():
    c, stub = make([SimpleNamespace(content=[_text_block("done")], stop_reason="end_turn")])
    messages = [
        {"role": "user", "content": "price?"},
        {"role": "assistant", "content": None,
         "tool_calls": [
             {"id": "t1", "name": "get_quote", "arguments": {"ticker": "A"}},
             {"id": "t2", "name": "get_quote", "arguments": {"ticker": "B"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "200"},
        {"role": "tool", "tool_call_id": "t2", "content": "300"},
    ]
    c.complete(messages)
    sent = stub.calls[0]["messages"]
    # exactly one user message follows the assistant turn, carrying both results
    tool_result_users = [m for m in sent if m["role"] == "user"
                         and isinstance(m["content"], list)
                         and m["content"] and m["content"][0].get("type") == "tool_result"]
    assert len(tool_result_users) == 1
    [merged] = tool_result_users
    assert len(merged["content"]) == 2
    assert merged["content"][0]["tool_use_id"] == "t1"
    assert merged["content"][0]["content"] == "200"
    assert merged["content"][1]["tool_use_id"] == "t2"
    assert merged["content"][1]["content"] == "300"


# -- Ops-hardening: `complete()` catches ANY exception from `.create()` and
# re-raises as LLMError -- this is what makes an SDK timeout (e.g.
# anthropic.APITimeoutError, raised once llm_timeout_seconds elapses)
# surface through AgentSession's `except LLMError` as the existing
# "(llm error: ...)" notice rather than an uncaught crash. Exercised here
# with a plain TimeoutError since the wrapping is exception-class-agnostic
# (a bare `except Exception`), so this covers the real SDK exception too.
def test_sdk_call_raising_any_exception_becomes_llm_error():
    class HangingStub:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **kwargs):
            raise TimeoutError("upstream hung")

    c = AnthropicClient("k", "claude-x", client=HangingStub())
    with pytest.raises(LLMError) as ei:
        c.complete([{"role": "user", "content": "x"}])
    assert "upstream hung" in str(ei.value)


# -- Token usage (LLM Usage panel, store/llm_usage.py) -----------------------

def test_usage_populated_from_response():
    usage = SimpleNamespace(input_tokens=123, output_tokens=45)
    c, _ = make([SimpleNamespace(content=[_text_block("hi")], stop_reason="end_turn",
                                 usage=usage)])
    out = c.complete([{"role": "user", "content": "hello"}])
    assert out.input_tokens == 123
    assert out.output_tokens == 45


def test_missing_usage_defaults_to_zero_never_raises():
    c, _ = make([SimpleNamespace(content=[_text_block("hi")], stop_reason="end_turn")])
    out = c.complete([{"role": "user", "content": "hello"}])
    assert out.input_tokens == 0
    assert out.output_tokens == 0


# -- Image parts (setup-wizard T5) -------------------------------------------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


def _image_message(text="what is this?"):
    return {"role": "user",
            "content": [{"type": "image", "mime": "image/png", "data": PNG_BYTES},
                        {"type": "text", "text": text}]}


def test_list_content_becomes_base64_image_blocks_then_text():
    c, stub = make([SimpleNamespace(content=[_text_block("two positions")],
                                    stop_reason="end_turn")])
    c.complete([{"role": "system", "content": "SYS"}, _image_message()])
    assert stub.calls[0]["messages"] == [{
        "role": "user",
        "content": [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64}},
            {"type": "text", "text": "what is this?"},
        ],
    }]


def test_a_provider_image_complaint_becomes_llm_image_unsupported():
    class Rejecting:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **kwargs):
            raise RuntimeError("400 messages.0.content.0: image input is not supported")

    c = AnthropicClient("k", "claude-x", client=Rejecting())
    with pytest.raises(LLMImageUnsupported):
        c.complete([_image_message()])


def test_an_image_complaint_without_images_stays_a_plain_llm_error():
    # The regex alone must not be enough: a text-only turn that happens to
    # mention "vision" in an unrelated provider error is an ordinary error.
    class Rejecting:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **kwargs):
            raise RuntimeError("429 vision-preview capacity exceeded")

    c = AnthropicClient("k", "claude-x", client=Rejecting())
    with pytest.raises(LLMError) as ei:
        c.complete([{"role": "user", "content": "hi"}])
    assert not isinstance(ei.value, LLMImageUnsupported)


def test_an_unrelated_error_on_an_image_turn_stays_a_plain_llm_error():
    class Rejecting:
        def __init__(self):
            self.messages = SimpleNamespace(create=self._create)

        def _create(self, **kwargs):
            raise TimeoutError("upstream hung")

    c = AnthropicClient("k", "claude-x", client=Rejecting())
    with pytest.raises(LLMError) as ei:
        c.complete([_image_message()])
    assert not isinstance(ei.value, LLMImageUnsupported)
