import sqlite3

from allpath_trade.agent.compact import Compactor, estimate_tokens
from allpath_trade.llm.base import LLMError, LLMResponse
from allpath_trade.store.conversations import ConversationStore
from allpath_trade.store.db import connect
from tests.test_agent_loop import ScriptedLLM


class FailingLLM:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        raise LLMError("boom")


class SetSummaryFailingStore:
    """Wraps a real store so reads and appends behave normally and stay
    aligned — only the final set_summary commit fails, mirroring an incident
    where the disk fills or a write lock times out mid-summarization."""

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


class SummaryReadFailingStore:
    """summary() raises — the earliest guarded read in maybe_compact, before
    `previous`/`since` are even known."""

    def summary(self, conversation_id):
        raise sqlite3.OperationalError("database is locked")

    def history_with_ids(self, conversation_id, after_turn_id=0):
        raise AssertionError("must not be reached: summary() should fail first")

    def set_summary(self, conversation_id, text, through_turn_id):
        raise AssertionError("must not be reached: summary() should fail first")


class MisalignedStore:
    """summary() and history_with_ids() both succeed (no exception), but
    history_with_ids() returns fewer turns than `history` — the store has
    fallen behind while `history` keeps growing (see AgentSession._append),
    a different condition from a store call raising outright."""

    def summary(self, conversation_id):
        return "", 0

    def history_with_ids(self, conversation_id, after_turn_id=0):
        return []

    def set_summary(self, conversation_id, text, through_turn_id):
        raise AssertionError("must not be reached: alignment check should catch this first")


class HistoryIdsReadFailingStore:
    """summary() succeeds (nothing previously summarized); history_with_ids()
    raises — the second guarded read, used to fetch turn_ids for the
    alignment check."""

    def summary(self, conversation_id):
        return "", 0

    def history_with_ids(self, conversation_id, after_turn_id=0):
        raise sqlite3.OperationalError("database is locked")

    def set_summary(self, conversation_id, text, through_turn_id):
        raise AssertionError("must not be reached: history_with_ids() should fail first")


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
    # budget_tokens=3_000: a first-ever compaction reserves
    # FIRST_SUMMARY_RESERVE_TOKENS off `target` before there's any previous
    # frame to measure — 2_000 no longer leaves enough room for a real cut
    # to be found (see compact.py); 3_000 is the smallest that still does.
    c = Compactor(llm, s, budget_tokens=3_000)
    history = s.history(cid)

    result, kept = c.maybe_compact(cid, history)

    assert len(result) < len(history)
    assert result[0]["role"] == "system"
    assert "NVDA" in result[0]["content"]
    assert s.summary(cid)[1] > 0
    # kept is the raw tail (no summary frame) the caller must adopt as its
    # new history; result is that same tail with the frame prepended.
    assert kept == result[1:]


