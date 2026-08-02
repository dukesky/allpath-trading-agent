from allpath_trade.agent.compact import Compactor, estimate_tokens
from allpath_trade.llm.base import LLMError, LLMResponse
from allpath_trade.store.conversations import ConversationStore
from allpath_trade.store.db import connect
from tests.test_agent_loop import ScriptedLLM


class FailingLLM:
    def complete(self, messages, tools=None):
        raise LLMError("boom")


def store(tmp_path) -> ConversationStore:
    return ConversationStore(connect(tmp_path / "t.db"))


def test_history_can_start_after_a_turn_id(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    for i in range(4):
        s.append(cid, {"role": "user", "content": f"m{i}"})
    all_turns = s.history_with_ids(cid)
    cutoff = all_turns[1][0]
    tail = s.history(cid, after_turn_id=cutoff)
    assert [m["content"] for m in tail] == ["m2", "m3"]


def test_summary_round_trips(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    s.set_summary(cid, "user prefers dividends", 7)
    assert s.summary(cid) == ("user prefers dividends", 7)


def big(role: str, n: int) -> dict:
    return {"role": role, "content": "x" * n}


def test_estimate_tokens_scales_with_content():
    assert estimate_tokens([big("user", 4000)]) > 900


def test_no_compaction_under_budget(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    llm = ScriptedLLM([])
    c = Compactor(llm, s, budget_tokens=10_000)
    history = [big("user", 100), big("assistant", 100)]
    assert c.maybe_compact(cid, history) == history


def test_compaction_summarizes_the_oldest_messages(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    for _ in range(10):
        s.append(cid, big("user", 2000))
        s.append(cid, big("assistant", 2000))
    llm = ScriptedLLM([LLMResponse(text="earlier: the user asked about NVDA")])
    c = Compactor(llm, s, budget_tokens=2_000)
    history = s.history(cid)

    result = c.maybe_compact(cid, history)

    assert len(result) < len(history)
    assert result[0]["role"] == "system"
    assert "NVDA" in result[0]["content"]
    assert s.summary(cid)[1] > 0


def test_compaction_never_splits_a_tool_call_from_its_result(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    s.append(cid, big("user", 3000))
    s.append(cid, {"role": "assistant", "content": "",
                   "tool_calls": [{"id": "c1", "name": "quote", "arguments": {}}]})
    s.append(cid, {"role": "tool", "tool_call_id": "c1", "content": "199.0"})
    s.append(cid, big("assistant", 3000))
    s.append(cid, big("user", 100))
    llm = ScriptedLLM([LLMResponse(text="summary")])
    c = Compactor(llm, s, budget_tokens=500)

    result = c.maybe_compact(cid, s.history(cid))

    kept = [m for m in result if m["role"] != "system"]
    ids = {m["tool_call_id"] for m in kept if m["role"] == "tool"}
    called = {call["id"] for m in kept for call in m.get("tool_calls", [])}
    assert ids == called


def test_flush_hook_runs_before_summarizing(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    for _ in range(6):
        s.append(cid, big("user", 3000))
        s.append(cid, big("assistant", 100))
    order: list[str] = []
    llm = ScriptedLLM([LLMResponse(text="summary")])
    # budget_tokens=1500: with these message sizes a smaller budget leaves no
    # user-boundary suffix under target (see deviation note in the report),
    # so maybe_compact would bail out at cut==0 before ever calling the hook —
    # this value is the smallest that forces a genuine cut.
    c = Compactor(llm, s, budget_tokens=1500,
                  on_before_compact=lambda msgs: order.append("flush"))
    c.maybe_compact(cid, s.history(cid))
    assert order == ["flush"]


def test_llm_failure_leaves_history_untouched(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    for _ in range(6):
        s.append(cid, big("user", 3000))
    history = s.history(cid)
    # budget_tokens=1500, same reasoning as above: must force a real cut so
    # the compactor actually calls the (failing) LLM, otherwise this test
    # would pass vacuously without exercising the failure path at all.
    c = Compactor(FailingLLM(), s, budget_tokens=1500)
    assert c.maybe_compact(cid, history) == history
    assert s.summary(cid) == ("", 0)


def test_second_compaction_round_sets_a_correct_marker(tmp_path):
    """A long-running conversation gets compacted more than once. The second
    round's `history` argument only covers turns after the first summary
    marker — the through-marker computed for the second round must line up
    with that same subset, not with turn ids counted from the start."""
    s = store(tmp_path)
    cid = s.start()
    for _ in range(10):
        s.append(cid, big("user", 2000))
        s.append(cid, big("assistant", 2000))
    c1 = Compactor(ScriptedLLM([LLMResponse(text="round 1")]), s, budget_tokens=2_000)
    c1.maybe_compact(cid, s.history(cid))
    _, through1 = s.summary(cid)

    for _ in range(10):
        s.append(cid, big("user", 2000))
        s.append(cid, big("assistant", 2000))
    hist2 = s.history(cid, after_turn_id=through1)
    c2 = Compactor(ScriptedLLM([LLMResponse(text="round 2")]), s, budget_tokens=2_000)
    result2 = c2.maybe_compact(cid, hist2)

    _, through2 = s.summary(cid)
    kept_in_db = s.history(cid, after_turn_id=through2)
    kept_in_result = [m for m in result2 if m["role"] != "system"]
    assert kept_in_db == kept_in_result


def test_session_resumes_from_the_summary_marker(tmp_path):
    from allpath_trade.agent.loop import AgentSession
    from allpath_trade.agent.tools import ToolRegistry

    s = store(tmp_path)
    cid = s.start()
    s.append(cid, {"role": "user", "content": "old and forgotten"})
    ids = [tid for tid, _ in s.history_with_ids(cid)]
    s.set_summary(cid, "briefing", ids[-1])
    s.append(cid, {"role": "user", "content": "still visible"})

    session = AgentSession(ScriptedLLM([]), ToolRegistry(), "sys",
                           store=s, conversation_id=cid)
    assert [m["content"] for m in session.history] == ["still visible"]
