from allpath_trade.memory.observations import ObservationLog
from allpath_trade.store.db import connect


def test_add_and_recent(tmp_path):
    log = ObservationLog(connect(tmp_path / "db.sqlite"))
    log.add("chat", "user asked about NVDA", subject="NVDA")
    log.add("sentinel", "trigger fired")
    rows = log.recent()
    assert [r["source"] for r in rows] == ["chat", "sentinel"]
    assert rows[0]["subject"] == "NVDA"


def test_recent_since_filter(tmp_path):
    log = ObservationLog(connect(tmp_path / "db.sqlite"))
    log.add("chat", "old note")
    rows = log.recent(since_iso="2999-01-01T00:00:00+00:00")
    assert rows == []


def test_window_returns_newest_first_within_bounds(tmp_path):
    log = ObservationLog(connect(tmp_path / "db.sqlite"))
    log.add("chat", "first")
    log.add("chat", "second")
    log.add("chat", "third")
    rows = log.window(since_iso="1900-01-01T00:00:00+00:00",
                      until_iso="2999-01-01T00:00:00+00:00")
    assert [r["text"] for r in rows] == ["third", "second", "first"]


def test_window_limit_keeps_newest_on_overflow(tmp_path):
    # This is the exact upstream bug from the Task 4 reflection review: a
    # day with more rows than `limit` must keep the newest ones, not the
    # oldest -- `recent`'s `ORDER BY id ASC LIMIT` would silently drop the
    # afternoon/close on an overflow day.
    log = ObservationLog(connect(tmp_path / "db.sqlite"))
    for i in range(10):
        log.add("chat", f"o{i}")
    rows = log.window(since_iso="1900-01-01T00:00:00+00:00",
                      until_iso="2999-01-01T00:00:00+00:00", limit=3)
    assert [r["text"] for r in rows] == ["o9", "o8", "o7"]


def test_window_excludes_rows_outside_bounds(tmp_path):
    log = ObservationLog(connect(tmp_path / "db.sqlite"))
    log.add("chat", "in range")
    rows = log.window(since_iso="2999-01-01T00:00:00+00:00",
                      until_iso="2999-12-31T00:00:00+00:00")
    assert rows == []


def test_recent_and_window_are_scoped_by_account(tmp_path):
    # shadow-dual-active T4 CRITICAL carry (T1 review): a shadow observation
    # must never surface through a paper-scoped ObservationLog's reads, or
    # vice versa.
    conn = connect(tmp_path / "db.sqlite")
    paper = ObservationLog(conn, account="paper")
    shadow = ObservationLog(conn, account="shadow")
    paper.add("chat", "paper only note")
    shadow.add("chat", "shadow only note")

    paper_rows = paper.recent()
    shadow_rows = shadow.recent()
    assert [r["text"] for r in paper_rows] == ["paper only note"]
    assert [r["text"] for r in shadow_rows] == ["shadow only note"]

    paper_window = paper.window(since_iso="1900-01-01T00:00:00+00:00",
                                until_iso="2999-01-01T00:00:00+00:00")
    assert [r["text"] for r in paper_window] == ["paper only note"]


def test_last_marker_ts_is_scoped_by_account(tmp_path):
    # The consolidator's watermark read (memory/consolidate.py's
    # `_last_marker_ts`) must find only this account's own "consolidator"
    # marker row -- otherwise whichever account's consolidator ran most
    # recently would silently advance the OTHER account's watermark.
    conn = connect(tmp_path / "db.sqlite")
    paper = ObservationLog(conn, account="paper")
    shadow = ObservationLog(conn, account="shadow")
    shadow.add("consolidator", "shadow consolidation run")

    assert paper.last_marker_ts("consolidator") is None
    assert shadow.last_marker_ts("consolidator") is not None


def test_observation_log_rejects_invalid_account(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    try:
        ObservationLog(conn, account="../../etc")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an invalid account")
