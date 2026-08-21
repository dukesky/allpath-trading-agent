from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL DEFAULT 'paper',  -- shadow-dual-active T1: which pipeline
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

-- shadow-dual-active T2 (carried from T1 review): `account` is a plain
-- column here, not part of a constraint SQLite can't ALTER in place (unlike
-- `reports`'s UNIQUE or `rule_states`'s PRIMARY KEY) -- version numbers are
-- never required to be unique across rows, so a legacy DB just gets the
-- column ALTERed + backfilled 'paper' below, no table rebuild needed.
CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL DEFAULT 'paper',
    strategy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    ts TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL
);

-- shadow-dual-active T1: PK includes `account` so the same (strategy_id,
-- rule_id) can be independently armed/triggered per account -- paper and
-- shadow strategy dirs can both define a strategy called e.g. "growth"
-- (Task 2: strategies/{account}/) with rules that must not share state.
-- Legacy on-disk DBs still have the old 2-column PK; `_rebuild_rule_states`
-- below rebuilds them into this shape (SQLite can't ALTER a PRIMARY KEY).
CREATE TABLE IF NOT EXISTS rule_states (
    account TEXT NOT NULL DEFAULT 'paper',
    strategy_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_ts TEXT NOT NULL,
    PRIMARY KEY (account, strategy_id, rule_id)
);

CREATE TABLE IF NOT EXISTS pending_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL DEFAULT 'paper',  -- shadow-dual-active T1
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
    execution_result TEXT,
    kind TEXT NOT NULL DEFAULT 'order'  -- 'order' | 'strategy_revision' (Phase 6)
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL DEFAULT 'paper',  -- shadow-dual-active T1
    started_ts TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'chat'  -- 'chat' | 'reflection' (Phase 6)
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

-- Daily reflection reports (Phase 6). One row per (account, ET trading day);
-- the UNIQUE(account, date) constraint is also how the reflection scheduler
-- stays idempotent across process restarts -- see ReportStore. Was
-- UNIQUE(date) alone before shadow-dual-active T1 -- `_rebuild_reports`
-- below migrates a legacy on-disk table into this shape (SQLite can't ALTER
-- a UNIQUE constraint in place); a fresh install gets this shape directly
-- via CREATE TABLE IF NOT EXISTS.
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL DEFAULT 'paper',
    date TEXT NOT NULL,
    body TEXT NOT NULL,
    summary TEXT NOT NULL,
    conversation_id INTEGER,
    model TEXT NOT NULL DEFAULT '',
    tokens_used INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    created_at TEXT NOT NULL,
    UNIQUE (account, date)
);

-- LLM usage + cost-estimate panel (store/llm_usage.py). One row per
-- completed LLM call, written from the single `_RecordingClient` choke
-- point in llm/factory.py's `build_llm` -- see that module's docstring.
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    tier TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    purpose TEXT NOT NULL DEFAULT ''
);

-- shadow-dual-active T3: ShadowLedger's own tables (broker/shadow.py).
-- Deliberately have NO `account` column, unlike every table above --
-- these tables ARE the shadow account's ledger (there is one shadow
-- account, so there is nothing to dimension by), not a shared table a
-- second account's rows could leak into. New tables land straight in
-- SCHEMA (not _MIGRATIONS) per the house convention: CREATE TABLE IF NOT
-- EXISTS already covers both a fresh install and an existing on-disk DB
-- that predates this table, with no ALTER step needed.
CREATE TABLE IF NOT EXISTS shadow_positions (
    ticker TEXT PRIMARY KEY,
    qty TEXT NOT NULL,
    avg_cost TEXT NOT NULL,
    last_price TEXT,
    last_price_ts TEXT,
    updated_ts TEXT NOT NULL
);

-- Single-row table (CHECK id=1) holding shadow cash. INSERT ... ON
-- CONFLICT(id) DO UPDATE is how ShadowLedger both creates and updates it,
-- so there is no separate "initialize the ledger" step.
CREATE TABLE IF NOT EXISTS shadow_cash (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash TEXT NOT NULL,
    updated_ts TEXT NOT NULL
);