def test_compaction_keeps_a_tool_pair_intact_when_it_survives_the_cut(tmp_path):
    """A budget that would reduce everything to `set() == set()` (nothing
    survives the cut, so a tool call/result pairing check is vacuously true)
    proves nothing. Here the cut lands before the pair, so it must appear
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


def test_cut_index_role_guard_prevents_orphaning_a_tool_result(tmp_path):
    """Sized so the smallest suffix that fits under target starts right at
    the `tool` message (index 2): a large tool-result payload followed by a
    small user turn. A guard-less "first suffix that fits" implementation
    would cut there, orphaning the result from the assistant message that
    carries its `tool_calls`. The role guard must push the cut to the next
    user boundary (index 3) instead, keeping the pair together in `older`."""
    s = store(tmp_path)
    cid = s.start()
    s.append(cid, big("user", 8000))
    s.append(cid, {"role": "assistant", "content": "",
                   "tool_calls": [{"id": "c1", "name": "quote", "arguments": {}}]})
    s.append(cid, {"role": "tool", "tool_call_id": "c1", "content": "y" * 2000})
    s.append(cid, big("user", 50))
    llm = ScriptedLLM([LLMResponse(text="summary")])
    c = Compactor(llm, s, budget_tokens=1720)

    _context, kept = c.maybe_compact(cid, s.history(cid))

    assert not any(m["role"] == "tool" for m in kept), \
        "guard-less cut would land inside the pair, orphaning the tool result"
    assert kept == [big("user", 50)]


def test_flush_hook_runs_before_summarizing(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    for _ in range(6):
        s.append(cid, big("user", 3000))
        s.append(cid, big("assistant", 100))
    order: list[str] = []
    llm = ScriptedLLM([LLMResponse(text="summary")])
    # budget_tokens=2500: must be large enough that _cut_index finds a real
    # user-boundary suffix under target, or maybe_compact bails at cut==0
    # before ever calling the hook.
    c = Compactor(llm, s, budget_tokens=2500,
                  on_before_compact=lambda msgs: order.append("flush"))
    c.maybe_compact(cid, s.history(cid))
    assert order == ["flush"]


def test_llm_failure_leaves_history_untouched(tmp_path):
    s = store(tmp_path)
    cid = s.start()
    for _ in range(6):
        s.append(cid, big("user", 3000))
    history = s.history(cid)
    # budget_tokens=2200: must force a real cut so the compactor actually
    # calls the (failing) LLM — otherwise this would pass vacuously. The
    # call counter below is the self-guard: if a future change to the
    # reserve/floor ever pushes this back into the cut==0 early return,
    # llm.calls == 0 fails loudly instead of the assertions below passing
    # for the wrong reason.
    llm = FailingLLM()
    c = Compactor(llm, s, budget_tokens=2200)
    context, kept = c.maybe_compact(cid, history)
    assert llm.calls == 1
    assert context == history
    assert kept == history
    assert s.summary(cid) == ("", 0)


def test_summary_read_failure_degrades_without_crashing(capsys):
    """store.summary() is the first store call maybe_compact makes, before
    it even knows whether a previous summary exists. It must degrade the
    same way an LLM failure does rather than propagate."""
    llm = ScriptedLLM([])  # must never be called: fails before any cut is computed
    c = Compactor(llm, SummaryReadFailingStore(), budget_tokens=100)
    history = [big("user", 500)]

    context, kept = c.maybe_compact(1, history)

    assert context == history
    assert kept == history
    assert "compaction skipped" in capsys.readouterr().err


def test_history_with_ids_read_failure_degrades_without_crashing(capsys):
    """The second guarded read (fetching turn_ids for the alignment check)
    must degrade the same way — before the summarizing LLM call, not after."""
    history = [big("user", 3000) for _ in range(6)]
    llm = ScriptedLLM([])  # must never be called: the read fails before summarizing
    c = Compactor(llm, HistoryIdsReadFailingStore(), budget_tokens=2200)

    context, kept = c.maybe_compact(1, history)

    assert context == history
    assert kept == history
    assert "compaction skipped" in capsys.readouterr().err


def test_alignment_mismatch_degrades_without_crashing(capsys):
    """Distinct from a store call raising: both reads succeed, but the turn
    count no longer matches `history`. Must degrade the same way, with a
    warning that says something different from the store-failure one."""
    history = [big("user", 3000) for _ in range(6)]
    llm = ScriptedLLM([])  # must never be called: mismatch caught before summarizing
    c = Compactor(llm, MisalignedStore(), budget_tokens=2200)

    context, kept = c.maybe_compact(1, history)

    assert context == history
    assert kept == history
    err = capsys.readouterr().err
    assert "compaction skipped" in err
    assert "drifted" in err  # different wording from the store-failure warning


def test_set_summary_failure_leaves_marker_untouched_and_session_survives(tmp_path, capsys):
    """Reproduces the incident the review traced: turns persist normally,
    compaction triggers, both reads succeed and stay aligned — only the
    final set_summary commit fails (disk fills, or another writer holds the
    lock past the busy timeout). Before the fix this propagated straight out
    of maybe_compact, out of run_turn, and ended a live chat; it must instead
    degrade the same way an LLM failure does: marker untouched, and the
    session stays usable for the next turn."""
    real = store(tmp_path)
    cid = real.start()
    for _ in range(6):
        real.append(cid, big("user", 3000))
    broken = SetSummaryFailingStore(real)
    llm = ScriptedLLM([LLMResponse(text="summary"), LLMResponse(text="summary again")])
    c = Compactor(llm, broken, budget_tokens=2200)
    history = real.history(cid)

    context, kept = c.maybe_compact(cid, history)

    assert context == history
    assert kept == history                      # marker didn't move: history is unchanged
    assert broken.summary_calls == 1
    assert real.summary(cid) == ("", 0)          # marker never advanced

    # the next turn must still work: calling again with the (unchanged)
    # history behaves the same way rather than raising or corrupting state.
    context2, kept2 = c.maybe_compact(cid, kept)
    assert context2 == history
    assert kept2 == history
    assert broken.summary_calls == 2

    err = capsys.readouterr().err
    assert err.count("compaction skipped") == 1  # warned once per instance, not per attempt


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
    # budget_tokens=3_000: see the reserve comment in the test above — 2_000
    # no longer forces a real cut on either round now that a first-ever
    # compaction reserves FIRST_SUMMARY_RESERVE_TOKENS off `target`.
    c1 = Compactor(ScriptedLLM([LLMResponse(text="round 1")]), s, budget_tokens=3_000)
    c1.maybe_compact(cid, s.history(cid))
    _, through1 = s.summary(cid)

    for _ in range(10):
        s.append(cid, big("user", 2000))
        s.append(cid, big("assistant", 2000))
    hist2 = s.history(cid, after_turn_id=through1)
    c2 = Compactor(ScriptedLLM([LLMResponse(text="round 2")]), s, budget_tokens=3_000)
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
    # Pin the "marker lands before a user turn" guarantee directly, rather
    # than leaving it to _cut_index's internal role guard (an implementation
    # detail one call away from this test, and one that vanishes if that
    # guard is ever weakened without a test noticing here).
    assert s.history(cid, after_turn_id=through)[0]["role"] == "user"


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
