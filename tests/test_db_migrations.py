"""Regression tests for allpath_trade/store/db.py's migration machinery.

shadow-dual-active T2 carries these forward from the T1 review: rebuild
idempotence across two separate `connect()` calls (not just one process's
in-memory state), and a half-migrated database (some but not all of the
account-dimension changes already applied) -- both were hand-probed during
T1's review rather than pinned as tests."""

import sqlite3

from allpath_trade.store.db import SCHEMA, connect


def test_migration_is_idempotent_across_two_connects(tmp_path):
    # A legacy (pre-shadow-dual-active) `reports`/`rule_states` shape, each
    # seeded with one row, migrated by a first `connect()` -- then a wholly
    # separate second `connect()` on the same on-disk file (a fresh process
    # restart, not the same LockedConnection) must be a strict no-op: same
    # row counts, same data, no `_v2` table left over from a re-fired
    # rebuild.
    path = tmp_path / "t.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " date TEXT NOT NULL UNIQUE, body TEXT NOT NULL, summary TEXT NOT NULL,"
        " conversation_id INTEGER, model TEXT NOT NULL DEFAULT '',"
        " tokens_used INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ok',"
        " created_at TEXT NOT NULL)")
    raw.execute(
        "INSERT INTO reports (date, body, summary, created_at)"
        " VALUES ('2026-08-01', 'body', 'summary', '2026-08-01T00:00:00+00:00')")
    raw.execute(
        "CREATE TABLE rule_states (strategy_id TEXT NOT NULL, rule_id TEXT NOT NULL,"
        " state TEXT NOT NULL, updated_ts TEXT NOT NULL,"
        " PRIMARY KEY (strategy_id, rule_id))")
    raw.execute(
        "INSERT INTO rule_states (strategy_id, rule_id, state, updated_ts)"
        " VALUES ('a', 'r1', 'triggered', '2026-08-01T00:00:00+00:00')")
    raw.commit()
    raw.close()

    conn1 = connect(path)
    reports_1 = [tuple(r) for r in conn1.execute(
        "SELECT account, date, body FROM reports ORDER BY id")]
    rule_states_1 = [tuple(r) for r in conn1.execute(
        "SELECT account, strategy_id, rule_id, state FROM rule_states"
        " ORDER BY strategy_id")]
    reports_sql_1 = conn1.execute(
        "SELECT sql FROM sqlite_master WHERE name='reports'").fetchone()["sql"]
    rule_states_sql_1 = conn1.execute(
        "SELECT sql FROM sqlite_master WHERE name='rule_states'").fetchone()["sql"]
    conn1.close()

    # Second, independent connect() on the same file.
    conn2 = connect(path)
    reports_2 = [tuple(r) for r in conn2.execute(
        "SELECT account, date, body FROM reports ORDER BY id")]
    rule_states_2 = [tuple(r) for r in conn2.execute(
        "SELECT account, strategy_id, rule_id, state FROM rule_states"
        " ORDER BY strategy_id")]

    assert reports_1 == reports_2 == [("paper", "2026-08-01", "body")]
    assert rule_states_1 == rule_states_2 == [("paper", "a", "r1", "triggered")]
    assert conn2.execute(
        "SELECT sql FROM sqlite_master WHERE name='reports'"
    ).fetchone()["sql"] == reports_sql_1
    assert conn2.execute(
        "SELECT sql FROM sqlite_master WHERE name='rule_states'"
    ).fetchone()["sql"] == rule_states_sql_1
    names = {r["name"] for r in conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "reports_v2" not in names
    assert "rule_states_v2" not in names


def test_half_migrated_db_mixed_plain_alters_all_complete(tmp_path):
    # Simulate a process that died partway through a previous `_migrate()`
    # run: `trades` already has its `account` column (and a row backfilled
    # by it), but `pending_reviews`/`conversations` are still the old
    # pre-shadow-dual-active shape with no `account` column at all. Each
    # `_MIGRATIONS` statement is applied independently (a per-statement
    # try/except), so an already-applied ALTER failing on `trades` must not
    # stop the still-pending ones on the other tables.
    path = tmp_path / "t.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL, qty TEXT,"
        " notional TEXT, status TEXT NOT NULL, reason TEXT NOT NULL,"
        " strategy_id TEXT, risk_reasons TEXT NOT NULL DEFAULT '[]',"
        " broker_order_id TEXT)")
    raw.execute("ALTER TABLE trades ADD COLUMN account TEXT NOT NULL DEFAULT 'paper'")
    raw.execute(
        "INSERT INTO trades (ts, ticker, side, status, reason)"
        " VALUES ('2026-08-01T00:00:00+00:00', 'AAPL', 'buy', 'filled', 'test')")
    raw.execute(
        "CREATE TABLE pending_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts TEXT NOT NULL, strategy_id TEXT NOT NULL, rule_id TEXT NOT NULL,"
        " ticker TEXT NOT NULL, rule_type TEXT NOT NULL, condition TEXT NOT NULL,"
        " action TEXT NOT NULL, snapshot TEXT NOT NULL, intent TEXT,"
        " status TEXT NOT NULL DEFAULT 'pending', resolved_ts TEXT,"
        " resolution_note TEXT, execution_result TEXT,"
        " kind TEXT NOT NULL DEFAULT 'order')")
    raw.execute(
        "CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " started_ts TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',"
        " kind TEXT NOT NULL DEFAULT 'chat')")
    raw.commit()
    raw.close()

    conn = connect(path)  # must complete without error

    trades_cols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)")}
    assert "account" in trades_cols
    row = conn.execute("SELECT account FROM trades").fetchone()
    assert row["account"] == "paper"

    for table in ("pending_reviews", "conversations"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert "account" in cols, table


def test_half_migrated_db_t1_complete_t2_strategy_versions_not_yet(tmp_path):
    # The real-world shape this task's own DB change lands on top of: a
    # live database that already went through T1 (reports/rule_states
    # already rebuilt to the new (account, ...) shape) but predates T2's
    # `strategy_versions.account` column. Migration must add + backfill it
    # without disturbing the already-migrated tables, and the store's
    # normal read/write path must work immediately afterward.
    path = tmp_path / "t.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(SCHEMA)  # gives reports/rule_states the NEW shape directly
    # SCHEMA now creates strategy_versions with `account` already -- drop and
    # recreate it in the OLD (pre-T2) shape to simulate a DB that went
    # through T1 but not yet T2.
    raw.execute("DROP TABLE strategy_versions")
    raw.execute(
        "CREATE TABLE strategy_versions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " strategy_id TEXT NOT NULL, version INTEGER NOT NULL, ts TEXT NOT NULL,"
        " reason TEXT NOT NULL DEFAULT '', content TEXT NOT NULL)")
    raw.execute(
        "INSERT INTO strategy_versions (strategy_id, version, ts, reason, content)"
        " VALUES ('a', 1, '2026-08-01T00:00:00+00:00', 'legacy', 'content: v1')")
    raw.commit()
    raw.close()

    conn = connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(strategy_versions)")}
    assert "account" in cols
    row = conn.execute(
        "SELECT account FROM strategy_versions WHERE strategy_id='a'").fetchone()
    assert row["account"] == "paper"

    # reports/rule_states (already new-shape) must be untouched by this run.
    assert "UNIQUE (account, date)" in conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='reports'").fetchone()["sql"]

    from allpath_trade.strategy.model import RuleState
    from allpath_trade.strategy.store import StrategyStore

    store = StrategyStore(tmp_path, conn)  # no strategy YAML needed for this check
    store.set_rule_state("a", "r1", RuleState.TRIGGERED)  # exercises the live tables
    rows = store.versions("a")
    assert [r["version"] for r in rows] == [1]
    assert rows[0]["account"] == "paper"


# -- shadow-dual-active T4 CRITICAL carry: search_index (FTS5) + observations
# account-column migration --


def test_search_index_rebuild_backfills_paper_and_preserves_rows(tmp_path):
    """A populated legacy `search_index` (no `account` column) migrated by
    `connect()` must keep every row, backfilled `account='paper'`, and stay
    fully searchable afterward -- the exact "test on a populated legacy
    index" the plan calls for."""
    path = tmp_path / "t.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE VIRTUAL TABLE search_index USING fts5("
        "kind UNINDEXED, ref_id UNINDEXED, subject, content)")
    raw.execute(
        "INSERT INTO search_index (kind, ref_id, subject, content)"
        " VALUES ('observation', '1', 'sentinel', 'AAPL stop-loss executed')")
    raw.execute(
        "INSERT INTO search_index (kind, ref_id, subject, content)"
        " VALUES ('turn', '1', 'user', 'why did we exit AAPL')")
    raw.commit()
    raw.close()

    conn = connect(path)
    rows = [dict(r) for r in conn.execute(
        "SELECT kind, ref_id, subject, content, account FROM search_index"
        " ORDER BY ref_id")]
    assert len(rows) == 2
    assert all(r["account"] == "paper" for r in rows)
    assert {r["content"] for r in rows} == {
        "AAPL stop-loss executed", "why did we exit AAPL"}

    # Still searchable, scoped by the backfilled account.
    hits = list(conn.execute(
        "SELECT ref_id FROM search_index WHERE search_index MATCH 'AAPL'"
        " AND account = 'paper'"))
    assert len(hits) == 2


