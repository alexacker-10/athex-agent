from __future__ import annotations

from datetime import UTC, date, datetime

from athex_agent.data import calendar as cal


def test_orthodox_easter_known_dates():
    assert cal.orthodox_easter(2024) == date(2024, 5, 5)
    assert cal.orthodox_easter(2025) == date(2025, 4, 20)
    assert cal.orthodox_easter(2026) == date(2026, 4, 12)
    assert cal.orthodox_easter(2027) == date(2027, 5, 2)


def test_holidays_2026():
    h = cal.holidays(2026)
    for d in [
        date(2026, 1, 1),
        date(2026, 1, 6),
        date(2026, 2, 23),  # Clean Monday
        date(2026, 3, 25),
        date(2026, 4, 10),  # Good Friday
        date(2026, 4, 13),  # Easter Monday
        date(2026, 5, 1),
        date(2026, 6, 1),  # Whit Monday
        date(2026, 8, 15),
        date(2026, 10, 28),
        date(2026, 12, 25),
        date(2026, 12, 26),
    ]:
        assert d in h, d
    assert date(2026, 12, 24) not in h and date(2026, 12, 31) not in h


def test_trading_day_navigation():
    assert cal.is_trading_day(date(2026, 9, 18))  # Friday
    assert not cal.is_trading_day(date(2026, 9, 19))  # Saturday
    assert not cal.is_trading_day(date(2026, 10, 28))  # Ochi Day (Wednesday)
    assert cal.next_trading_day(date(2026, 9, 18)) == date(2026, 9, 21)
    assert cal.prev_trading_day(date(2026, 9, 21)) == date(2026, 9, 18)
    assert cal.next_trading_day(date(2026, 10, 27)) == date(2026, 10, 29)
    assert cal.add_trading_days(date(2026, 9, 18), 5) == date(2026, 9, 25)
    assert cal.add_trading_days(date(2026, 9, 25), -5) == date(2026, 9, 18)
    assert cal.trading_days_between(date(2026, 9, 18), date(2026, 9, 25)) == 5
    assert cal.trading_days_between(date(2026, 9, 25), date(2026, 9, 18)) == 0
    assert cal.first_trading_day_of_month(2026, 11) == date(2026, 11, 2)
    assert cal.first_trading_day_of_month(2026, 10) == date(2026, 10, 1)
    assert (
        len(cal.trading_days(date(2026, 1, 1), date(2026, 12, 31))) == 251
    )  # 261 weekdays minus 10 weekday holidays


def test_session_times_follow_dst():
    assert cal.session_open_utc(date(2026, 9, 21)) == datetime(2026, 9, 21, 7, 30, tzinfo=UTC)
    assert cal.session_open_utc(date(2026, 12, 1)) == datetime(2026, 12, 1, 8, 30, tzinfo=UTC)
    assert cal.session_close_utc(date(2026, 9, 21)) == datetime(2026, 9, 21, 14, 20, tzinfo=UTC)


def test_last_completed_session():
    assert cal.last_completed_session(datetime(2026, 9, 21, 14, 19, tzinfo=UTC)) == date(
        2026, 9, 18
    )
    assert cal.last_completed_session(datetime(2026, 9, 21, 14, 20, tzinfo=UTC)) == date(
        2026, 9, 21
    )
    assert cal.last_completed_session(datetime(2026, 9, 20, 12, 0, tzinfo=UTC)) == date(2026, 9, 18)
