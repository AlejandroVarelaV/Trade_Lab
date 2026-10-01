"""Alpaca market data (read-only) and the trading calendar.

- Stocks/ETFs: daily bars from the free IEX feed, raw prices (no split or
  dividend adjustment, see README "Known limitations").
- Crypto: hourly bars aggregated into 22:00-UTC days (calendars.py), so the
  close is the price at the daily mark. A day is built only when complete and
  only if its last hour (21:00-22:00) exists; otherwise it stays missing.
- Fills for crypto use the first 1-minute bar at or after 12:00 UTC.

A bar with a missing or non-finite value is rejected and reported, never stored.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from tradelab.calendars import UTC, crypto_day_bounds, crypto_day_of

log = logging.getLogger(__name__)

DATA_URL = "https://data.alpaca.markets"
NEW_YORK = ZoneInfo("America/New_York")
SOURCE_STOCK = "alpaca_iex_1d_raw"
SOURCE_CRYPTO = "alpaca_crypto_1h_22utc"


@dataclass(frozen=True)
class Bar:
    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str


@dataclass(frozen=True)
class Session:
    date: date
    open_at: datetime
    close_at: datetime


def _finite_bar(raw: dict) -> bool:
    try:
        vals = [float(raw[k]) for k in ("o", "h", "l", "c", "v")]
    except (KeyError, TypeError, ValueError):
        return False
    return all(math.isfinite(v) for v in vals) and min(vals[:4]) > 0 and vals[4] >= 0


def _ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


def parse_stock_bars(payload: dict[str, list[dict]]) -> tuple[list[Bar], list[str]]:
    """Alpaca 1Day stock bars → Bars dated by their New York session date."""
    bars, rejected = [], []
    for symbol, raws in payload.items():
        for raw in raws:
            d = _ts(raw["t"]).astimezone(NEW_YORK).date()
            if not _finite_bar(raw):
                rejected.append(f"{symbol} {d}: non-finite or missing value")
                continue
            bars.append(Bar(symbol, d, float(raw["o"]), float(raw["h"]), float(raw["l"]),
                            float(raw["c"]), float(raw["v"]), SOURCE_STOCK))
    return bars, rejected


def aggregate_crypto_days(symbol: str, hourly: list[dict], first_day: date, last_day: date
                          ) -> tuple[list[Bar], list[str]]:
    """Hourly bars → 22:00-UTC daily bars for first_day..last_day (inclusive).

    Returns (bars, issues). A day needs its last hour (21:00 UTC); fewer than 24
    hours is reported but still built from the hours that exist (aggregating
    real trades, not filling).
    """
    by_day: dict[date, list[tuple[datetime, dict]]] = {}
    issues: list[str] = []
    for raw in hourly:
        ts = _ts(raw["t"])
        if not _finite_bar(raw):
            issues.append(f"{symbol} {ts:%Y-%m-%d %H:%M}: non-finite hourly bar rejected")
            continue
        by_day.setdefault(crypto_day_of(ts), []).append((ts, raw))

    bars = []
    d = first_day
    while d <= last_day:
        hours = sorted(by_day.get(d, []), key=lambda x: x[0])
        _, end = crypto_day_bounds(d)
        if not hours:
            issues.append(f"{symbol} {d}: no hourly bars")
        elif hours[-1][0] != end - timedelta(hours=1):
            issues.append(f"{symbol} {d}: last hour (21:00 UTC) missing, day not built")
        else:
            if len(hours) < 24:
                issues.append(f"{symbol} {d}: only {len(hours)}/24 hourly bars")
            raws = [r for _, r in hours]
            bars.append(Bar(
                symbol, d,
                open=float(raws[0]["o"]),
                high=max(float(r["h"]) for r in raws),
                low=min(float(r["l"]) for r in raws),
                close=float(raws[-1]["c"]),
                volume=sum(float(r["v"]) for r in raws),
                source=SOURCE_CRYPTO,
            ))
        d += timedelta(days=1)
    return bars, issues


def parse_calendar(rows: list[dict]) -> list[Session]:
    sessions = []
    for r in rows:
        d = date.fromisoformat(r["date"])
        o = datetime.combine(d, datetime.strptime(r["open"], "%H:%M").time(), NEW_YORK)
        c = datetime.combine(d, datetime.strptime(r["close"], "%H:%M").time(), NEW_YORK)
        sessions.append(Session(d, o.astimezone(UTC), c.astimezone(UTC)))
    return sessions


class AlpacaClient:
    """Read-only Alpaca client. `trading_url` must be the paper URL (settings guard)."""

    def __init__(self, key_id: str, secret_key: str, trading_url: str,
                 http: httpx.Client | None = None):
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        self._trading_url = trading_url
        self._http = http or httpx.Client(timeout=30)

    def _get(self, url: str, params: dict) -> dict | list:
        for attempt in range(4):
            resp = self._http.get(url, params=params, headers=self._headers)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2 ** attempt
                log.warning("alpaca %s on %s, retry in %ss", resp.status_code, url, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def _bars(self, path: str, params: dict) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        params = {**params, "limit": 10000}
        while True:
            data = self._get(f"{DATA_URL}{path}", params)
            for sym, rows in (data.get("bars") or {}).items():
                out.setdefault(sym, []).extend(rows)
            token = data.get("next_page_token")
            if not token:
                return out
            params["page_token"] = token

    def stock_daily_bars(self, symbols: list[str], start: date, end: date
                         ) -> tuple[list[Bar], list[str]]:
        payload = self._bars("/v2/stocks/bars", {
            "symbols": ",".join(symbols), "timeframe": "1Day", "feed": "iex",
            "adjustment": "raw", "start": start.isoformat(), "end": end.isoformat(),
        })
        bars, rejected = parse_stock_bars(payload)
        return [b for b in bars if start <= b.date <= end], rejected

    def crypto_daily_bars(self, symbols: list[str], first_day: date, last_day: date
                          ) -> tuple[list[Bar], list[str]]:
        start, _ = crypto_day_bounds(first_day)
        _, end = crypto_day_bounds(last_day)
        payload = self._bars("/v1beta3/crypto/us/bars", {
            "symbols": ",".join(symbols), "timeframe": "1Hour",
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": (end - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        })
        bars, issues = [], []
        for sym in symbols:
            b, i = aggregate_crypto_days(sym, payload.get(sym, []), first_day, last_day)
            bars += b
            issues += i
        return bars, issues

    def crypto_first_minute(self, symbol: str, at: datetime, until: datetime
                            ) -> tuple[datetime, float] | None:
        """(timestamp, open) of the first 1-minute bar in [at, until), or None."""
        payload = self._bars("/v1beta3/crypto/us/bars", {
            "symbols": symbol, "timeframe": "1Min",
            "start": at.isoformat().replace("+00:00", "Z"),
            "end": until.isoformat().replace("+00:00", "Z"),
        })
        for raw in sorted(payload.get(symbol, []), key=lambda r: r["t"]):
            ts = _ts(raw["t"])
            if at <= ts < until and _finite_bar(raw):
                return ts, float(raw["o"])
        return None

    def calendar(self, start: date, end: date) -> list[Session]:
        rows = self._get(f"{self._trading_url}/v2/calendar",
                         {"start": start.isoformat(), "end": end.isoformat()})
        return parse_calendar(rows)
