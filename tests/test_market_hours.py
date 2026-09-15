from datetime import UTC, datetime

from allpath_trade.market_hours import ET, is_us_market_open, next_us_market_open


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


def test_next_open_after_friday_close_is_monday():
    friday_5pm = _et(2026, 9, 11, 17, 0)  # Friday
    got = next_us_market_open(friday_5pm)
    assert got == datetime(2026, 9, 14, 9, 30, tzinfo=ET)


def test_next_open_before_todays_open_on_a_weekday_is_today():
    tuesday_8am = _et(2026, 9, 15, 8, 0)  # Tuesday
    got = next_us_market_open(tuesday_8am)
    assert got == datetime(2026, 9, 15, 9, 30, tzinfo=ET)


def test_next_open_on_a_weekend_is_monday():
    saturday = _et(2026, 9, 12, 12, 0)  # Saturday
    got = next_us_market_open(saturday)
    assert got == datetime(2026, 9, 14, 9, 30, tzinfo=ET)


def test_next_open_while_market_is_open_is_the_current_sessions_open():
    monday_noon = _et(2026, 9, 14, 12, 0)  # Monday, market open
    got = next_us_market_open(monday_noon)
    assert got == datetime(2026, 9, 14, 9, 30, tzinfo=ET)


def test_next_open_treats_naive_datetime_as_utc():
    # 2026-09-11 20:00 UTC == Friday 16:00 ET (market closes right at this
    # instant -- closed, not open) -- next open is Monday 09:30 ET.
    got = next_us_market_open(datetime(2026, 9, 11, 20, 0))  # noqa: DTZ001
    assert got == datetime(2026, 9, 14, 9, 30, tzinfo=ET)
