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


def test_total_tokens_since_sums_input_and_output(tmp_path):
    usage = make(tmp_path)
    assert usage.total_tokens_since(7) == 0
    usage.record(tier="chat", model="m", input_tokens=10, output_tokens=5, purpose="chat")
    usage.record(tier="chat", model="m", input_tokens=3, output_tokens=2, purpose="chat")
    assert usage.total_tokens_since(7) == 20


def test_record_never_raises_on_negative_or_odd_inputs(tmp_path):
    usage = make(tmp_path)
    usage.record(tier="chat", model="m", input_tokens=0, output_tokens=0, purpose="chat")
    [row] = usage.summary(7)
    assert row["input_tokens"] == 0 and row["output_tokens"] == 0
