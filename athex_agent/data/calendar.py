"""ATHEX trading calendar and session times.

Holidays: New Year, Epiphany, Clean Monday, Independence Day (25 Mar), Orthodox Good Friday and
Easter Monday, Labour Day, Whit Monday, Assumption (15 Aug), Ochi Day (28 Oct), Christmas, Boxing
Day. Ad-hoc closures or openings go in configs/calendar_overrides.yaml (never edit this file for a
one-off). Session: pre-open 10:00-10:30, opening auction print at 10:30, continuous trading to
17:00, close 17:20 (Athens time).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ATHENS = ZoneInfo("Europe/Athens")
UTC = ZoneInfo("UTC")
OPEN_TIME = time(10, 30)
CLOSE_TIME = time(17, 20)

_OVERRIDES_PATH = Path(__file__).resolve().parents[2] / "configs" / "calendar_overrides.yaml"


def orthodox_easter(year: int) -> date:
    """Meeus's Julian algorithm, shifted to the Gregorian calendar (valid 1900-2099)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = (d + e + 114) % 31 + 1
    return date(year, month, day) + timedelta(days=13)


@lru_cache(maxsize=64)
def holidays(year: int) -> frozenset[date]:
    easter = orthodox_easter(year)
    fixed = {
        date(year, 1, 1),
        date(year, 1, 6),
        date(year, 3, 25),
        date(year, 5, 1),
        date(year, 8, 15),
        date(year, 10, 28),
        date(year, 12, 25),
        date(year, 12, 26),
    }
    moveable = {
        easter - timedelta(days=48),  # Clean Monday
        easter - timedelta(days=2),  # Good Friday
        easter + timedelta(days=1),  # Easter Monday
        easter + timedelta(days=50),  # Whit Monday
    }
    # Greek law moves Labour Day to the next working day when it falls in the Easter break.
    may1 = date(year, 5, 1)
    if may1 in moveable or may1 == easter:
        d = may1
        while d in moveable or d == easter or d.weekday() >= 5:
            d += timedelta(days=1)
        fixed.discard(may1)
        fixed.add(d)
    return frozenset(fixed | moveable)


@lru_cache(maxsize=1)
def _overrides() -> tuple[frozenset[date], frozenset[date]]:
    if not _OVERRIDES_PATH.exists():
        return frozenset(), frozenset()
    data = yaml.safe_load(_OVERRIDES_PATH.read_text(encoding="utf-8")) or {}
    closed = frozenset(_as_date(x) for x in data.get("extra_closures", []) or [])
    opened = frozenset(_as_date(x) for x in data.get("extra_open", []) or [])
    return closed, opened


def _as_date(x: object) -> date:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x))


def is_trading_day(d: date) -> bool:
    closed, opened = _overrides()
    if d in opened:
        return True
    if d.weekday() >= 5 or d in holidays(d.year) or d in closed:
        return False
    return True


def next_trading_day(d: date) -> date:
    """First trading day strictly after d."""
    n = d + timedelta(days=1)
    while not is_trading_day(n):
        n += timedelta(days=1)
    return n


def prev_trading_day(d: date) -> date:
    """Last trading day strictly before d."""
    p = d - timedelta(days=1)
    while not is_trading_day(p):
        p -= timedelta(days=1)
    return p


def add_trading_days(d: date, n: int) -> date:
    """d shifted by n trading days (n may be negative). d itself need not be a trading day."""
    out = d
    step = next_trading_day if n >= 0 else prev_trading_day
    for _ in range(abs(n)):
        out = step(out)
    return out


def trading_days(start: date, end: date) -> list[date]:
    """All trading days in [start, end]."""
    out = []
    d = start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def trading_days_between(a: date, b: date) -> int:
    """Number of trading days strictly after a up to and including b (0 if b <= a)."""
    if b <= a:
        return 0
    return len(trading_days(a + timedelta(days=1), b))


def first_trading_day_of_month(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d if is_trading_day(d) else next_trading_day(d)


def session_open_utc(d: date) -> datetime:
    return datetime.combine(d, OPEN_TIME, tzinfo=ATHENS).astimezone(UTC)


def session_close_utc(d: date) -> datetime:
    return datetime.combine(d, CLOSE_TIME, tzinfo=ATHENS).astimezone(UTC)


def last_completed_session(now_utc: datetime) -> date:
    """The most recent trading date whose close is at or before now_utc."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    local = now_utc.astimezone(ATHENS)
    d = local.date()
    if not is_trading_day(d) or local.time() < CLOSE_TIME:
        d = prev_trading_day(d)
    return d