-- Every submit_order call appends a row here, filled or rejected alike
-- (rejections carry their reason in `note`, no fill_price/filled_at) --
-- this table is the shadow account's full audit trail. `side='adjust'`
-- rows (written by ShadowLedger's ledger-mutation helpers: set_position,
-- set_cash, remove_position, record_fill -- Task 6's applier, never
-- reachable from the agent directly) are also appended here for the same
-- audit-trail reason, but are excluded from get_order/get_orders (Broker
-- interface methods, side IN ('buy','sell') only) since they aren't
-- orders the user submitted through the trading flow.
CREATE TABLE IF NOT EXISTS shadow_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT,
    notional TEXT,
    status TEXT NOT NULL,
    fill_price TEXT,
    filled_at TEXT,
    note TEXT NOT NULL DEFAULT ''
);

-- One row per ET calendar day, upserted both by get_account (every read,
-- best-effort) and (eventually) a nightly job -- see ShadowLedger.
CREATE TABLE IF NOT EXISTS shadow_equity_daily (
    date TEXT PRIMARY KEY,
    equity TEXT NOT NULL,
    cash TEXT NOT NULL
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
    "ALTER TABLE conversations ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'",
    "ALTER TABLE pending_reviews ADD COLUMN kind TEXT NOT NULL DEFAULT 'order'",
    "ALTER TABLE trades ADD COLUMN filled_at TEXT",
    # Approve-by-link (see ReviewQueue.add/consume_token): only a sha256
    # hash of the single-use token is ever persisted, never the plaintext.
    # NULL on both columns means "no link was ever issued for this row" --
    # true for every row inserted before this migration, and the only state
    # that must always fail closed in ReviewQueue._token_ok.
    "ALTER TABLE pending_reviews ADD COLUMN approval_token_hash TEXT",
    "ALTER TABLE pending_reviews ADD COLUMN token_expires_ts TEXT",
    # shadow-dual-active T1: account dimension. Plain ALTER + DEFAULT is
    # enough for these four -- legacy rows (all pre-dating the shadow
    # account) backfill 'paper' for free via the column default, exactly
    # like the `kind` backfills above. `reports` and `rule_states` ALSO
    # need a constraint that SQLite can't ALTER in place (a UNIQUE key and
    # a PRIMARY KEY respectively) -- those columns are added here too, but
    # the constraint fix is a separate table-rebuild step; see
    # `_rebuild_reports`/`_rebuild_rule_states` below.
    "ALTER TABLE trades ADD COLUMN account TEXT NOT NULL DEFAULT 'paper'",
    "ALTER TABLE pending_reviews ADD COLUMN account TEXT NOT NULL DEFAULT 'paper'",
    "ALTER TABLE conversations ADD COLUMN account TEXT NOT NULL DEFAULT 'paper'",
    "ALTER TABLE reports ADD COLUMN account TEXT NOT NULL DEFAULT 'paper'",
    "ALTER TABLE rule_states ADD COLUMN account TEXT NOT NULL DEFAULT 'paper'",
    # shadow-dual-active T2 (carried from T1 review): strategy_versions was
    # keyed on strategy_id alone -- after the strategies/{account}/
    # directory split (this task), a same-id strategy in each account would
    # otherwise share one version history, the same leak class as the
    # rule_states PK T1 fixed. No table rebuild needed here (see the SCHEMA
    # comment above `strategy_versions`): plain ALTER + DEFAULT backfills
    # every legacy row 'paper', exactly like the four T1 columns above.
    "ALTER TABLE strategy_versions ADD COLUMN account TEXT NOT NULL DEFAULT 'paper'",
]

# LOUD REMINDER for whoever adds the next account-scoped (or any other)
# column to `reports` or `rule_states`: both tables have been rebuilt at
# least once already (`_rebuild_reports`/`_rebuild_rule_states` below)
# because SQLite cannot ALTER a UNIQUE/PRIMARY KEY constraint in place. Each
# rebuild function hand-writes its own full DDL (CREATE TABLE ..._v2 with
# every column spelled out) and its own explicit column list for the
# INSERT ... SELECT copy. Adding a column ONLY to `_MIGRATIONS` (the ALTER
# list above) is not enough for these two tables on a fresh install that
# still needs a rebuild for some OTHER, still-unmigrated reason -- and
# adding a column only to SCHEMA's CREATE TABLE IF NOT EXISTS is not enough
# for an existing on-disk DB that goes through the rebuild path. Any new
# column on `reports` or `rule_states` must be added in ALL THREE places:
# SCHEMA's CREATE TABLE, the matching `_rebuild_*` function's CREATE TABLE
# ..._v2 + INSERT column list, AND (if it needs backfilling on a DB that
# predates the rebuild too) a plain ALTER in `_MIGRATIONS` above, run before
# the rebuild so the rebuild's SELECT can read it. Forgetting one of the
# three is silent: the column will exist on SOME on-disk databases and not
# others, depending on exactly which migration path a given install took.


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


