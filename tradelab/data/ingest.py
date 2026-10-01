"""Ingestion: upsert bars, FX and the trading calendar, and report gaps.

Every daily run re-ingests the last REINGEST_DAYS days and upserts, so late or
corrected data repairs itself. Rows are only ever written from source values:
a missing bar stays missing (no forward fill) and is reported by `find_gaps`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from psycopg2.extras import execute_values

from tradelab.calendars import ecb_business_days, last_complete_day
from tradelab.data import ecb
from tradelab.data.alpaca import AlpacaClient, Bar, Session

REINGEST_DAYS = 7
BACKFILL_DAYS = 420          # ≈ 290 US sessions: SMA200 + 63-day returns exist on day one
CALENDAR_AHEAD_DAYS = 30     # future sessions are needed to schedule fill windows


@dataclass
class UpsertCount:
    inserted: int = 0
    updated: int = 0          # existing row whose values changed (a repair)
    unchanged: int = 0
    changed_keys: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"inserted": self.inserted, "updated": self.updated,
                "unchanged": self.unchanged, "changed": self.changed_keys[:20]}


def instruments(cur) -> dict[str, str]:
    """Active universe: symbol → asset_class."""
    cur.execute("SELECT symbol, asset_class FROM instruments WHERE active_to IS NULL ORDER BY id")
    return dict(cur.fetchall())


def upsert_bars(cur, bars: list[Bar]) -> UpsertCount:
    count = UpsertCount()
    if not bars:
        return count
    rows = execute_values(cur, """
        INSERT INTO bars_daily (symbol, date, open, high, low, close, volume, source)
        VALUES %s
        ON CONFLICT (symbol, date) DO UPDATE SET
            open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
            close = EXCLUDED.close, volume = EXCLUDED.volume, source = EXCLUDED.source,
            ingested_at = now()
        WHERE (bars_daily.open, bars_daily.high, bars_daily.low, bars_daily.close,
               bars_daily.volume, bars_daily.source)
              IS DISTINCT FROM
              (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close,
               EXCLUDED.volume, EXCLUDED.source)
        RETURNING symbol, date, (xmax = 0) AS inserted""",
        [(b.symbol, b.date, b.open, b.high, b.low, b.close, b.volume, b.source) for b in bars],
        template="(%s, %s, round(%s::numeric, 8), round(%s::numeric, 8), round(%s::numeric, 8),"
                 " round(%s::numeric, 8), round(%s::numeric, 8), %s)",
        fetch=True)
    for symbol, d, inserted in rows:
        if inserted:
            count.inserted += 1
        else:
            count.updated += 1
            count.changed_keys.append(f"{symbol} {d}")
    count.unchanged = len(bars) - len(rows)
    return count


def upsert_fx(cur, rows: list[tuple[date, float]]) -> UpsertCount:
    count = UpsertCount()
    if not rows:
        return count
    out = execute_values(cur, """
        INSERT INTO fx_daily (date, eurusd, source) VALUES %s
        ON CONFLICT (date) DO UPDATE SET eurusd = EXCLUDED.eurusd, source = EXCLUDED.source,
                                         ingested_at = now()
        WHERE (fx_daily.eurusd, fx_daily.source) IS DISTINCT FROM (EXCLUDED.eurusd, EXCLUDED.source)
        RETURNING date, (xmax = 0)""",
        [(d, v, ecb.SOURCE) for d, v in rows], fetch=True)
    for d, inserted in out:
        if inserted:
            count.inserted += 1
        else:
            count.updated += 1
            count.changed_keys.append(f"EURUSD {d}")
    count.unchanged = len(rows) - len(out)
    return count


def upsert_calendar(cur, sessions: list[Session]) -> int:
    if not sessions:
        return 0
    execute_values(cur, """
        INSERT INTO market_calendar (date, open_at, close_at) VALUES %s
        ON CONFLICT (date) DO UPDATE SET open_at = EXCLUDED.open_at, close_at = EXCLUDED.close_at""",
        [(s.date, s.open_at, s.close_at) for s in sessions])
    return len(sessions)


def ingest(cur, client: AlpacaClient, t: datetime, *, backfill: bool = False,
           fx_fetch=ecb.fetch) -> dict:
    """Fetch and upsert everything complete at time t. Returns a report dict."""
    last_day = last_complete_day(t)
    first_day = last_day - timedelta(days=(BACKFILL_DAYS if backfill else REINGEST_DAYS) - 1)
    universe = instruments(cur)
    stocks = [s for s, c in universe.items() if c in ("stock", "etf")]
    cryptos = [s for s, c in universe.items() if c == "crypto"]

    upsert_calendar(cur, client.calendar(first_day, t.date() + timedelta(days=CALENDAR_AHEAD_DAYS)))
    # Only sessions that have closed by t: a running session's bar is never stored.
    cur.execute("SELECT max(date) FROM market_calendar WHERE close_at <= %s AND date <= %s",
                (t, last_day))
    last_session = cur.fetchone()[0]

    stock_bars, stock_rejected = ([], [])
    if last_session and last_session >= first_day:
        stock_bars, stock_rejected = client.stock_daily_bars(stocks, first_day, last_session)
    crypto_bars, crypto_issues = client.crypto_daily_bars(cryptos, first_day, last_day)
    fx_rows, fx_rejected = fx_fetch(first_day, last_day)

    return {
        "window": [first_day.isoformat(), last_day.isoformat()],
        "last_session": last_session.isoformat() if last_session else None,
        "bars": upsert_bars(cur, stock_bars + crypto_bars).as_dict(),
        "fx": upsert_fx(cur, fx_rows).as_dict(),
        "rejected": stock_rejected + fx_rejected,
        "crypto_issues": crypto_issues,
    }


def find_gaps(cur, t: datetime, since: date) -> dict[str, list[str]]:
    """Expected-but-missing rows from `since` up to what is complete at t.

    Stocks/ETFs: every closed session in market_calendar. Crypto: every day.
    FX: every ECB (TARGET) business day.
    """
    last_day = last_complete_day(t)
    universe = instruments(cur)
    cur.execute("SELECT date FROM market_calendar WHERE date BETWEEN %s AND %s AND close_at <= %s",
                (since, last_day, t))
    sessions = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT symbol, date FROM bars_daily WHERE date BETWEEN %s AND %s", (since, last_day))
    have: dict[str, set[date]] = {}
    for sym, d in cur.fetchall():
        have.setdefault(sym, set()).add(d)

    gaps: dict[str, list[str]] = {}
    all_days = {since + timedelta(days=i) for i in range((last_day - since).days + 1)}
    for sym, cls in universe.items():
        expected = all_days if cls == "crypto" else sessions
        missing = sorted(expected - have.get(sym, set()))
        if missing:
            gaps[sym] = [d.isoformat() for d in missing]

    cur.execute("SELECT date FROM fx_daily WHERE date BETWEEN %s AND %s", (since, last_day))
    fx_have = {r[0] for r in cur.fetchall()}
    fx_missing = [d.isoformat() for d in ecb_business_days(since, last_day) if d not in fx_have]
    if fx_missing:
        gaps["EURUSD"] = fx_missing
    return gaps
