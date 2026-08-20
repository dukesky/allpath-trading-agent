import sqlite3

import pytest

from allpath_trade.agent.tools import fence_external
from allpath_trade.memory.search import SessionSearch
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


def test_start_defaults_to_chat_kind(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    s = ConversationStore(conn)
    cid = s.start()
    row = conn.execute("SELECT kind FROM conversations WHERE id = ?", (cid,)).fetchone()
    assert row["kind"] == "chat"


def test_latest_filters_by_kind(tmp_path):
    # Phase 6: reflection sessions get their own conversation kind so the
    # web chat's `latest()` call never resumes a reflection transcript.
    s = make(tmp_path)
    chat_id = s.start(kind="chat")
    reflection_id = s.start(kind="reflection")
    assert s.latest(kind="chat") == chat_id
    assert s.latest(kind="reflection") == reflection_id
    assert s.latest() == chat_id  # default kind is "chat"


def test_latest_kind_with_no_matching_rows_is_none(tmp_path):
    s = make(tmp_path)
    s.start(kind="chat")
    assert s.latest(kind="reflection") is None


def test_legacy_conversations_row_defaults_kind_chat_after_migration(tmp_path):
    # Simulate a pre-Phase-6 database: conversations table exists but has no
    # `kind` column. `connect()`'s CREATE TABLE IF NOT EXISTS won't touch an
    # existing table, so the ALTER TABLE migration must add the column (and
    # backfill existing rows) for legacy data to keep working.
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " started_ts TEXT NOT NULL, title TEXT NOT NULL DEFAULT '')")
    raw.execute("INSERT INTO conversations (started_ts) VALUES ('2020-01-01T00:00:00+00:00')")
    raw.commit()
    raw.close()

    conn = connect(path)
    row = conn.execute("SELECT kind FROM conversations").fetchone()
    assert row["kind"] == "chat"


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


def test_turns_since_respects_limit_and_stays_oldest_first(tmp_path):
    # Finding 3: without a SQL-level LIMIT, a first run after upgrade (or
    # after any gap) loads and JSON-parses the ENTIRE turn history in one
    # shot while holding the connection -- a stall on the live chat thread,
    # not just extra work. `limit` bounds the fetch to the OLDEST
    # unconsumed turns (still ORDER BY id), so callers can page forward
    # from wherever the last page actually stopped.
    s = make(tmp_path)
    cid = s.start()
    for i in range(5):
        s.append(cid, {"role": "user", "content": f"turn {i}"})

    first_page = s.turns_since(0, limit=3)
    assert [m["content"] for _tid, _kind, m in first_page] == ["turn 0", "turn 1", "turn 2"]

    last_id = first_page[-1][0]
    second_page = s.turns_since(last_id, limit=3)
    assert [m["content"] for _tid, _kind, m in second_page] == ["turn 3", "turn 4"]

    # limit=None (the default) keeps the old unbounded behavior
    assert len(s.turns_since(0)) == 5


def test_turns_since_carries_the_owning_conversations_kind(tmp_path):
    # Finding F3: turns_since now returns (id, kind, message) instead of
    # (id, message) -- the consolidator needs the OWNING conversation's kind
    # (chat vs. reflection) to attribute each line correctly instead of
    # hardcoding "[chat]" for everything it reads (see memory/consolidate.py
    # Consolidator._turn_lines).
    s = make(tmp_path)
    chat_cid = s.start()  # kind="chat" default
    reflection_cid = s.start(kind="reflection")
    s.append(chat_cid, {"role": "user", "content": "chat turn"})
    s.append(reflection_cid, {"role": "assistant", "content": "reflection turn"})

    turns = s.turns_since(0)

    kinds_by_content = {m["content"]: kind for _tid, kind, m in turns}
    assert kinds_by_content == {"chat turn": "chat", "reflection turn": "reflection"}


