from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Self

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
"""


_MIGRATIONS = [
    "ALTER TABLE pending_reviews ADD COLUMN agent_analysis TEXT",
]


class LockedConnection:
    """Serializes access to one sqlite connection.

    `serve` runs the web app and the sentinel scheduler in a single process,
    so two threads write to this database. Every store in the codebase takes
    a connection object and writes with a single statement followed by a
    commit, so one lock around the connection is both sufficient and cheaper
    than threading a connection pool through every constructor. A single-user
    app has nothing to gain from write concurrency."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()

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

    def __enter__(self) -> Self:
        # Mirrors sqlite3.Connection's context manager (commit on clean exit,
        # rollback on exception) rather than raw lock acquisition, so this
        # stays a true drop-in if a future caller writes `with conn:`. The
        # lock is held for the whole block so a multi-statement transaction,
        # should one ever appear, is not interleaved with another thread's.
        self._lock.acquire()
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc_val: BaseException | None, exc_tb: object) -> None:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._lock.release()


class _Rows:
    """A materialized cursor: rows are read eagerly, `lastrowid`/`rowcount`
    are captured, so nothing depends on the cursor staying valid once another
    thread takes the connection lock.

    Eager `fetchall()` is safe for every statement this codebase issues
    through `execute`/`executemany`/`executescript`: INSERT, UPDATE, and DDL
    cursors have no result set, and `sqlite3` returns `[]` from `fetchall()`
    on those rather than raising — it does not raise `ProgrammingError` just
    because there is nothing to fetch (verified empirically against this
    project's sqlite3/Python version; the `except` below guards the one case
    the docs leave undefined, not an observed failure). `lastrowid` and
    `rowcount` are read off the cursor before `fetchall()` runs, and neither
    value changes as a result of fetching, so capture order is not
    load-bearing here — it is kept first-then-fetch anyway as the more
    obviously correct order."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount
        try:
            self._rows = cursor.fetchall()
        except sqlite3.ProgrammingError:
            self._rows = []  # statement returned no result set

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)

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
