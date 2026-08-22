import json
import sqlite3

import pytest

from allpath_trade.agent.attachments import ImageAttachment
from allpath_trade.agent.compact import Compactor
from allpath_trade.agent.loop import AgentSession
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.llm.base import (
    LLMClient,
    LLMError,
    LLMImageUnsupported,
    LLMResponse,
    ToolCall,
)
from allpath_trade.store.conversations import ConversationStore
from allpath_trade.store.db import connect


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


def test_run_turn_accepts_a_non_colliding_extra_key():
    # ChatService's `source` (Telegram plan Task 4) is the real-world case:
    # a presentation-only bookkeeping key that must ride along on the
    # appended user message without touching the LLM-facing protocol.
    s = AgentSession(ScriptedLLM([LLMResponse(text="hi")]), make_registry(), "SYS")
    s.run_turn("hello", extra={"source": "telegram"})
    assert s.history[0] == {"role": "user", "content": "hello", "source": "telegram"}


def test_run_turn_asserts_extra_keys_never_collide_with_protocol_keys():
    # Reviewer-requested carry-forward (Telegram plan Task 4 review): an
    # `extra` key named e.g. "role" or "content" would silently clobber the
    # real protocol field via dict-unpacking order -- this must fail loudly
    # at the merge point rather than send a mangled message to the LLM.
    s = AgentSession(ScriptedLLM([]), make_registry(), "SYS")
    with pytest.raises(AssertionError):
        s.run_turn("hello", extra={"role": "system"})


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


class BrokenStore:
    """Simulates a database that has become unwritable mid-session."""

    def __init__(self):
        self.calls = 0
        self.summary_calls = 0

    def history(self, conversation_id, after_turn_id=0):
        return []

    def history_with_ids(self, conversation_id, after_turn_id=0):
        # Nothing ever actually lands in the "store" (append always raises),
        # so from its point of view the turn count after any marker is
        # permanently zero — this is what lets a Compactor be paired with a
        # BrokenStore at all, to exercise degradation under a persistence
        # failure.
        return []

    def summary(self, conversation_id):
        return "", 0

    def append(self, conversation_id, message):
        self.calls += 1
        raise sqlite3.OperationalError("attempt to write a readonly database")

    def set_summary(self, conversation_id, text, through_turn_id):
        self.summary_calls += 1
        raise sqlite3.OperationalError("attempt to write a readonly database")


def test_persistence_failure_degrades_to_in_memory(capsys):
    llm = ScriptedLLM([tool_response("echo", {"a": 1}), LLMResponse(text="still here"),
                       LLMResponse(text="and still")])
    store = BrokenStore()
    s = AgentSession(llm, make_registry(), "SYS", store=store, conversation_id=1)
    assert s.run_turn("go") == "still here"          # turn completes despite db failure
    assert [m["role"] for m in s.history] == ["user", "assistant", "tool", "assistant"]
    assert s.run_turn("again") == "and still"        # session stays usable
    assert store.calls > 4                            # kept trying to persist
    err = capsys.readouterr().err
    assert err.count("not being saved") == 1          # warned exactly once, not per message


class SetSummaryFailsStore:
    """Reads and appends work normally against a real store (turns persist,
    reads stay aligned) — only the final set_summary commit fails, e.g. disk
    fills or a write lock times out mid-summarization. Unlike BrokenStore,
    this exercises the path where the alignment check passes and the
    failure is set_summary's own, not a symptom of the store falling
    behind."""

    def __init__(self, inner: ConversationStore):
        self._inner = inner
        self.summary_calls = 0

    def append(self, conversation_id, message):
        self._inner.append(conversation_id, message)

    def history(self, conversation_id, after_turn_id=0):
        return self._inner.history(conversation_id, after_turn_id)

    def history_with_ids(self, conversation_id, after_turn_id=0):
        return self._inner.history_with_ids(conversation_id, after_turn_id)

    def summary(self, conversation_id):
        return self._inner.summary(conversation_id)

    def set_summary(self, conversation_id, text, through_turn_id):
        self.summary_calls += 1
        raise sqlite3.OperationalError("attempt to write a readonly database")


