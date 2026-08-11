from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                -- UTC ISO-8601
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT,
    notional TEXT,
    status TEXT NOT NULL,            -- rejected | error | submitted | filled | ...
    reason TEXT NOT NULL,            -- human-readable intent reason
    strategy_id TEXT,
    risk_reasons TEXT NOT NULL DEFAULT '[]',  -- JSON list
    broker_order_id TEXT
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    ts TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_states (
    strategy_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_ts TEXT NOT NULL,
    PRIMARY KEY (strategy_id, rule_id)
);

CREATE TABLE IF NOT EXISTS pending_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    condition TEXT NOT NULL,
    action TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    intent TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_ts TEXT,
    resolution_note TEXT,
    execution_result TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    layer TEXT NOT NULL,
    key TEXT,
    action TEXT NOT NULL,
    before TEXT,
    after TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    subject TEXT,
    text TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    kind UNINDEXED, ref_id UNINDEXED, subject, content
);

-- Tiny key-value table for process-level facts that don't belong in any
-- append-only journal (observations) or one-row-per-entity table
-- (rule_states). The sentinel heartbeat is the first tenant: one row,
-- overwritten on every pass, so it stays invisible to consolidation instead
-- of accumulating an hourly row forever. See AppState (store/app_state.py).
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Daily reflection reports (Phase 6). One row per ET trading day; the
-- `date` UNIQUE constraint is also how the reflection scheduler stays
-- idempotent across process restarts -- see ReportStore.
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL,
    summary TEXT NOT NULL,
    conversation_id INTEGER,
    model TEXT NOT NULL DEFAULT '',
    tokens_used INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    created_at TEXT NOT NULL
);
"""


_MIGRATIONS = [
    "ALTER TABLE pending_reviews ADD COLUMN agent_analysis TEXT",
    "ALTER TABLE conversations ADD COLUMN summary TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversations ADD COLUMN summarized_through INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pending_reviews ADD COLUMN source TEXT NOT NULL DEFAULT 'sentinel'",
    "ALTER TABLE pending_reviews ADD COLUMN conversation_id INTEGER",
    "ALTER TABLE pending_reviews ADD COLUMN risk_preview TEXT",
    "ALTER TABLE trades ADD COLUMN filled_qty TEXT",
    "ALTER TABLE trades ADD COLUMN filled_avg_price TEXT",
]


class LockedConnection:
    """Serializes access to one sqlite connection.

    `serve` runs the web app and the sentinel scheduler in a single process,
    so two threads write to this database. Most writes in this codebase are a
    single statement followed by a commit, which per-statement locking
    (`execute`/`commit` each take the lock separately) already covers. A few
    writes span more than one statement — a record plus its FTS index entry —
    and those go through `transaction()` instead, which holds the lock across
    the whole scope so another thread's commit can't land in between. A
    single-user app has nothing to gain from write concurrency beyond that."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()
        self._txn_depth = 0

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value) -> None:
        self._conn.row_factory = value

    def execute(self, sql: str, parameters=()):
        with self._lock:
            cur = self._conn.execute(sql, parameters)
            # Materialize now: the caller may iterate the cursor after another
            # thread has taken the lock and started writing.
            return _Rows(cur)

    def executemany(self, sql: str, seq):
        with self._lock:
            return _Rows(self._conn.executemany(sql, seq))

    def executescript(self, script: str):
        with self._lock:
            return _Rows(self._conn.executescript(script))

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self):
        """Hold the lock across several statements and commit them together.

        Most writes here are one statement plus a commit, which `execute` and
        `commit` already serialize. A few write two rows that must land as a
        unit — a record plus its FTS index entry — and those must not have
        another thread's commit interleaved between them.

        On failure, only the statements issued inside this block are undone.
        `conn.rollback()` would roll back the *whole* shared connection,
        including another thread's write that is sitting uncommitted because
        it released the lock between its `execute()` and `commit()` calls
        (every store does this) — that write would vanish out from under it
        while its caller still believes the following `commit()` succeeded,
        since sqlite3 captures `rowcount`/`lastrowid` at execute time, before
        either thread's commit runs. A `SAVEPOINT`, scoped to this block via
        `ROLLBACK TO`/`RELEASE`, undoes only what this block did. Savepoint
        names are keyed by nesting depth (the lock is an `RLock`, so a
        `transaction()` can legitimately nest on the same thread) so an inner
        rollback can't target an outer savepoint by name collision. Only the
        outermost call commits; a nested call's `RELEASE` on its own
        savepoint is enough."""
        with self._lock:
            depth = self._txn_depth
            self._txn_depth += 1
            savepoint = f"txn_sp_{depth}"
            try:
                self._conn.execute(f"SAVEPOINT {savepoint}")
                try:
                    yield self
                except BaseException:
                    self._conn.execute(f"ROLLBACK TO {savepoint}")
                    self._conn.execute(f"RELEASE {savepoint}")
                    raise
                self._conn.execute(f"RELEASE {savepoint}")
                if depth == 0:
                    self._conn.commit()
            finally:
                self._txn_depth -= 1


class _Rows:
    """A materialized cursor: rows are read eagerly, `lastrowid`/`rowcount`
    are captured, so nothing depends on the cursor staying valid once another
    thread takes the connection lock.

    Eager fetch is safe for every statement this codebase issues through
    `execute`/`executemany`/`executescript`: INSERT, UPDATE, and DDL cursors
    have no result set, and `sqlite3` returns `[]` from `fetchall()` on those
    rather than raising (verified empirically against this project's
    sqlite3/Python version). `lastrowid` and `rowcount` are read off the
    cursor before fetching, and neither value changes as a result of
    fetching, so capture order is not load-bearing here — it is kept
    first-then-fetch anyway as the more obviously correct order.

    `fetchone()`/`fetchall()` share one position like the real cursor: a
    `fetchall()` after some `fetchone()` calls returns only the remaining
    rows. `__iter__` does not — it always walks the full materialized list
    regardless of `_pos`, diverging from `sqlite3.Cursor` (whose iterator and
    `fetchone`/`fetchall` share position). No call site in this codebase
    interleaves iteration with `fetchone`/`fetchall` on the same cursor, so
    this has not mattered in practice; fix `__iter__` to consume from
    `_pos` too if a future caller needs that."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount
        self._rows = cursor.fetchall()
        self._pos = 0

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> list:
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self._rows)


def _migrate(conn: LockedConnection) -> None:
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists


def connect(path: Path | str) -> LockedConnection:
    # check_same_thread=False: APScheduler runs jobs on worker threads, not
    # the thread that built the connection, and `serve` (Phase 5) adds a
    # second writer (the web app). LockedConnection below serializes all
    # access through one lock rather than handing each thread its own
    # connection: every store already takes a `conn` object, single-statement-
    # then-commit is the universal write pattern here, and a single-user app
    # gains nothing from write concurrency. WAL mode plus a busy timeout
    # covers the reader side (readers no longer block on a writer's
    # transaction). The CLI's one-shot commands run in separate processes, so
    # there is no cross-process contention beyond what WAL already handles.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    locked = LockedConnection(conn)
    locked.executescript(SCHEMA)
    _migrate(locked)
    return locked
