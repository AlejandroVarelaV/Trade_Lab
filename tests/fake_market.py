"""A deterministic stand-in for Alpaca and the ECB, for DB tests of the daily run."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from tradelab.calendars import is_ecb_business_day
from tradelab.data.alpaca import SOURCE_CRYPTO, SOURCE_STOCK, Bar, Session

UTC = timezone.utc
START = date(2026, 6, 1)
BASE = {"SPY": 700.0, "QQQ": 600.0, "IWM": 250.0, "TLT": 90.0, "GLD": 300.0, "XLE": 90.0,
        "XLF": 50.0, "XLK": 250.0, "XLV": 150.0, "AAPL": 330.0, "MSFT": 500.0, "NVDA": 180.0,
        "AMZN": 230.0, "GOOGL": 250.0, "META": 750.0, "BTC/USD": 80000.0, "ETH/USD": 4000.0,
        "SOL/USD": 120.0}


def price(symbol: str, d: date) -> float:
    """A gentle, deterministic up-and-down walk."""
    n = (d - START).days
    return round(BASE[symbol] * (1 + 0.002 * n + 0.01 * ((n % 5) - 2)), 4)


def fx(d: date) -> float:
    return round(1.10 + 0.0005 * ((d - START).days % 20), 4)


class FakeAlpaca:
    def __init__(self):
        self.missing: set[tuple[str, date]] = set()        # bars the "source" doesn't have
        self.overrides: dict[tuple[str, date], float] = {}  # corrected closes
        self.minute_price_factor = 0.998

    @staticmethod
    def sessions(start: date, end: date) -> list[Session]:
        out, d = [], start
        while d <= end:
            if d.weekday() < 5:
                out.append(Session(d, datetime.combine(d, time(13, 30), UTC),
                                   datetime.combine(d, time(20, 0), UTC)))
            d += timedelta(days=1)
        return out

    def calendar(self, start, end):
        return self.sessions(max(start, START), end)

    def _bar(self, sym, d, source):
        c = self.overrides.get((sym, d), price(sym, d))
        o = round(price(sym, d) * 0.999, 4)
        return Bar(sym, d, o, max(o, c) * 1.01, min(o, c) * 0.99, c, 1_000_000.0 + (d - START).days,
                   source)

    def stock_daily_bars(self, symbols, start, end):
        return [self._bar(s, x.date, SOURCE_STOCK) for x in self.sessions(max(start, START), end)
                for s in symbols if (s, x.date) not in self.missing], []

    def crypto_daily_bars(self, symbols, first, last):
        out, d = [], max(first, START)
        while d <= last:
            out += [self._bar(s, d, SOURCE_CRYPTO) for s in symbols if (s, d) not in self.missing]
            d += timedelta(days=1)
        return out, []

    def crypto_first_minute(self, symbol, at, until):
        return at, round(price(symbol, at.date()) * self.minute_price_factor, 4)


def fake_fx(start: date, end: date):
    out, d = [], max(start, START)
    while d <= end:
        if is_ecb_business_day(d):
            out.append((d, fx(d)))
        d += timedelta(days=1)
    return out, []
