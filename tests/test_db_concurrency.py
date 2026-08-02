import sqlite3
import threading

from allpath_trade.store.db import connect


def test_concurrent_writers_do_not_lose_rows(tmp_path):
    conn = connect(tmp_path / "t.db")
    errors: list[Exception] = []

    def writer(tag: str) -> None:
        try:
            for i in range(50):
                conn.execute(
                    "INSERT INTO observations (ts, source, subject, text)"
                    " VALUES ('t', ?, NULL, ?)", (tag, str(i)))
                conn.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"w{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    count = conn.execute("SELECT COUNT(*) AS c FROM observations").fetchone()["c"]
    assert count == 200


def test_wal_mode_is_enabled(tmp_path):
    conn = connect(tmp_path / "t.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


# _Rows wraps every statement type the codebase issues through `execute`
# (INSERT, UPDATE, DDL, executescript, SELECT) with an eager `fetchall()`.
# These pin down that the wrapper is correct for each, not just "happens to
# pass" the two tests above.

def test_insert_lastrowid(tmp_path):
    conn = connect(tmp_path / "t.db")
    cur = conn.execute(
        "INSERT INTO observations (ts, source, subject, text)"
        " VALUES ('t', 's', NULL, 'x')")
    conn.commit()
    assert cur.lastrowid == 1
    cur2 = conn.execute(
        "INSERT INTO observations (ts, source, subject, text)"
        " VALUES ('t', 's', NULL, 'y')")
    conn.commit()
    assert cur2.lastrowid == 2


def test_update_rowcount_reflects_matched_rows(tmp_path):
    conn = connect(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO observations (ts, source, subject, text)"
        " VALUES ('t', 's', NULL, 'x')")
    conn.commit()

    matched = conn.execute("UPDATE observations SET text='z' WHERE id=1")
    conn.commit()
    assert matched.rowcount == 1

    # This is the shape ReviewQueue.approve/reject depend on for their
    # atomic-claim logic: a WHERE clause that matches nothing must report 0,
    # not raise and not silently report success.
    unmatched = conn.execute("UPDATE observations SET text='z' WHERE id=999")
    conn.commit()
    assert unmatched.rowcount == 0


def test_ddl_statement_via_execute_does_not_raise(tmp_path):
    conn = connect(tmp_path / "t.db")
    cur = conn.execute("CREATE TABLE scratch (id INTEGER PRIMARY KEY)")
    assert cur.fetchall() == []


def test_executescript_reopen_is_idempotent(tmp_path):
    # connect() runs the full CREATE-TABLE-IF-NOT-EXISTS schema via
    # executescript on every call, including against a database that
    # already has that schema (the second `connect` in this test, and every
    # process restart in real usage).
    path = tmp_path / "t.db"
    connect(path)
    conn = connect(path)  # must not raise on re-running executescript(SCHEMA)
    conn.execute(
        "INSERT INTO observations (ts, source, subject, text)"
        " VALUES ('t', 's', NULL, 'x')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS c FROM observations").fetchone()["c"] == 1


def test_select_fetchone_and_fetchall_and_iteration(tmp_path):
    conn = connect(tmp_path / "t.db")
    for i in range(3):
        conn.execute(
            "INSERT INTO observations (ts, source, subject, text)"
            " VALUES ('t', 's', NULL, ?)", (str(i),))
        conn.commit()

    rows = conn.execute("SELECT text FROM observations ORDER BY id").fetchall()
    assert [r["text"] for r in rows] == ["0", "1", "2"]

    one = conn.execute("SELECT text FROM observations ORDER BY id").fetchone()
    assert one["text"] == "0"

    none = conn.execute("SELECT text FROM observations WHERE id = -1").fetchone()
    assert none is None

    iterated = [r["text"] for r in conn.execute(
        "SELECT text FROM observations ORDER BY id")]
    assert iterated == ["0", "1", "2"]


def test_transaction_commits_on_success(tmp_path):
    # Read back through a *second*, independent connection: sqlite always
    # shows a connection its own uncommitted work, so reading through `conn`
    # (the connection that wrote the row) would pass even if the trailing
    # commit() at the end of transaction() were silently dropped. WAL mode
    # makes this concurrent read from a second connection clean.
    path = tmp_path / "t.db"
    conn = connect(path)

    with conn.transaction() as txn:
        txn.execute(
            "INSERT INTO observations (ts, source, subject, text)"
            " VALUES ('t', 's', NULL, 'committed')")

    other = sqlite3.connect(str(path))
    try:
        count = other.execute(
            "SELECT COUNT(*) AS c FROM observations").fetchone()[0]
    finally:
        other.close()
    assert count == 1


def test_transaction_rolls_back_on_exception(tmp_path):
    conn = connect(tmp_path / "t.db")

    class Boom(Exception):
        pass

    try:
        with conn.transaction() as txn:
            txn.execute(
                "INSERT INTO observations (ts, source, subject, text)"
                " VALUES ('t', 's', NULL, 'rolled-back')")
            raise Boom
    except Boom:
        pass

    assert conn.execute(
        "SELECT COUNT(*) AS c FROM observations").fetchone()["c"] == 0


def test_transaction_rollback_does_not_discard_other_threads_uncommitted_write(tmp_path):
    # Pins the second-review-wave hazard: transaction()'s rollback used to
    # call conn.rollback(), which rolls back the *whole* shared connection --
    # including another thread's write that is sitting uncommitted because it
    # released the lock between execute() and commit() (the pattern every
    # store here uses, e.g. ReviewQueue.approve's atomic claim). A
    # SAVEPOINT-scoped rollback must undo only the failing block's own
    # statements and leave the other thread's pending write intact and still
    # committable.
    path = tmp_path / "t.db"
    conn = connect(path)

    write_issued = threading.Event()
    txn_settled = threading.Event()
    errors: list[Exception] = []

    class Boom(Exception):
        pass

    def writer_thread() -> None:
        try:
            # execute() takes and releases the lock on its own; the write is
            # issued but not yet committed when this returns.
            conn.execute(
                "INSERT INTO observations (ts, source, subject, text)"
                " VALUES ('t', 'writer', NULL, 'uncommitted')")
            write_issued.set()
            txn_settled.wait(timeout=5)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 — surfaced by assertion below
            errors.append(exc)

    def txn_thread() -> None:
        try:
            write_issued.wait(timeout=5)
            try:
                with conn.transaction() as txn:
                    txn.execute(
                        "INSERT INTO observations (ts, source, subject, text)"
                        " VALUES ('t', 'txn', NULL, 'should-be-rolled-back')")
                    raise Boom
            except Boom:
                pass
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            txn_settled.set()

    t1 = threading.Thread(target=writer_thread)
    t2 = threading.Thread(target=txn_thread)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert errors == []

    other = sqlite3.connect(str(path))
    try:
        rows = other.execute(
            "SELECT source FROM observations ORDER BY id").fetchall()
    finally:
        other.close()
    assert rows == [("writer",)]


def test_transaction_allows_nested_execute_via_relock(tmp_path):
    # The lock is an RLock: execute()'s own "with self._lock" must re-enter
    # cleanly from inside transaction()'s "with self._lock", or a
    # multi-statement write would deadlock the writing thread against itself.
    conn = connect(tmp_path / "t.db")

    with conn.transaction() as txn:
        first = txn.execute(
            "INSERT INTO observations (ts, source, subject, text)"
            " VALUES ('t', 's', NULL, 'a')")
        second = txn.execute(
            "INSERT INTO search_index (kind, ref_id, subject, content)"
            " VALUES ('observation', ?, 's', 'a')", (str(first.lastrowid),))
        assert second.rowcount == 1

    assert conn.execute(
        "SELECT COUNT(*) AS c FROM observations").fetchone()["c"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM search_index").fetchone()["c"] == 1


def test_rows_fetchone_advances_and_returns_none_past_end(tmp_path):
    conn = connect(tmp_path / "t.db")
    for i in range(3):
        conn.execute(
            "INSERT INTO observations (ts, source, subject, text)"
            " VALUES ('t', 's', NULL, ?)", (str(i),))
        conn.commit()

    cur = conn.execute("SELECT text FROM observations ORDER BY id")
    assert cur.fetchone()["text"] == "0"
    assert cur.fetchone()["text"] == "1"
    assert cur.fetchone()["text"] == "2"
    assert cur.fetchone() is None
    assert cur.fetchone() is None


def test_rows_fetchall_returns_only_remaining_rows_after_fetchone(tmp_path):
    # Matches sqlite3.Cursor's real behavior: fetchall() after some
    # fetchone() calls returns only what's left, not the whole result set.
    conn = connect(tmp_path / "t.db")
    for i in range(3):
        conn.execute(
            "INSERT INTO observations (ts, source, subject, text)"
            " VALUES ('t', 's', NULL, ?)", (str(i),))
        conn.commit()

    cur = conn.execute("SELECT text FROM observations ORDER BY id")
    assert cur.fetchone()["text"] == "0"
    remaining = cur.fetchall()
    assert [r["text"] for r in remaining] == ["1", "2"]
    assert cur.fetchall() == []
    assert cur.fetchone() is None
