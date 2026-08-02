from tradewind.agent.loop import AgentSession
from tradewind.agent.tools import ToolRegistry
from tradewind.llm.base import LLMClient, LLMError, LLMResponse, ToolCall
from tradewind.store.conversations import ConversationStore
from tradewind.store.db import connect


class ScriptedLLM(LLMClient):
    model = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.seen = []

    def complete(self, messages, tools=None):
        self.seen.append(messages)
        if not self.responses:
            raise AssertionError("script exhausted")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def tool_response(name, args, id_="c1"):
    return LLMResponse(tool_calls=[ToolCall(id=id_, name=name, arguments=args)],
                       stop_reason="tool_use")


def make_registry():
    reg = ToolRegistry()
    reg.register("echo", "echo", {"type": "object", "properties": {}},
                 lambda **kw: f"echo:{kw}")
    return reg


def test_plain_text_turn():
    s = AgentSession(ScriptedLLM([LLMResponse(text="hi")]), make_registry(), "SYS")
    assert s.run_turn("hello") == "hi"
    assert s.history[0] == {"role": "user", "content": "hello"}
    assert s.history[-1]["content"] == "hi"


def test_tool_loop_executes_and_feeds_back():
    llm = ScriptedLLM([tool_response("echo", {"a": 1}), LLMResponse(text="done")])
    s = AgentSession(llm, make_registry(), "SYS")
    assert s.run_turn("go") == "done"
    # second LLM call saw the tool result
    tool_msgs = [m for m in llm.seen[1] if m["role"] == "tool"]
    assert tool_msgs and "echo:" in tool_msgs[0]["content"]


def test_multi_tool_call_in_one_response_executes_both_in_order():
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[
            ToolCall(id="c1", name="echo", arguments={"a": 1}),
            ToolCall(id="c2", name="echo", arguments={"a": 2})],
            stop_reason="tool_use"),
        LLMResponse(text="done")])
    s = AgentSession(llm, make_registry(), "SYS")
    assert s.run_turn("go") == "done"
    tool_msgs = [m for m in s.history if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["tool_call_id"] == "c1" and "'a': 1" in tool_msgs[0]["content"]
    assert tool_msgs[1]["tool_call_id"] == "c2" and "'a': 2" in tool_msgs[1]["content"]
    # second llm call saw both tool results
    second_call_tool_msgs = [m for m in llm.seen[1] if m["role"] == "tool"]
    assert len(second_call_tool_msgs) == 2


def test_system_prompt_is_first_message_every_call():
    llm = ScriptedLLM([LLMResponse(text="a"), LLMResponse(text="b")])
    s = AgentSession(llm, make_registry(), "SYS")
    s.run_turn("one")
    s.run_turn("two")
    assert all(seen[0] == {"role": "system", "content": "SYS"} for seen in llm.seen)


def test_iteration_limit():
    llm = ScriptedLLM([tool_response("echo", {}, id_=f"c{i}") for i in range(9)])
    s = AgentSession(llm, make_registry(), "SYS", max_iters=3)
    out = s.run_turn("loop")
    assert "limit" in out


def test_llm_error_returns_notice_and_keeps_history():
    llm = ScriptedLLM([LLMError("boom")])
    s = AgentSession(llm, make_registry(), "SYS")
    out = s.run_turn("hi")
    assert "llm error" in out
    assert s.history[0]["role"] == "user"


def test_persistence_roundtrip(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    store = ConversationStore(conn)
    cid = store.start()
    llm = ScriptedLLM([tool_response("echo", {"a": 1}), LLMResponse(text="done")])
    s = AgentSession(llm, make_registry(), "SYS", store=store, conversation_id=cid)
    s.run_turn("go")
    saved = store.history(cid)
    roles = [m["role"] for m in saved]
    assert roles == ["user", "assistant", "tool", "assistant"]
    # a resumed session rebuilds the same in-memory history
    s2 = AgentSession(ScriptedLLM([LLMResponse(text="again")]), make_registry(),
                      "SYS", store=store, conversation_id=cid)
    assert s2.history == saved


def test_on_tool_callback_invoked():
    seen = []
    llm = ScriptedLLM([tool_response("echo", {"a": 1}), LLMResponse(text="done")])
    s = AgentSession(llm, make_registry(), "SYS", on_tool=seen.append)
    s.run_turn("go")
    assert [c.name for c in seen] == ["echo"]