def test_search_index_rebuild_is_idempotent_across_two_connects(tmp_path):
    path = tmp_path / "t.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE VIRTUAL TABLE search_index USING fts5("
        "kind UNINDEXED, ref_id UNINDEXED, subject, content)")
    raw.execute(
        "INSERT INTO search_index (kind, ref_id, subject, content)"
        " VALUES ('observation', '1', 'sentinel', 'legacy row')")
    raw.commit()
    raw.close()

    conn1 = connect(path)
    rows1 = [tuple(r) for r in conn1.execute(
        "SELECT kind, ref_id, subject, content, account FROM search_index")]
    conn1.close()

    conn2 = connect(path)  # simulates a process restart: fresh connect()
    rows2 = [tuple(r) for r in conn2.execute(
        "SELECT kind, ref_id, subject, content, account FROM search_index")]

    assert rows1 == rows2 == [("observation", "1", "sentinel", "legacy row", "paper")]
    # No leftover _v2 table from a re-fired rebuild.
    leftover = conn2.execute(
        "SELECT name FROM sqlite_master WHERE name='search_index_v2'").fetchone()
    assert leftover is None


def test_observations_account_column_backfills_paper_on_legacy_table(tmp_path):
    path = tmp_path / "t.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE observations (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts TEXT NOT NULL, source TEXT NOT NULL, subject TEXT, text TEXT NOT NULL)")
    raw.execute(
        "INSERT INTO observations (ts, source, subject, text)"
        " VALUES ('2026-08-01T00:00:00+00:00', 'chat', NULL, 'legacy note')")
    raw.commit()
    raw.close()

    conn = connect(path)
    row = conn.execute("SELECT account, text FROM observations").fetchone()
    assert row["account"] == "paper"
    assert row["text"] == "legacy note"
