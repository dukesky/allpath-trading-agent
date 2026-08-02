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
    context, kept = c.maybe_compact(cid, history)
    assert context == history
    assert kept == history


def test_compaction_summarizes_the_oldest_messages(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    for _ in range(10):
        s.append(cid, big("user", 2000))
        s.append(cid, big("assistant", 2000))
    llm = ScriptedLLM([LLMResponse(text="earlier: the user asked about NVDA")])
    c = Compactor(llm, s, budget_tokens=2_000)
    history = s.history(cid)

    result, kept = c.maybe_compact(cid, history)

    assert len(result) < len(history)
    assert result[0]["role"] == "system"
    assert "NVDA" in result[0]["content"]
    assert s.summary(cid)[1] > 0
    # kept is the raw tail (no summary frame) the caller must adopt as its
    # new history; result is that same tail with the frame prepended.
    assert kept == result[1:]


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

    result, _kept_history = c.maybe_compact(cid, s.history(cid))

    visible = [m for m in result if m["role"] != "system"]
    ids = {m["tool_call_id"] for m in visible if m["role"] == "tool"}
    called = {call["id"] for m in visible for call in m.get("tool_calls", [])}
    assert ids == called


def test_compaction_keeps_a_tool_pair_intact_when_it_survives_the_cut(tmp_path):
    """The test above only proves splitting doesn't happen when *nothing*
    survives the cut — `kept`/`ids`/`called` all end up empty, so `ids ==
    called` reduces to `set() == set()` and never actually shows a pair being
    kept together. Here the cut lands before the pair, so it must appear
    intact in the tail — a naive budget-driven cut that ignored role
    boundaries could still land between the tool call and its result."""
    s = store(tmp_path)
    cid = s.start()
    s.append(cid, big("user", 3000))
    s.append(cid, big("assistant", 3000))
    s.append(cid, big("user", 50))
    s.append(cid, {"role": "assistant", "content": "",
                   "tool_calls": [{"id": "c1", "name": "quote", "arguments": {}}]})
    s.append(cid, {"role": "tool", "tool_call_id": "c1", "content": "199.0"})
    s.append(cid, big("user", 50))
    llm = ScriptedLLM([LLMResponse(text="summary")])
    c = Compactor(llm, s, budget_tokens=150)

    _context, kept = c.maybe_compact(cid, s.history(cid))

    tool_msgs = [m for m in kept if m["role"] == "tool"]
    call_msgs = [m for m in kept if m.get("tool_calls")]
    assert tool_msgs and call_msgs, "the pair should have survived the cut, not been dropped"
    assert {m["tool_call_id"] for m in tool_msgs} == \
        {call["id"] for m in call_msgs for call in m["tool_calls"]}


def test_cut_index_never_lands_between_a_tool_call_and_its_result():
    """Direct check on _cut_index across a spread of targets: whatever the
    budget, the boundary between the assistant's tool_calls message (index 3)
    and its tool result (index 4) must never be chosen."""
    from allpath_trade.agent.compact import _cut_index

    messages = [
        big("user", 3000), big("assistant", 3000), big("user", 50),
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "name": "quote", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "199.0"},
        big("user", 50),
    ]
    for target in range(0, 2000, 25):
        assert _cut_index(messages, target) not in (3, 4)


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
    context, kept = c.maybe_compact(cid, history)
    assert context == history
    assert kept == history
    assert s.summary(cid) == ("", 0)


def test_cut_zero_returns_the_oversized_context_unchanged(tmp_path):
    """Every message here is bigger on its own than `target`, so no
    user-boundary suffix ever fits — _cut_index returns 0 and maybe_compact
    must degrade to the framed-but-oversized context rather than picking an
    unsafe cut, summarizing nothing, and leaving the marker untouched."""
    s = store(tmp_path)
    cid = s.start()
    s.append(cid, big("user", 3000))
    s.append(cid, big("assistant", 3000))
    history = s.history(cid)
    llm = ScriptedLLM([])  # must never be called: no cut means no summarizing
    c = Compactor(llm, s, budget_tokens=500)

    context, kept = c.maybe_compact(cid, history)

    assert context == history
    assert kept == history
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
    result2, kept2 = c2.maybe_compact(cid, hist2)

    _, through2 = s.summary(cid)
    kept_in_db = s.history(cid, after_turn_id=through2)
    kept_in_result = [m for m in result2 if m["role"] != "system"]
    assert kept_in_db == kept_in_result
    assert kept_in_db == kept2


def test_two_compaction_rounds_via_run_turn_drop_no_turns(tmp_path):
    """Reproduces the AgentSession/Compactor alignment bug directly through
    run_turn — the way it actually happens in production — rather than by
    re-fetching history from the store between rounds the way the test above
    does. That's exactly why the test above didn't catch this: it hands the
    second round a freshly-fetched, already-aligned `hist2`, but AgentSession
    never re-fetches — it reuses its own `self.history` across the whole
    session. Before the fix, run_turn never trimmed self.history, so it grew
    long while the store's marker advanced, and the second compaction's `cut`
    indexed into the wrong list — silently dropping turns that were never
    folded into any summary.

    The assertion mirrors the reviewer's repro: after compaction has run more
    than once, whatever the store considers "not yet summarized" (everything
    after the marker) must be exactly what the session is still holding in
    memory. No turn may be unaccounted for in both places at once."""
    from allpath_trade.agent.loop import AgentSession
    from allpath_trade.agent.tools import ToolRegistry

    s = store(tmp_path)
    cid = s.start()
    for _ in range(10):
        s.append(cid, big("user", 300))
        s.append(cid, big("assistant", 300))

    compactor_llm = ScriptedLLM([LLMResponse(text=f"summary {i}") for i in range(100)])
    compactor = Compactor(compactor_llm, s, budget_tokens=1200)
    session_llm = ScriptedLLM([LLMResponse(text=f"reply {i}") for i in range(30)])
    session = AgentSession(session_llm, ToolRegistry(), "sys", store=s,
                           conversation_id=cid, compactor=compactor)

    for i in range(15):
        session.run_turn(f"question {i} " + "x" * 300)

    # Sanity: the scenario must actually force compaction more than once,
    # otherwise this test would pass vacuously without exercising round two.
    assert len(compactor_llm.seen) >= 2

    _, through = s.summary(cid)
    kept_in_db = s.history(cid, after_turn_id=through)
    assert kept_in_db == session.history


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
