from datetime import UTC, datetime

from allpath_trade.market_hours import is_us_market_open


def _et(y, m, d, hh, mm):
    # EDT (UTC-4) in September
    return datetime(y, m, d, hh + 4, mm, tzinfo=UTC)


def test_open_mid_session_weekday():
    assert is_us_market_open(_et(2026, 9, 14, 12, 0)) is True  # Monday


def test_boundaries_open_inclusive_close_exclusive():
    assert is_us_market_open(_et(2026, 9, 14, 9, 30)) is True
    assert is_us_market_open(_et(2026, 9, 14, 9, 29)) is False
    assert is_us_market_open(_et(2026, 9, 14, 16, 0)) is False


def test_weekend_closed():
    assert is_us_market_open(_et(2026, 9, 12, 12, 0)) is False  # Saturday


def test_naive_datetime_treated_as_utc():
    assert is_us_market_open(datetime(2026, 9, 14, 16, 0)) is True  # noqa: DTZ001
