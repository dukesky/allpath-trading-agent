from datetime import UTC, datetime, timedelta

from allpath_trade.store.db import connect
from allpath_trade.store.llm_usage import LLMUsage


def make(tmp_path) -> LLMUsage:
    return LLMUsage(connect(tmp_path / "t.db"))


def test_record_then_summary_groups_by_tier_and_model(tmp_path):
    usage = make(tmp_path)
    usage.record(tier="chat", model="claude-sonnet-5", input_tokens=100,
                output_tokens=20, purpose="chat")
    usage.record(tier="chat", model="claude-sonnet-5", input_tokens=50,
                output_tokens=10, purpose="chat")
    usage.record(tier="memory", model="claude-opus-5", input_tokens=1000,
                output_tokens=200, purpose="memory")

    rows = {(r["tier"], r["model"]): r for r in usage.summary(7)}
    assert rows[("chat", "claude-sonnet-5")]["input_tokens"] == 150
    assert rows[("chat", "claude-sonnet-5")]["output_tokens"] == 30
    assert rows[("chat", "claude-sonnet-5")]["calls"] == 2
    assert rows[("memory", "claude-opus-5")]["input_tokens"] == 1000


def test_summary_excludes_rows_outside_the_window(tmp_path):
    usage = make(tmp_path)
    usage.record(tier="chat", model="m", input_tokens=10, output_tokens=5, purpose="chat")
    old_ts = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    usage._conn.execute(
        "INSERT INTO llm_usage (ts, tier, model, input_tokens, output_tokens, purpose)"
        " VALUES (?, 'chat', 'm', 999, 999, 'chat')", (old_ts,))
    usage._conn.commit()

    rows = usage.summary(7)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 10


def test_daily_groups_by_utc_calendar_day(tmp_path):
    usage = make(tmp_path)
    usage.record(tier="chat", model="m", input_tokens=10, output_tokens=5, purpose="chat")
    usage.record(tier="review", model="m2", input_tokens=20, output_tokens=8, purpose="review")

    rows = usage.daily(7)
    today = datetime.now(UTC).date().isoformat()
    assert len(rows) == 1
    assert rows[0]["day"] == today
    assert rows[0]["input_tokens"] == 30
    assert rows[0]["output_tokens"] == 13


def test_summary_for_day_defaults_to_today_utc(tmp_path):
    usage = make(tmp_path)
    usage.record(tier="chat", model="m", input_tokens=10, output_tokens=5, purpose="chat")
    usage.record(tier="review", model="m2", input_tokens=20, output_tokens=8, purpose="review")

    rows = usage.summary_for_day()

    assert {(r["tier"], r["model"], r["input_tokens"], r["output_tokens"]) for r in rows} == {
        ("chat", "m", 10, 5), ("review", "m2", 20, 8)}


def test_summary_for_day_excludes_rows_from_other_utc_calendar_days(tmp_path):
    # The whole point of `summary_for_day` over `summary(1)`: a row from
    # yesterday (UTC) must never bleed into "today"'s total, even though a
    # rolling 24h window from "now" would still include most of it.
    conn = connect(tmp_path / "t.db")
    usage = LLMUsage(conn)
    today = datetime.now(UTC).date().isoformat()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    conn.execute(
        "INSERT INTO llm_usage (ts, tier, model, input_tokens, output_tokens, purpose)"
        " VALUES (?, 'chat', 'm', 100, 100, 'chat')", (f"{yesterday}T23:59:00+00:00",))
    conn.execute(
        "INSERT INTO llm_usage (ts, tier, model, input_tokens, output_tokens, purpose)"
        " VALUES (?, 'chat', 'm', 7, 3, 'chat')", (f"{today}T00:00:01+00:00",))
    conn.commit()

    rows = usage.summary_for_day(today)

    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 7
    assert rows[0]["output_tokens"] == 3


def test_summary_for_day_accepts_an_explicit_date(tmp_path):
    conn = connect(tmp_path / "t.db")
    usage = LLMUsage(conn)
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    conn.execute(
        "INSERT INTO llm_usage (ts, tier, model, input_tokens, output_tokens, purpose)"
        " VALUES (?, 'chat', 'm', 5, 1, 'chat')", (f"{yesterday}T12:00:00+00:00",))
    conn.commit()

    assert usage.summary_for_day() == []  # nothing today
    rows = usage.summary_for_day(yesterday)
    assert len(rows) == 1 and rows[0]["input_tokens"] == 5


def test_record_never_raises_on_negative_or_odd_inputs(tmp_path):
    usage = make(tmp_path)
    usage.record(tier="chat", model="m", input_tokens=0, output_tokens=0, purpose="chat")
    [row] = usage.summary(7)
    assert row["input_tokens"] == 0 and row["output_tokens"] == 0
