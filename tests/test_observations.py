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
