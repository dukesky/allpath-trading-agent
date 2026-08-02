from allpath_trade.store.conversations import ConversationStore
from allpath_trade.store.db import connect


def make(tmp_path):
    return ConversationStore(connect(tmp_path / "db.sqlite"))


def test_start_and_latest(tmp_path):
    s = make(tmp_path)
    assert s.latest() is None
    c1 = s.start()
    c2 = s.start()
    assert s.latest() == c2 and c2 > c1


def test_append_and_history_roundtrip(tmp_path):
    s = make(tmp_path)
    cid = s.start()
    s.append(cid, {"role": "user", "content": "hi"})
    s.append(cid, {"role": "assistant", "content": None,
                   "tool_calls": [{"id": "t1", "name": "x", "arguments": {"a": 1}}]})
    hist = s.history(cid)
    assert hist[0] == {"role": "user", "content": "hi"}
    assert hist[1]["tool_calls"][0]["arguments"] == {"a": 1}


def test_agent_analysis_column_migrated(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pending_reviews)")}
    assert "agent_analysis" in cols
    connect(tmp_path / "db.sqlite")  # idempotent second run


def test_summary_columns_migrated(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}
    assert {"summary", "summarized_through"} <= cols
    connect(tmp_path / "db.sqlite")  # idempotent second run