def test_set_summary_failure_degrades_instead_of_crashing(tmp_path, capsys):
    """Reproduces the incident the review traced: turns persist normally,
    compaction triggers, both reads succeed and stay aligned — only the
    final set_summary commit fails. Before the fix this propagated straight
    out of maybe_compact and out of run_turn, ending a live chat mid-turn.
    It must instead degrade like an LLM failure: the marker never moves and
    the session keeps working turn after turn."""
    real = ConversationStore(connect(tmp_path / "compact.db"))
    cid = real.start()
    for _ in range(10):
        real.append(cid, {"role": "user", "content": "x" * 300})
        real.append(cid, {"role": "assistant", "content": "x" * 300})
    broken = SetSummaryFailsStore(real)

    compactor_llm = ScriptedLLM([LLMResponse(text=f"summary {i}") for i in range(30)])
    compactor = Compactor(compactor_llm, broken, budget_tokens=1200)
    session_llm = ScriptedLLM([LLMResponse(text=f"reply {i}") for i in range(30)])
    session = AgentSession(session_llm, make_registry(), "SYS", store=broken,
                           conversation_id=cid, compactor=compactor)

    for i in range(10):
        reply = session.run_turn(f"question {i} " + "x" * 300)
        assert reply == f"reply {i}"              # every turn completes despite the failure

    assert broken.summary_calls > 0                # set_summary was attempted and raised
    assert real.summary(cid) == ("", 0)             # marker never moved
    err = capsys.readouterr().err
    assert "compaction skipped" in err
    assert err.count("compaction skipped") == 1     # warned once, not per attempt


def test_compaction_degrades_instead_of_crashing_when_store_is_unwritable(capsys):
    """A persistence failure must never end a conversation in progress
    (AgentSession._append's docstring). But when a Compactor is attached,
    self.history keeps growing in memory while a BrokenStore never records
    anything — the exact same shape of mismatch maybe_compact's alignment
    check exists to catch. Before the fix that check was an `assert`
    sitting after the summarizing LLM call, so on this path it fired and
    raised an uncaught AssertionError straight out of run_turn, ending the
    chat the persistence-degradation path exists to keep alive. It must
    instead degrade quietly: the turn still completes, no exception
    escapes, and the (fictional, since nothing ever persists) summary
    marker is never touched."""
    llm = ScriptedLLM([LLMResponse(text=f"reply {i}") for i in range(20)])
    store = BrokenStore()
    compactor = Compactor(llm, store, budget_tokens=200)
    s = AgentSession(llm, make_registry(), "SYS", store=store,
                     conversation_id=1, compactor=compactor)

    for i in range(10):
        reply = s.run_turn(f"question {i} " + "x" * 300)
        assert reply == f"reply {i}"          # turn completes despite db failure

    assert store.calls > 0                     # kept trying to persist raw turns
    assert store.summary_calls == 0             # compaction never advanced the marker
    err = capsys.readouterr().err
    assert err.count("not being saved") == 1    # warned exactly once, not per message


# --- image attachments (setup-wizard T5) ------------------------------------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _png(name="positions.png"):
    return ImageAttachment(data=PNG_BYTES, mime="image/png", name=name)


def test_images_reach_the_llm_as_list_content_image_parts_then_text():
    llm = ScriptedLLM([LLMResponse(text="I see two positions")])
    s = AgentSession(llm, make_registry(), "SYS")
    s.run_turn("what do you make of this?", images=[_png()])
    last = llm.seen[0][-1]
    assert last == {
        "role": "user",
        "content": [
            {"type": "image", "mime": "image/png", "data": PNG_BYTES},
            {"type": "text", "text": "what do you make of this?"},
        ],
    }


def test_images_ride_along_on_every_iteration_of_a_tool_loop():
    # Anthropic rejects a history whose earlier user turn changes shape
    # between calls; the parts must be identical on the follow-up call.
    llm = ScriptedLLM([tool_response("echo", {"a": 1}), LLMResponse(text="done")])
    s = AgentSession(llm, make_registry(), "SYS")
    s.run_turn("read this", images=[_png()])
    first_user = next(m for m in llm.seen[1] if m["role"] == "user")
    assert first_user["content"][0]["type"] == "image"


