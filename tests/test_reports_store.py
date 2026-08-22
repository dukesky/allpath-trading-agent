import sqlite3

import pytest

from allpath_trade.store.db import connect
from allpath_trade.store.reports import ReportStore


def make_store(tmp_path):
    return ReportStore(connect(tmp_path / "t.db"))


def test_add_and_get_round_trip(tmp_path):
    store = make_store(tmp_path)
    rid = store.add(date="2026-08-10", body="full report body", summary="short summary",
                    conversation_id=1, model="opus", tokens_used=1234)
    assert rid == 1
    row = store.get("2026-08-10")
    assert row["date"] == "2026-08-10"
    assert row["body"] == "full report body"
    assert row["summary"] == "short summary"
    assert row["conversation_id"] == 1
    assert row["model"] == "opus"
    assert row["tokens_used"] == 1234
    assert row["status"] == "ok"


def test_get_missing_date_returns_none(tmp_path):
    store = make_store(tmp_path)
    assert store.get("2026-08-10") is None


def test_exists(tmp_path):
    store = make_store(tmp_path)
    assert store.exists("2026-08-10") is False
    store.add(date="2026-08-10", body="b", summary="s", conversation_id=None,
             model="opus", tokens_used=1)
    assert store.exists("2026-08-10") is True


def test_add_duplicate_date_replaces_the_existing_row(tmp_path):
    """I9: `add` UPSERTs on (account, date) rather than raising -- a night
    that stored a `failed` row must be replaceable by a later successful
    retry for the same ET day, and the (account, date) UNIQUE constraint
    means the retry has nowhere else to put it. Still exactly one row per
    (account, date); the id is stable across the replace."""
    store = make_store(tmp_path)
    first = store.add(date="2026-08-10", body="b", summary="s", conversation_id=None,
             model="opus", tokens_used=1, status="failed")
    second = store.add(date="2026-08-10", body="b2", summary="s2", conversation_id=7,
             model="opus", tokens_used=2)
    assert second == first
    assert len(store.list()) == 1
    row = store.get("2026-08-10")
    assert row["status"] == "ok"
    assert row["body"] == "b2"
    assert row["summary"] == "s2"
    assert row["conversation_id"] == 7


def test_add_replace_leaves_the_other_accounts_row_alone(tmp_path):
    conn = connect(tmp_path / "t.db")
    paper, shadow = ReportStore(conn), ReportStore(conn, account="shadow")
    paper.add(date="2026-08-10", body="paper", summary="s", conversation_id=None,
             model="opus", tokens_used=1, status="failed")
    shadow.add(date="2026-08-10", body="shadow", summary="s", conversation_id=None,
              model="opus", tokens_used=1)
    paper.add(date="2026-08-10", body="paper retry", summary="s",
             conversation_id=None, model="opus", tokens_used=1)
    assert paper.get("2026-08-10")["body"] == "paper retry"
    assert shadow.get("2026-08-10")["body"] == "shadow"
    assert shadow.get("2026-08-10")["status"] == "ok"


def test_exists_ok_ignores_a_failed_row(tmp_path):
    """I9: `exists` is the "is there any row" question -- a `failed` row
    answers it True and would block the day's retry forever. `exists_ok`
    is the question the reflection idempotency guard actually wants."""
    store = make_store(tmp_path)
    store.add(date="2026-08-10", body="b", summary="", conversation_id=None,
             model="opus", tokens_used=1, status="failed")
    assert store.exists("2026-08-10") is True
    assert store.exists_ok("2026-08-10") is False
    store.add(date="2026-08-10", body="b2", summary="s", conversation_id=None,
             model="opus", tokens_used=1)
    assert store.exists_ok("2026-08-10") is True


def test_exists_ok_false_when_no_row_at_all(tmp_path):
    assert make_store(tmp_path).exists_ok("2026-08-10") is False


def test_list_orders_newest_date_first(tmp_path):
    store = make_store(tmp_path)
    store.add(date="2026-08-08", body="b1", summary="s1", conversation_id=None,
             model="opus", tokens_used=1)
    store.add(date="2026-08-10", body="b2", summary="s2", conversation_id=None,
             model="opus", tokens_used=1)
    store.add(date="2026-08-09", body="b3", summary="s3", conversation_id=None,
             model="opus", tokens_used=1)
    rows = store.list()
    assert [r["date"] for r in rows] == ["2026-08-10", "2026-08-09", "2026-08-08"]


def test_list_respects_limit(tmp_path):
    store = make_store(tmp_path)
    for i in range(5):
        store.add(date=f"2026-08-{i + 1:02d}", body="b", summary="s",
                 conversation_id=None, model="opus", tokens_used=1)
    rows = store.list(limit=2)
    assert len(rows) == 2


