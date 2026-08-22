from allpath_trade.agent.memory_tools import register_memory_tools
from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.llm.base import ToolCall
from allpath_trade.memory.observations import ObservationLog
from allpath_trade.memory.search import SessionSearch
from allpath_trade.memory.store import MemoryStore
from allpath_trade.store.conversations import ConversationStore
from allpath_trade.store.db import connect


def test_turns_and_observations_are_searchable(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    convs = ConversationStore(conn)
    cid = convs.start()
    convs.append(cid, {"role": "user", "content": "why did we exit NVDA in March"})
    convs.append(cid, {"role": "assistant", "content": "stop-loss fired at 180"})
    ObservationLog(conn).add("sentinel", "NVDA stop-loss executed", subject="NVDA")

    results = SessionSearch(conn).query("NVDA stop-loss")
    kinds = {r["kind"] for r in results}
    assert "turn" in kinds and "observation" in kinds
    assert any("stop-loss" in r["snippet"] for r in results)


def test_malformed_query_returns_empty(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    assert SessionSearch(conn).query('"unbalanced AND ((') == []


def test_session_search_tool(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    ObservationLog(conn).add("sentinel", "AAPL dip-buy queued", subject="AAPL")
    reg = ToolRegistry()
    register_memory_tools(reg, memory=MemoryStore(tmp_path / "m", conn),
                          search=SessionSearch(conn))
    out = reg.execute(ToolCall(id="x", name="session_search",
                               arguments={"query": "dip-buy"}))
    assert "AAPL" in out


def test_ticker_subject_is_indexed_even_when_absent_from_text(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    ObservationLog(conn).add("sentinel", "stop rule executed", subject="NVDA")
    results = SessionSearch(conn).query("NVDA")
    assert any(r["kind"] == "observation" for r in results)


def test_tool_no_matches(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    reg = ToolRegistry()
    register_memory_tools(reg, memory=MemoryStore(tmp_path / "m", conn),
                          search=SessionSearch(conn))
    out = reg.execute(ToolCall(id="x", name="session_search",
                               arguments={"query": "zzz"}))
    assert "no matches" in out


def test_shadow_turn_text_never_leaks_into_paper_search_results(tmp_path):
    # shadow-dual-active T4 CRITICAL carry -- the exact T1 review leak
    # probe: a shadow chat turn must never surface in paper's
    # session_search results (that would put shadow data straight into the
    # paper agent's context through a tool call), and vice versa.
    conn = connect(tmp_path / "db.sqlite")
    paper_convs = ConversationStore(conn, account="paper")
    shadow_convs = ConversationStore(conn, account="shadow")
    paper_cid = paper_convs.start()
    shadow_cid = shadow_convs.start()
    paper_convs.append(paper_cid, {"role": "user",
                                   "content": "paper secret: exit NVDA at 180"})
    shadow_convs.append(shadow_cid, {"role": "user",
                                     "content": "shadow secret: exit NVDA at 180"})

    paper_results = SessionSearch(conn, account="paper").query("NVDA")
    shadow_results = SessionSearch(conn, account="shadow").query("NVDA")

    paper_snippets = " ".join(r["snippet"] for r in paper_results)
    shadow_snippets = " ".join(r["snippet"] for r in shadow_results)
    assert "shadow secret" not in paper_snippets
    assert "paper secret" not in shadow_snippets
    assert "paper secret" in paper_snippets
    assert "shadow secret" in shadow_snippets


def test_shadow_observation_never_leaks_into_paper_search_results(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    ObservationLog(conn, account="paper").add(
        "sentinel", "paper-only stop-loss executed", subject="NVDA")
    ObservationLog(conn, account="shadow").add(
        "sentinel", "shadow-only stop-loss executed", subject="NVDA")

    paper_results = SessionSearch(conn, account="paper").query("NVDA")
    shadow_results = SessionSearch(conn, account="shadow").query("NVDA")

    assert any("paper-only" in r["snippet"] for r in paper_results)
    assert not any("shadow-only" in r["snippet"] for r in paper_results)
    assert any("shadow-only" in r["snippet"] for r in shadow_results)
    assert not any("paper-only" in r["snippet"] for r in shadow_results)


def test_session_search_rejects_invalid_account(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    try:
        SessionSearch(conn, account="not-a-real-account")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an invalid account")