def test_images_are_never_kept_on_the_history_message(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    store = ConversationStore(conn)
    cid = store.start()
    llm = ScriptedLLM([LLMResponse(text="ok")])
    s = AgentSession(llm, make_registry(), "SYS", store=store, conversation_id=cid)
    s.run_turn("here", images=[_png()])

    user = s.history[-2]
    assert "images" not in user
    assert user["content"] == "here"
    assert user["display"] == "[image: positions.png, 1 KB] here"
    saved = store.history(cid)
    assert "images" not in saved[0]
    assert saved[0]["content"] == "here"
    rows = conn.execute("SELECT message FROM conversation_turns").fetchall()
    blob = "".join(r["message"] for r in rows)
    assert "images" not in blob and "PNG" not in blob
    indexed = conn.execute("SELECT content FROM search_index").fetchall()
    assert all("PNG" not in r["content"] for r in indexed)
    assert any("[image: positions.png" in r["content"] for r in indexed)


def test_images_are_dropped_even_when_the_llm_raises(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    store = ConversationStore(conn)
    cid = store.start()
    llm = ScriptedLLM([LLMError("boom")])
    s = AgentSession(llm, make_registry(), "SYS", store=store, conversation_id=cid)
    reply = s.run_turn("here", images=[_png()])

    assert reply.startswith("(llm error:")
    assert all("images" not in m for m in s.history)
    rows = conn.execute("SELECT message FROM conversation_turns").fetchall()
    assert all("images" not in r["message"] for r in rows)


def test_an_image_unsupported_error_propagates_instead_of_becoming_a_notice(tmp_path):
    # ChatService maps this one to its own fixed reply; run_turn must not
    # swallow it into the generic "(llm error: ...)" notice first. The
    # transient images key is still popped on the way out.
    conn = connect(tmp_path / "db.sqlite")
    store = ConversationStore(conn)
    cid = store.start()
    llm = ScriptedLLM([LLMImageUnsupported("llm request failed: no image support")])
    s = AgentSession(llm, make_registry(), "SYS", store=store, conversation_id=cid)
    with pytest.raises(LLMImageUnsupported):
        s.run_turn("here", images=[_png()])
    assert all("images" not in m for m in s.history)
    assert [m["role"] for m in s.history] == ["user"]


def test_a_text_only_turn_is_unchanged_by_the_images_parameter():
    llm = ScriptedLLM([LLMResponse(text="hi")])
    s = AgentSession(llm, make_registry(), "SYS")
    s.run_turn("hello", images=[])
    assert s.history[0] == {"role": "user", "content": "hello"}
    assert llm.seen[0] == [{"role": "system", "content": "SYS"},
                           {"role": "user", "content": "hello"}]


def test_an_images_only_turn_sends_no_empty_text_part():
    # Anthropic 400s on an empty text block, with a message that matches no
    # "unsupported" pattern -- the user would get a raw provider error for
    # a perfectly ordinary "screenshot, no caption" message.
    llm = ScriptedLLM([LLMResponse(text="ok")])
    s = AgentSession(llm, make_registry(), "SYS")
    s.run_turn("", images=[_png()])
    assert llm.seen[0][-1]["content"] == [
        {"type": "image", "mime": "image/png", "data": PNG_BYTES}]
    # ...and the stored display line has no trailing space.
    assert s.history[0]["display"] == "[image: positions.png, 1 KB]"


def _compaction_run(tmp_path, db_name, final_images, monkeypatch):
    """Drive a session past its context budget, ending on one turn that may
    carry images, and report what the Compactor actually did plus every
    message `estimate_tokens` was asked to weigh."""
    import allpath_trade.agent.compact as compact_mod

    weighed: list[dict] = []
    real_estimate = compact_mod.estimate_tokens

    def spy(messages):
        weighed.extend(messages)
        return real_estimate(messages)

    monkeypatch.setattr(compact_mod, "estimate_tokens", spy)
    store = ConversationStore(connect(tmp_path / db_name))
    cid = store.start()
    flushes: list[int] = []
    summarizer = ScriptedLLM([LLMResponse(text=f"summary {i}") for i in range(40)])
    compactor = Compactor(summarizer, store, budget_tokens=1200,
                          on_before_compact=lambda msgs: flushes.append(len(msgs)))
    session = AgentSession(ScriptedLLM([LLMResponse(text=f"reply {i}") for i in range(40)]),
                           make_registry(), "SYS", store=store,
                           conversation_id=cid, compactor=compactor)
    for i in range(15):
        session.run_turn(f"question {i} " + "x" * 600)
    session.run_turn("last one", images=final_images)
    return ({"summaries": len(summarizer.seen), "flushes": len(flushes),
             "context": len(session.history)}, weighed)


def test_a_turn_with_huge_images_compacts_exactly_like_the_text_only_baseline(
        tmp_path, monkeypatch):
    # Review finding (Important 1): with `images` on the history dict,
    # estimate_tokens' `json.dumps(m, default=str)` valued a 5 MB
    # screenshot at millions of tokens, so `_cut_index` never found a
    # fitting suffix -- compaction AND the on_before_compact memory flush
    # silently stopped for that turn, after seconds of re-serializing bytes
    # under ChatService's turn lock.
    big = [ImageAttachment(data=b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024),
                           mime="image/png", name=f"shot{i}.png") for i in range(4)]
    baseline, _ = _compaction_run(tmp_path, "baseline.db", None, monkeypatch)
    with_images, weighed = _compaction_run(tmp_path, "images.db", big, monkeypatch)

    assert baseline["summaries"] > 0 and baseline["flushes"] > 0  # it really compacted
    assert with_images == baseline
    # estimate_tokens never sees an image: no bytes, no `images` key, and
    # no message anywhere near a megabyte.
    assert all("images" not in m for m in weighed)
    assert max(len(json.dumps(m, default=str)) for m in weighed) < 5_000