def test_add_failed_status_defaults_ok_otherwise(tmp_path):
    store = make_store(tmp_path)
    store.add(date="2026-08-10", body="error: llm timeout", summary="report failed",
             conversation_id=None, model="opus", tokens_used=0, status="failed")
    row = store.get("2026-08-10")
    assert row["status"] == "failed"


# --- shadow-dual-active T1: account scoping -------------------------------

def test_same_date_two_accounts_both_allowed(tmp_path):
    conn = connect(tmp_path / "t.db")
    paper = ReportStore(conn)
    shadow = ReportStore(conn, account="shadow")

    paper.add(date="2026-08-10", body="paper body", summary="paper summary",
              conversation_id=None, model="opus", tokens_used=1)
    # Must NOT raise -- (account, date) is the unique key, not date alone.
    shadow.add(date="2026-08-10", body="shadow body", summary="shadow summary",
              conversation_id=None, model="opus", tokens_used=1)

    prow = paper.get("2026-08-10")
    srow = shadow.get("2026-08-10")
    assert prow["body"] == "paper body"
    assert prow["account"] == "paper"
    assert srow["body"] == "shadow body"
    assert srow["account"] == "shadow"


def test_same_date_same_account_still_unique(tmp_path):
    # I9 turned `add` into an UPSERT, so it no longer raises on a repeat --
    # assert the (account, date) UNIQUE constraint itself is still there, on
    # a raw INSERT that has no ON CONFLICT clause to absorb it. Without the
    # constraint the "one row per (account, ET day)" invariant every reader
    # (`get`, the Reports page) relies on would quietly disappear.
    conn = connect(tmp_path / "t.db")
    paper = ReportStore(conn)
    paper.add(date="2026-08-10", body="b", summary="s", conversation_id=None,
             model="opus", tokens_used=1)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reports (account, date, body, summary, model,"
            " tokens_used, status, created_at) VALUES"
            " ('paper', '2026-08-10', 'b2', 's2', 'opus', 1, 'ok', 'now')")


def test_list_and_list_between_scoped_to_account(tmp_path):
    conn = connect(tmp_path / "t.db")
    paper = ReportStore(conn)
    shadow = ReportStore(conn, account="shadow")
    paper.add(date="2026-08-10", body="b", summary="s", conversation_id=None,
             model="opus", tokens_used=1)
    shadow.add(date="2026-08-10", body="b", summary="s", conversation_id=None,
              model="opus", tokens_used=1)
    shadow.add(date="2026-08-11", body="b", summary="s", conversation_id=None,
              model="opus", tokens_used=1)

    assert [r["date"] for r in paper.list()] == ["2026-08-10"]
    assert len(shadow.list()) == 2
    assert [r["date"] for r in paper.list_between("2026-08-01", "2026-08-31")] == [
        "2026-08-10"]


def test_legacy_reports_row_defaults_account_paper_after_migration(tmp_path):
    # Simulate a pre-shadow-dual-active database: `reports` exists with the
    # old single-column UNIQUE(date), no `account` column. CREATE TABLE IF
    # NOT EXISTS won't touch it, so the migration must (1) add + backfill
    # `account`, and (2) rebuild the table so the unique key becomes
    # (account, date) -- SQLite can't ALTER a UNIQUE constraint in place.
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " date TEXT NOT NULL UNIQUE, body TEXT NOT NULL, summary TEXT NOT NULL,"
        " conversation_id INTEGER, model TEXT NOT NULL DEFAULT '',"
        " tokens_used INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ok',"
        " created_at TEXT NOT NULL)")
    raw.execute(
        "INSERT INTO reports (date, body, summary, model, created_at)"
        " VALUES ('2026-08-01', 'legacy body', 'legacy summary', 'opus',"
        " '2026-08-01T00:00:00+00:00')")
    raw.commit()
    raw.close()

    conn = connect(path)
    row = conn.execute("SELECT account FROM reports WHERE date = ?",
                       ("2026-08-01",)).fetchone()
    assert row["account"] == "paper"

    paper = ReportStore(conn)
    shadow = ReportStore(conn, account="shadow")
    assert paper.get("2026-08-01")["body"] == "legacy body"
    # The rebuilt table must genuinely enforce (account, date), not just
    # carry the column -- a same-date shadow report must be allowed.
    shadow.add(date="2026-08-01", body="shadow body", summary="s",
              conversation_id=None, model="opus", tokens_used=1)
    assert shadow.get("2026-08-01")["body"] == "shadow body"

    # Migration must also be idempotent across restarts.
    conn2 = connect(path)
    row2 = conn2.execute("SELECT account FROM reports WHERE date = ?",
                         ("2026-08-01",)).fetchone()
    assert row2["account"] == "paper"


def test_constructor_rejects_invalid_account(tmp_path):
    with pytest.raises(ValueError, match="invalid account"):
        ReportStore(connect(tmp_path / "t.db"), account="evil")