def _table_sql(conn: LockedConnection, table: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    return row["sql"] if row is not None else None


def _rebuild_reports(conn: LockedConnection) -> None:
    """Table-rebuild for `reports`: SQLite has no `ALTER TABLE ... ADD
    CONSTRAINT`, so the old UNIQUE(date) key can't become UNIQUE(account,
    date) in place. Guarded by inspecting the table's own stored CREATE
    TABLE text (`sqlite_master.sql`) for the new constraint, rather than a
    sentinel row or a PRAGMA count, because that text is unambiguous and
    can't drift out of sync with what's actually enforced -- if it's
    there, the rebuild already happened (or this is a fresh install, where
    `CREATE TABLE IF NOT EXISTS` above created the new shape directly and
    this is a genuine no-op). Idempotent across restarts: run once on a
    legacy DB, forever a no-op after.

    Must run AFTER the `account` column ALTER above (also idempotent) --
    the copy step below reads that column, so on a legacy DB it needs to
    already exist (backfilled 'paper' by the ALTER's DEFAULT)."""
    sql = _table_sql(conn, "reports")
    if sql is None or "UNIQUE (account, date)" in sql:
        return
    with conn.transaction():
        conn.execute(
            "CREATE TABLE reports_v2 ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " account TEXT NOT NULL DEFAULT 'paper',"
            " date TEXT NOT NULL,"
            " body TEXT NOT NULL,"
            " summary TEXT NOT NULL,"
            " conversation_id INTEGER,"
            " model TEXT NOT NULL DEFAULT '',"
            " tokens_used INTEGER NOT NULL DEFAULT 0,"
            " status TEXT NOT NULL DEFAULT 'ok',"
            " created_at TEXT NOT NULL,"
            " UNIQUE (account, date))")
        conn.execute(
            "INSERT INTO reports_v2 (id, account, date, body, summary,"
            " conversation_id, model, tokens_used, status, created_at)"
            " SELECT id, account, date, body, summary, conversation_id,"
            " model, tokens_used, status, created_at FROM reports")
        conn.execute("DROP TABLE reports")
        conn.execute("ALTER TABLE reports_v2 RENAME TO reports")


def _rebuild_rule_states(conn: LockedConnection) -> None:
    """Table-rebuild for `rule_states`: the PRIMARY KEY must become
    (account, strategy_id, rule_id) so the same strategy/rule id armed in
    both `paper` and `shadow` (Task 2: each account gets its own strategy
    directory, so ids CAN collide) tracks independent state instead of one
    account's ON CONFLICT upsert silently overwriting the other's row.
    SQLite can't ALTER a PRIMARY KEY in place, hence the rebuild. Same
    guard shape as `_rebuild_reports` -- inspect the stored CREATE TABLE
    text, no-op if already the new shape (rebuilt already, or a fresh
    install that got it directly from SCHEMA)."""
    sql = _table_sql(conn, "rule_states")
    if sql is None or "PRIMARY KEY (account, strategy_id, rule_id)" in sql:
        return
    with conn.transaction():
        conn.execute(
            "CREATE TABLE rule_states_v2 ("
            " account TEXT NOT NULL DEFAULT 'paper',"
            " strategy_id TEXT NOT NULL,"
            " rule_id TEXT NOT NULL,"
            " state TEXT NOT NULL,"
            " updated_ts TEXT NOT NULL,"
            " PRIMARY KEY (account, strategy_id, rule_id))")
        conn.execute(
            "INSERT INTO rule_states_v2 (account, strategy_id, rule_id,"
            " state, updated_ts)"
            " SELECT account, strategy_id, rule_id, state, updated_ts"
            " FROM rule_states")
        conn.execute("DROP TABLE rule_states")
        conn.execute("ALTER TABLE rule_states_v2 RENAME TO rule_states")


def _migrate(conn: LockedConnection) -> None:
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    # Constraint rebuilds run after the plain ALTERs above so the `account`
    # column they depend on is already backfilled on a legacy DB.
    _rebuild_reports(conn)
    _rebuild_rule_states(conn)


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
