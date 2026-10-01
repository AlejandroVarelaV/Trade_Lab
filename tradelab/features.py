"""Per-instrument features (SPEC §3.3), recomputed from bars_daily every run.

Closes are aligned on the *expected* dates (US sessions for stocks/ETFs, every
calendar day for crypto). A missing bar is a None in that series and is never
filled, so any feature whose window touches it is None too (NULL in the DB).
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from psycopg2.extras import execute_values

RETURN_HORIZONS = (1, 5, 21, 63)
VOL_WINDOW = 21
RSI_PERIOD = 14
VOLUME_Z_WINDOW = 20
ANNUALIZATION = {"stock": 252, "etf": 252, "crypto": 365}


def _window(xs: list[float | None], end: int, n: int) -> list[float] | None:
    """xs[end-n+1 .. end] if all present, else None."""
    if end - n + 1 < 0:
        return None
    w = xs[end - n + 1:end + 1]
    return None if any(v is None for v in w) else w  # type: ignore[return-value]


def _std(xs: list[float]) -> float:
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def wilder_rsi(closes: list[float | None], period: int = RSI_PERIOD) -> list[float | None]:
    """Wilder RSI. The smoothing restarts after a gap and needs period+1 closes."""
    out: list[float | None] = [None] * len(closes)
    avg_gain = avg_loss = None
    run: list[float] = []            # closes since the last gap
    for i, c in enumerate(closes):
        if c is None:
            run, avg_gain, avg_loss = [], None, None
            continue
        run.append(c)
        if len(run) < period + 1:
            continue
        if avg_gain is None:
            diffs = [run[j] - run[j - 1] for j in range(1, period + 1)]
            avg_gain = sum(max(d, 0) for d in diffs) / period
            avg_loss = sum(max(-d, 0) for d in diffs) / period
        else:
            d = run[-1] - run[-2]
            avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
            avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
        if avg_loss == 0:
            out[i] = 100.0 if avg_gain > 0 else 50.0
        else:
            out[i] = 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def compute(dates: list[date], closes: list[float | None], volumes: list[float | None],
            asset_class: str) -> list[dict]:
    """Features for every expected date that has a bar."""
    rsi = wilder_rsi(closes)
    ann = math.sqrt(ANNUALIZATION[asset_class])
    rows = []
    for i, (d, c) in enumerate(zip(dates, closes)):
        if c is None:
            continue
        row: dict = {"date": d, "close": c}
        for n in RETURN_HORIZONS:
            prev = closes[i - n] if i - n >= 0 else None
            row[f"ret_{n}d"] = None if prev is None else c / prev - 1
        w = _window(closes, i, VOL_WINDOW + 1)
        row["vol_21d"] = (_std([math.log(w[j] / w[j - 1]) for j in range(1, len(w))]) * ann
                          if w else None)
        row["rsi_14"] = rsi[i]
        for n in (50, 200):
            w = _window(closes, i, n)
            row[f"dist_ma{n}"] = c / (sum(w) / n) - 1 if w else None
        row["volume_z"] = None
        if asset_class != "crypto":
            past = _window(volumes, i - 1, VOLUME_Z_WINDOW)
            v = volumes[i]
            if past and v is not None:
                sd = _std(past)
                row["volume_z"] = (v - sum(past) / len(past)) / sd if sd > 0 else None
        rows.append(row)
    return rows


COLUMNS = ("close", "ret_1d", "ret_5d", "ret_21d", "ret_63d", "vol_21d", "rsi_14",
           "dist_ma50", "dist_ma200", "volume_z")


def recompute_all(cur, through: date) -> dict:
    """Rebuild features_daily for every active instrument up to `through`.

    Returns {symbol: [names of features that are NULL on its latest bar]}.
    """
    cur.execute("SELECT symbol, asset_class FROM instruments WHERE active_to IS NULL ORDER BY id")
    universe = cur.fetchall()
    cur.execute("SELECT date FROM market_calendar WHERE date <= %s ORDER BY date", (through,))
    sessions = [r[0] for r in cur.fetchall()]
    report: dict[str, list[str]] = {}
    for symbol, cls in universe:
        cur.execute("SELECT date, close::float8, volume::float8 FROM bars_daily"
                    " WHERE symbol = %s AND date <= %s ORDER BY date", (symbol, through))
        bars = {d: (c, v) for d, c, v in cur.fetchall()}
        if not bars:
            report[symbol] = ["no bars"]
            continue
        first = min(bars)
        if cls == "crypto":
            dates = [first + timedelta(days=i) for i in range((through - first).days + 1)]
        else:
            dates = [s for s in sessions if s >= first]
            # A bar on a date the calendar doesn't know is still kept, in order.
            dates = sorted(set(dates) | set(bars))
        closes = [bars[d][0] if d in bars else None for d in dates]
        volumes = [bars[d][1] if d in bars else None for d in dates]
        rows = compute(dates, closes, volumes, cls)
        cur.execute("DELETE FROM features_daily WHERE symbol = %s", (symbol,))
        execute_values(cur, f"INSERT INTO features_daily (symbol, date, {', '.join(COLUMNS)}) VALUES %s",
                       [(symbol, r["date"], *[r[k] for k in COLUMNS]) for r in rows])
        last = rows[-1]
        nulls = [k for k in COLUMNS if last[k] is None and not (k == "volume_z" and cls == "crypto")]
        if last["date"] != dates[-1]:
            nulls.insert(0, f"latest bar is {last['date']}, expected {dates[-1]}")
        if nulls:
            report[symbol] = nulls
    return report
