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


def test_add_duplicate_date_raises(tmp_path):
    store = make_store(tmp_path)
    store.add(date="2026-08-10", body="b", summary="s", conversation_id=None,
             model="opus", tokens_used=1)
    with pytest.raises(sqlite3.IntegrityError):
        store.add(date="2026-08-10", body="b2", summary="s2", conversation_id=None,
                 model="opus", tokens_used=1)


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
