from tradewind.agent.memory_tools import register_memory_tools
from tradewind.agent.tools import ToolRegistry
from tradewind.llm.base import ToolCall
from tradewind.memory.observations import ObservationLog
from tradewind.memory.search import SessionSearch
from tradewind.memory.store import MemoryStore
from tradewind.store.conversations import ConversationStore
from tradewind.store.db import connect


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


def test_tool_no_matches(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    reg = ToolRegistry()
    register_memory_tools(reg, memory=MemoryStore(tmp_path / "m", conn),
                          search=SessionSearch(conn))
    out = reg.execute(ToolCall(id="x", name="session_search",
                               arguments={"query": "zzz"}))
    assert "no matches" in out
