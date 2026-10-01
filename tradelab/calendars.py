"""Time rules (SPEC §3.2, §4): when data is complete and when orders fill.

- A crypto "day" D runs from D-1 22:00 UTC to D 22:00 UTC, so its close is the
  22:00 UTC mark price and the 22:30 run always sees a complete bar.
- Stocks/ETFs follow the US sessions in `market_calendar` (Alpaca /v2/calendar).
- The ECB publishes EUR/USD on TARGET business days: weekdays except New Year,
  Good Friday, Easter Monday, 1 May, 25 and 26 December.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

UTC = timezone.utc
CRYPTO_DAY_END = time(22, 0)        # crypto close = mark time
CRYPTO_FILL_TIME = time(12, 0)      # first 1-minute bar at or after 12:00 UTC
DAILY_RUN_TIME = time(22, 30)


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def target_holidays(year: int) -> set[date]:
    e = easter(year)
    return {date(year, 1, 1), e - timedelta(days=2), e + timedelta(days=1),
            date(year, 5, 1), date(year, 12, 25), date(year, 12, 26)}


def is_ecb_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in target_holidays(d.year)


def ecb_business_days(start: date, end: date) -> list[date]:
    return [d for d in daterange(start, end) if is_ecb_business_day(d)]


def last_ecb_business_day(d: date) -> date:
    while not is_ecb_business_day(d):
        d -= timedelta(days=1)
    return d


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def last_complete_day(t: datetime) -> date:
    """Latest date whose daily mark (22:00 UTC) is at or before t."""
    t = t.astimezone(UTC)
    return t.date() if t.time() >= CRYPTO_DAY_END else t.date() - timedelta(days=1)


def crypto_day_bounds(d: date) -> tuple[datetime, datetime]:
    """[start, end) of crypto day d: (d-1 22:00, d 22:00) UTC."""
    end = datetime.combine(d, CRYPTO_DAY_END, UTC)
    return end - timedelta(days=1), end


def crypto_day_of(ts: datetime) -> date:
    """The crypto day a timestamp belongs to."""
    ts = ts.astimezone(UTC)
    return ts.date() + timedelta(days=1) if ts.time() >= CRYPTO_DAY_END else ts.date()


def next_crypto_fill_window(decided_at: datetime) -> datetime:
    """The first 12:00 UTC strictly after the decision."""
    decided_at = decided_at.astimezone(UTC)
    window = datetime.combine(decided_at.date(), CRYPTO_FILL_TIME, UTC)
    return window if window > decided_at else window + timedelta(days=1)


def daily_run_time(d: date) -> datetime:
    return datetime.combine(d, DAILY_RUN_TIME, UTC)
