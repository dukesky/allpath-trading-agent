from datetime import UTC, datetime, timedelta

import pytest

from allpath_trade.store.db import connect
from allpath_trade.store.views import CheckinStore, ViewStore


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "t.db")


NOW = datetime(2026, 11, 6, 22, 0, tzinfo=UTC)


def test_record_and_list_active_view(conn):
    views = ViewStore(conn, account="shadow")
    vid = views.add(subject="nvda", stance="bullish, add below 180",
                    horizon="6-12 months", conviction="high",
                    invalidation="datacenter capex guide cut", now=NOW)
    rows = views.active(now=NOW)
    assert [r["id"] for r in rows] == [vid]
    assert rows[0]["subject"] == "NVDA"
    assert rows[0]["source"] == "checkin"


def test_views_expire(conn):
    views = ViewStore(conn, account="shadow")
    views.add(subject="TSLA", stance="s", horizon="h", conviction="low",
              invalidation="i", expires_days=5, now=NOW)
    assert views.active(now=NOW + timedelta(days=4))
    assert views.active(now=NOW + timedelta(days=6)) == []


def test_expiry_days_clamped(conn):
    views = ViewStore(conn, account="shadow")
    views.add(subject="X", stance="s", horizon="h", conviction="c",
              invalidation="i", expires_days=10_000, now=NOW)
    row = views.active(now=NOW)[0]
    assert datetime.fromisoformat(row["expires_ts"]) == NOW + timedelta(days=180)


def test_views_are_account_scoped(conn):
    ViewStore(conn, account="shadow").add(
        subject="NVDA", stance="s", horizon="h", conviction="c",
        invalidation="i", now=NOW)
    assert ViewStore(conn, account="paper").active(now=NOW) == []


def test_relevant_views_match_ticker_and_market(conn):
    views = ViewStore(conn, account="shadow")
    views.add(subject="NVDA", stance="a", horizon="h", conviction="c",
              invalidation="i", now=NOW)
    views.add(subject="market", stance="b", horizon="h", conviction="c",
              invalidation="i", now=NOW)
    views.add(subject="AI capex", stance="c", horizon="h", conviction="c",
              invalidation="i", now=NOW)
    views.add(subject="TSLA", stance="d", horizon="h", conviction="c",
              invalidation="i", now=NOW)
    assert sorted(r["subject"] for r in views.relevant("nvda", now=NOW)) == [
        "MARKET", "NVDA"]


def test_empty_fields_rejected(conn):
    with pytest.raises(ValueError):
        ViewStore(conn, account="shadow").add(
            subject=" ", stance="s", horizon="h", conviction="c",
            invalidation="i", now=NOW)


def test_checkin_once_per_day_and_lookup_by_message(conn):
    store = CheckinStore(conn, account="shadow")
    assert not store.exists_for("2026-11-06")
    cid = store.add(et_date="2026-11-06", questions=["Q1?", "Q2?"],
                    telegram_message_id=42, now=NOW)
    assert store.exists_for("2026-11-06")
    row = store.by_message_id(42)
    assert row["id"] == cid
    assert row["questions"] == ["Q1?", "Q2?"]
    assert store.by_message_id(43) is None
    assert CheckinStore(conn, account="paper").by_message_id(42) is None