def test_a_system_note_is_indexed_by_its_readable_display_text(tmp_path):
    # ChatService.note_resolution stores `content` as the fence_external-
    # wrapped text (what the model sees) and `display` as the plain,
    # human-readable summary (what the template shows). Indexing `content`
    # would surface the FENCE_NOTICE wrapper boilerplate in session search
    # instead of the actual sentence -- this locks in that the search index
    # follows `display` when a message carries one.
    conn = connect(tmp_path / "db.sqlite")
    store = ConversationStore(conn)
    cid = store.start()
    line = "You resolved #12. Result: order submitted"
    store.append(cid, {"role": "user", "content": fence_external(line),
                       "kind": "system_note", "display": line})

    results = SessionSearch(conn).query("order submitted")
    assert any(r["kind"] == "turn" for r in results)
    row = conn.execute(
        "SELECT content FROM search_index WHERE kind = 'turn'").fetchone()
    assert row["content"] == line
    assert "external-content" not in row["content"]


# --- shadow-dual-active T1: account scoping -------------------------------

def test_two_account_interleave_isolated(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    paper = ConversationStore(conn)
    shadow = ConversationStore(conn, account="shadow")

    pcid = paper.start()
    scid = shadow.start()
    paper.append(pcid, {"role": "user", "content": "paper turn"})
    shadow.append(scid, {"role": "user", "content": "shadow turn"})

    assert [m["content"] for m in paper.history(pcid)] == ["paper turn"]
    assert [m["content"] for m in shadow.history(scid)] == ["shadow turn"]
    # A conversation started under the other account is invisible to
    # history/latest -- id collisions across accounts never leak turns.
    assert paper.history(scid) == []
    assert shadow.history(pcid) == []
    assert paper.latest() == pcid
    assert shadow.latest() == scid


def test_append_to_foreign_account_conversation_raises(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    paper = ConversationStore(conn)
    shadow = ConversationStore(conn, account="shadow")
    scid = shadow.start()
    with pytest.raises(ValueError):
        paper.append(scid, {"role": "user", "content": "should not land"})
    assert shadow.history(scid) == []


def test_turns_since_scoped_to_account(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    paper = ConversationStore(conn)
    shadow = ConversationStore(conn, account="shadow")
    pcid = paper.start()
    scid = shadow.start()
    paper.append(pcid, {"role": "user", "content": "paper turn"})
    shadow.append(scid, {"role": "user", "content": "shadow turn"})

    paper_turns = paper.turns_since(0)
    shadow_turns = shadow.turns_since(0)
    assert [m["content"] for _tid, _kind, m in paper_turns] == ["paper turn"]
    assert [m["content"] for _tid, _kind, m in shadow_turns] == ["shadow turn"]


def test_summary_scoped_to_account(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    paper = ConversationStore(conn)
    shadow = ConversationStore(conn, account="shadow")
    pcid = paper.start()
    scid = shadow.start()
    paper.set_summary(pcid, "paper summary", 1)

    assert paper.summary(pcid) == ("paper summary", 1)
    # shadow's instance can't read/overwrite paper's summary via its id.
    assert shadow.summary(pcid) == ("", 0)
    shadow.set_summary(pcid, "should not apply", 5)
    assert paper.summary(pcid) == ("paper summary", 1)
    assert shadow.summary(scid) == ("", 0)


def test_legacy_conversations_row_defaults_account_paper_after_migration(tmp_path):
    # Simulate a pre-shadow-dual-active database: `conversations` exists
    # without an `account` column.
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " started_ts TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',"
        " kind TEXT NOT NULL DEFAULT 'chat', summary TEXT NOT NULL DEFAULT '',"
        " summarized_through INTEGER NOT NULL DEFAULT 0)")
    raw.execute("INSERT INTO conversations (started_ts) VALUES ('2020-01-01T00:00:00+00:00')")
    raw.commit()
    raw.close()

    conn = connect(path)
    row = conn.execute("SELECT account FROM conversations").fetchone()
    assert row["account"] == "paper"

    store = ConversationStore(conn)
    assert store.latest() is not None
