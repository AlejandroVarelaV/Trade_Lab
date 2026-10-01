"""Fill engine, benchmark (C) rebalancing and daily mark-to-market.

Rules (SPEC §3.2, §4, §5), identical for A, B and C:
  - An order is decided at time t and fills at its fill window: stocks/ETFs at
    the next US session's open, crypto at the first 1-minute bar at or after
    12:00 UTC. Orders fill in the run that follows the fill day's 22:00 mark,
    when that day's ECB rate and bars are known.
  - Sizing: target_weight × equity at the fill window. Equity values the
    filled symbol at its fill price and every other position at the previous
    day's close. Sells run before buys; a buy is capped by available cash.
  - Trades under min_trade_eur are skipped (`below_min_size`), except a full
    close (target 0).
  - Ledger currency is EUR: price_eur = price_usd / eurusd (ECB, as of the day).

The ledger state is never stored as a running balance: cash and positions are
re-derived from `fills` (append-only) and the ETP fee accruals every run, so a
corrected bar repairs every snapshot that depends on it. A snapshot whose
inputs are missing is not written (no forward fill); weekends and holidays are
marked at the last session's close, since no new price exists.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Callable

from psycopg2.extras import execute_values

from tradelab.calendars import (UTC, crypto_day_bounds, daterange, last_complete_day,
                                last_ecb_business_day, next_crypto_fill_window)
from tradelab.config import Config
from tradelab.costs import CostProfile

EPS_QTY = 1e-9
CryptoPriceFn = Callable[[str, datetime, datetime], "tuple[datetime, float] | None"]


def _q(x: float, places: int, rounding=ROUND_HALF_UP) -> Decimal:
    return Decimal(repr(x)).quantize(Decimal(1).scaleb(-places), rounding=rounding)


# ─── Market data as known at time t (never later) ──────────────────────────

class Market:
    def __init__(self, cur, t: datetime):
        self.t = t
        self.last_day = last_complete_day(t)
        cur.execute("SELECT symbol, asset_class FROM instruments WHERE active_to IS NULL")
        self.asset_class: dict[str, str] = dict(cur.fetchall())
        cur.execute("SELECT date, open_at, close_at FROM market_calendar ORDER BY date")
        self.sessions = cur.fetchall()
        self.closed_sessions = [d for d, _, c in self.sessions if c <= t and d <= self.last_day]
        cur.execute("SELECT symbol, date, open::float8, close::float8 FROM bars_daily"
                    " WHERE date <= %s", (self.last_day,))
        self.bars: dict[str, dict[date, tuple[float, float]]] = {}
        for sym, d, o, c in cur.fetchall():
            self.bars.setdefault(sym, {})[d] = (o, c)
        cur.execute("SELECT date, eurusd::float8 FROM fx_daily WHERE date <= %s", (self.last_day,))
        self.fx: dict[date, float] = dict(cur.fetchall())

    def last_session(self, d: date) -> date | None:
        i = bisect.bisect_right(self.closed_sessions, d)
        return self.closed_sessions[i - 1] if i else None

    def close_usd(self, symbol: str, d: date) -> float | None:
        if self.asset_class[symbol] == "crypto":
            bar = self.bars.get(symbol, {}).get(d)
        else:
            s = self.last_session(d)
            bar = self.bars.get(symbol, {}).get(s) if s else None
        return bar[1] if bar else None

    def fx_asof(self, d: date) -> float | None:
        """The ECB rate in force on d (last TARGET business day ≤ d)."""
        return self.fx.get(last_ecb_business_day(d))

    def close_eur(self, symbol: str, d: date) -> float | None:
        c, f = self.close_usd(symbol, d), self.fx_asof(d)
        return None if c is None or f is None else c / f

    def fill_window(self, symbol: str, decided_at: datetime) -> datetime | None:
        if self.asset_class[symbol] == "crypto":
            return next_crypto_fill_window(decided_at)
        for _, open_at, _ in self.sessions:
            if open_at > decided_at:
                return open_at
        return None

    def stock_open(self, symbol: str, window: datetime) -> float | None:
        for d, open_at, close_at in self.sessions:
            if open_at == window:
                if close_at > self.t:
                    return None
                bar = self.bars.get(symbol, {}).get(d)
                return bar[0] if bar else None
        return None


# ─── Ledgers ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Ledger:
    scenario_id: int
    name: str
    portfolio: str
    capital: float
    cost: CostProfile
    active_from: datetime

    @property
    def start_date(self) -> date:
        return last_complete_day(self.active_from)

    @property
    def key(self) -> tuple[int, str]:
        return self.scenario_id, self.portfolio


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str
    qty: float
    price_usd: float
    fx_rate: float
    fees_eur: float
    filled_at: datetime

    @property
    def cash_flow(self) -> float:
        gross = self.qty * self.price_usd / self.fx_rate
        return -(gross + self.fees_eur) if self.side == "buy" else gross - self.fees_eur


def active_ledgers(cur, cfg: Config) -> list[Ledger]:
    cur.execute("""SELECT id, name, kind, capital_eur::float8, cost_profile, active_from
                   FROM scenarios WHERE active_to IS NULL ORDER BY id""")
    out = []
    for sid, name, kind, capital, cost, active_from in cur.fetchall():
        for pf in (("A", "B") if kind == "strategy" else ("C",)):
            out.append(Ledger(sid, name, pf, capital, cfg.cost_profiles[cost], active_from))
    return out


def load_fills(cur, ledgers: list[Ledger]) -> dict[tuple[int, str], list[Fill]]:
    fills: dict[tuple[int, str], list[Fill]] = {lg.key: [] for lg in ledgers}
    cur.execute("""SELECT scenario_id, portfolio, symbol, side, qty::float8, price_usd::float8,
                          fx_rate::float8, fees_eur::float8, filled_at
                   FROM fills WHERE fill_source = 'internal' ORDER BY filled_at, id""")
    for sid, pf, *rest in cur.fetchall():
        if (sid, pf) in fills:
            fills[(sid, pf)].append(Fill(*rest))
    return fills


@dataclass
class DayMark:
    date: date
    cash: float | None
    positions: dict[str, float]
    cost_basis: dict[str, float]
    values: dict[str, float | None]
    accruals: dict[str, tuple[float, float]] = field(default_factory=dict)

    @property
    def equity(self) -> float | None:
        if self.cash is None or any(v is None for v in self.values.values()):
            return None
        return self.cash + sum(self.values.values())  # type: ignore[arg-type]


def walk(ledger: Ledger, fills: list[Fill], market: Market, through: date) -> list[DayMark]:
    """Replay the ledger day by day from its start date to `through`.

    cash becomes None for good once an ETP accrual can't be computed (its base
    price is missing), because every later balance depends on it.
    """
    cash: float | None = ledger.capital
    pos: dict[str, float] = {}
    basis: dict[str, float] = {}
    marks = []
    i = 0
    for d in daterange(ledger.start_date, through):
        while i < len(fills) and fills[i].filled_at.astimezone(UTC).date() <= d:
            f = fills[i]
            if cash is not None:
                cash += f.cash_flow
            q0 = pos.get(f.symbol, 0.0)
            if f.side == "buy":
                pos[f.symbol] = q0 + f.qty
                basis[f.symbol] = basis.get(f.symbol, 0.0) + f.qty * f.price_usd / f.fx_rate
            else:
                q1 = q0 - f.qty
                basis[f.symbol] = basis.get(f.symbol, 0.0) * (q1 / q0 if q0 > 0 else 0.0)
                pos[f.symbol] = q1
                if abs(q1) < EPS_QTY:
                    pos.pop(f.symbol)
                    basis.pop(f.symbol, None)
            i += 1
        values: dict[str, float | None] = {}
        for sym, q in pos.items():
            px = market.close_eur(sym, d)
            values[sym] = None if px is None else q * px
        mark = DayMark(d, cash, dict(pos), dict(basis), values)
        if cash is not None:
            for sym, v in values.items():
                cls = market.asset_class[sym]
                if cls != "crypto" or ledger.cost.etp_annual_fee_pct == 0:
                    continue
                if v is None:
                    cash = None
                    break
                fee = ledger.cost.etp_daily_fee(cls, v)
                mark.accruals[sym] = (v, fee)
                cash -= fee
            mark.cash = cash
        marks.append(mark)
    return marks


def cash_at(ledger: Ledger, fills: list[Fill], market: Market, instant: datetime) -> float | None:
    """Cash just before `instant`: fills strictly before it, accruals of earlier days."""
    day = instant.astimezone(UTC).date()
    marks = walk(ledger, [f for f in fills if f.filled_at.astimezone(UTC).date() < day],
                 market, day - timedelta(days=1))
    cash = marks[-1].cash if marks else ledger.capital
    if cash is None:
        return None
    return cash + sum(f.cash_flow for f in fills
                      if f.filled_at.astimezone(UTC).date() == day and f.filled_at < instant)


def positions_at(fills: list[Fill], instant: datetime) -> dict[str, float]:
    pos: dict[str, float] = {}
    for f in fills:
        if f.filled_at < instant:
            pos[f.symbol] = pos.get(f.symbol, 0.0) + (f.qty if f.side == "buy" else -f.qty)
    return {s: q for s, q in pos.items() if abs(q) >= EPS_QTY}


# ─── Fill engine ───────────────────────────────────────────────────────────

def execute_pending(cur, ledgers: list[Ledger], market: Market, cfg: Config,
                    crypto_price: CryptoPriceFn) -> dict:
    """Fill every pending order whose window has passed and whose inputs exist."""
    by_key = {lg.key: lg for lg in ledgers}
    cur.execute("""SELECT id, scenario_id, portfolio, symbol, target_weight::float8, fill_window
                   FROM orders WHERE status = 'pending' AND fill_window <= %s
                   ORDER BY scenario_id, portfolio, fill_window, id""", (market.t,))
    pending = cur.fetchall()
    fills = load_fills(cur, ledgers)
    report = {"filled": [], "skipped": [], "waiting": []}

    groups: dict[tuple[int, str, datetime], list[tuple]] = {}
    for row in pending:
        groups.setdefault((row[1], row[2], row[5]), []).append(row)

    for (sid, pf, window), orders in groups.items():
        ledger = by_key.get((sid, pf))
        if ledger is None:
            report["waiting"].extend(f"order {o[0]}: scenario {sid} inactive" for o in orders)
            continue
        if window.astimezone(UTC).date() > market.last_day:
            continue    # fills once the fill day's mark and ECB rate are known
        _fill_group(cur, ledger, window, orders, fills[ledger.key], market, cfg,
                    crypto_price, report)
    return report


def _fill_group(cur, ledger, window, orders, fills, market, cfg, crypto_price, report):
    day = window.astimezone(UTC).date()
    fx = market.fx_asof(day)
    ref: dict[str, tuple[datetime, float]] = {}
    for oid, _, _, sym, _, _ in orders:
        if market.asset_class[sym] == "crypto":
            _, until = crypto_day_bounds(day)
            got = crypto_price(sym, window, until)
        else:
            px = market.stock_open(sym, window)
            got = (window, px) if px is not None else None
        if got:
            ref[sym] = got
    cash = cash_at(ledger, fills, market, window)
    pos = positions_at(fills, window)
    values = {}
    for sym, q in pos.items():
        if sym in ref and fx:
            values[sym] = q * ref[sym][1] / fx
        else:
            values[sym] = market.close_eur(sym, day - timedelta(days=1))
            values[sym] = None if values[sym] is None else q * values[sym]
    missing = []
    if fx is None:
        missing.append(f"EURUSD {day}")
    if cash is None:
        missing.append("cash (ETP accrual base missing)")
    missing += [f"{s} fill price" for _, _, _, s, _, _ in orders if s not in ref]
    missing += [f"{s} prior close" for s, v in values.items() if v is None]
    if missing:
        report["waiting"].extend(f"{ledger.name}/{ledger.portfolio} order {o[0]} {o[3]}: missing "
                                 + ", ".join(missing) for o in orders)
        return

    equity = cash + sum(values.values())
    plans = []
    for oid, _, _, sym, tw, _ in orders:
        ref_eur = ref[sym][1] / fx
        current = pos.get(sym, 0.0) * ref_eur
        plans.append((oid, sym, tw, tw * equity - current))
    plans.sort(key=lambda p: p[3])          # sells (negative delta) first

    for oid, sym, tw, delta in plans:
        cls = market.asset_class[sym]
        ts, ref_usd = ref[sym]
        held = pos.get(sym, 0.0)
        full_close = tw == 0 and held > EPS_QTY
        note = None
        if abs(delta) < cfg.min_trade_eur and not full_close:
            _resolve(cur, oid, "skipped", "below_min_size", market.t)
            report["skipped"].append(f"{ledger.name}/{ledger.portfolio} {sym} below_min_size "
                                     f"(delta {delta:+.2f} EUR)")
            continue
        if delta < 0:
            side = "sell"
            exec_usd = ledger.cost.exec_price(cls, ref_usd, "sell")
            qty = held if full_close else min(held, -delta * fx / ref_usd)
            qty_d = _q(qty, 10, ROUND_DOWN)
        else:
            side = "buy"
            exec_usd = ledger.cost.exec_price(cls, ref_usd, "buy")
            notional = min(delta, ledger.cost.max_affordable_notional(cls, cash))
            if notional < cfg.min_trade_eur:
                _resolve(cur, oid, "skipped", "insufficient_cash", market.t)
                report["skipped"].append(f"{ledger.name}/{ledger.portfolio} {sym} insufficient_cash")
                continue
            if notional < delta - 0.005:
                note = f"cash_limited: {notional:.2f} of {delta:.2f} EUR"
            qty_d = _q(notional * fx / exec_usd, 10, ROUND_DOWN)
        price_d, fx_d = _q(exec_usd, 8), _q(fx, 6)
        gross = float(qty_d * price_d / fx_d)
        tc = ledger.cost.trade_cost(cls, gross)
        comm_d, fxfee_d = _q(tc.commission_eur, 4), _q(tc.fx_fee_eur, 4)
        cur.execute("""
            INSERT INTO fills (scenario_id, portfolio, proposal_id, fill_reason, symbol, side, qty,
                               price_usd, fx_rate, fees_eur, filled_at, fill_source, order_id,
                               ref_price_usd, commission_eur, fx_fee_eur)
            VALUES (%s, %s, NULL, 'rebalance', %s, %s, %s, %s, %s, %s, %s, 'internal', %s,
                    %s, %s, %s)""",
            (ledger.scenario_id, ledger.portfolio, sym, side, qty_d, price_d, fx_d,
             comm_d + fxfee_d, ts, oid, _q(ref_usd, 8), comm_d, fxfee_d))
        _resolve(cur, oid, "filled", note, market.t)
        f = Fill(sym, side, float(qty_d), float(price_d), float(fx_d), float(comm_d + fxfee_d), ts)
        fills.append(f)
        fills.sort(key=lambda x: x.filled_at)
        cash += f.cash_flow
        pos[sym] = held + (f.qty if side == "buy" else -f.qty)
        report["filled"].append(f"{ledger.name}/{ledger.portfolio} {side} {f.qty:.6f} {sym} "
                                f"@ {f.price_usd:.2f} USD, fees {f.fees_eur:.2f} EUR"
                                + (f" ({note})" if note else ""))


def _resolve(cur, order_id: int, status: str, note: str | None, t: datetime) -> None:
    cur.execute("UPDATE orders SET status = %s, status_note = %s, resolved_at = %s WHERE id = %s",
                (status, note, t, order_id))


# ─── Benchmark C ───────────────────────────────────────────────────────────

def decide_rebalances(cur, ledgers: list[Ledger], market: Market, cfg: Config) -> list[str]:
    """Monthly rebalance of every C ledger: at inception and on the first run of a month."""
    t = market.t
    decided = []
    for lg in ledgers:
        if lg.portfolio != "C":
            continue
        cur.execute("""SELECT max(decided_at), count(*) FILTER (WHERE status = 'pending')
                       FROM orders WHERE scenario_id = %s AND portfolio = 'C'
                       AND reason = 'rebalance'""", (lg.scenario_id,))
        last, n_pending = cur.fetchone()
        if n_pending or (last is not None and (last.year, last.month) == (t.year, t.month)):
            continue
        for sym, w in cfg.benchmark_weights.items():
            window = market.fill_window(sym, t)
            if window is None:
                raise RuntimeError(f"no future session in market_calendar after {t}")
            cur.execute("""INSERT INTO orders (scenario_id, portfolio, reason, symbol, target_weight,
                                               decided_at, fill_window)
                           VALUES (%s, 'C', 'rebalance', %s, %s, %s, %s)""",
                        (lg.scenario_id, sym, w, t, window))
        decided.append(lg.name)
    return decided


# ─── Mark-to-market ────────────────────────────────────────────────────────

def mark_all(cur, ledgers: list[Ledger], market: Market) -> dict:
    """Rewrite the daily snapshots of every active ledger through market.last_day."""
    fills = load_fills(cur, ledgers)
    report = {"ledgers": 0, "unmarked": {}, "latest": {}}
    for lg in ledgers:
        marks = walk(lg, fills[lg.key], market, market.last_day)
        for table in ("positions_daily", "equity_daily", "accruals_daily"):
            cur.execute(f"DELETE FROM {table} WHERE scenario_id = %s AND portfolio = %s",
                        (lg.scenario_id, lg.portfolio))
        peak = None
        eq_rows, pos_rows, acc_rows, unmarked = [], [], [], []
        for m in marks:
            eq = m.equity
            if eq is None:
                unmarked.append(m.date.isoformat())
                continue
            peak = eq if peak is None else max(peak, eq)
            dd = 0.0 if peak <= 0 else max(0.0, 1 - eq / peak)
            eq_rows.append((lg.scenario_id, lg.portfolio, m.date, round(eq, 4), round(m.cash, 4),
                            round(dd, 6)))
            for sym, q in m.positions.items():
                pos_rows.append((lg.scenario_id, lg.portfolio, m.date, sym, q,
                                 m.cost_basis[sym] / q, round(m.values[sym], 4), None))
            for sym, (base, amount) in m.accruals.items():
                acc_rows.append((lg.scenario_id, lg.portfolio, m.date, sym, "etp_fee",
                                 round(base, 4), round(amount, 6)))
        if eq_rows:
            execute_values(cur, "INSERT INTO equity_daily (scenario_id, portfolio, date, equity_eur,"
                                " cash_eur, drawdown) VALUES %s", eq_rows)
        if pos_rows:
            execute_values(cur, "INSERT INTO positions_daily (scenario_id, portfolio, date, symbol,"
                                " qty, avg_price_eur, market_value_eur, stop_price) VALUES %s",
                           pos_rows)
        if acc_rows:
            execute_values(cur, "INSERT INTO accruals_daily (scenario_id, portfolio, date, symbol,"
                                " kind, base_eur, amount_eur) VALUES %s", acc_rows)
        report["ledgers"] += 1
        if unmarked:
            report["unmarked"][f"{lg.name}/{lg.portfolio}"] = unmarked
        if eq_rows:
            report["latest"][f"{lg.name}/{lg.portfolio}"] = [eq_rows[-1][2].isoformat(), eq_rows[-1][3]]
    return report
