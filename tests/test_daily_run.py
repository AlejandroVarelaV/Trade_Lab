"""End-to-end daily runs against a real Postgres with a fake market.

Covers: C benchmarks per (capital, cost profile), fills at the right window and
price, myinvestor fees, ETP accrual, hand reconciliation, the no-forward-fill
rule with self-repair, monthly rebalance, scenarios from YAML without code.
"""
from datetime import date, datetime, timedelta, timezone

import psycopg2
import pytest
import yaml

from tests.fake_market import FakeAlpaca, fake_fx, fx, price
from tradelab import config, settings
from tradelab.calendars import daily_run_time
from tradelab.jobs import daily

pytestmark = pytest.mark.db
UTC = timezone.utc
MON = date(2026, 9, 21)


@pytest.fixture()
def env(migrated_url, tmp_path, monkeypatch):
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)
    conn = psycopg2.connect(migrated_url)
    path = tmp_path / "scenarios.yaml"
    path.write_text(config.DEFAULT_PATH.read_text())
    market = FakeAlpaca()

    class Env:
        def run(self, d: date, **kw):
            return daily.run(conn, settings.load(), daily_run_time(d), client=market,
                             crypto_price=market.crypto_first_minute, fx_fetch=fake_fx,
                             scenarios_path=path, **kw)

        def q(self, sql, args=()):
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchall()

    e = Env()
    e.conn, e.market, e.path = conn, market, path
    yield e
    conn.close()


def _ledger_id(env, name):
    return env.q("SELECT id FROM scenarios WHERE name = %s AND active_to IS NULL", (name,))[0][0]


def test_first_run_creates_all_ledgers_and_c_orders(env):
    status, s = env.run(MON, backfill=True)
    assert status in ("ok", "warn"), s
    rows = env.q("SELECT kind, count(*) FROM scenarios WHERE active_to IS NULL GROUP BY kind")
    assert dict(rows) == {"strategy": 10, "benchmark": 4}
    assert env.q("SELECT count(DISTINCT config_hash) FROM scenarios")[0][0] == 14
    # 10 × (A, B) + 4 × C = 24 ledgers, each marked at capital on day one.
    eq = env.q("SELECT portfolio, count(*), bool_and(equity_eur = s.capital_eur) FROM equity_daily e"
               " JOIN scenarios s ON s.id = e.scenario_id GROUP BY portfolio ORDER BY 1")
    assert eq == [("A", 10, True), ("B", 10, True), ("C", 4, True)]
    orders = env.q("SELECT symbol, target_weight::float8, fill_window FROM orders"
                   " WHERE scenario_id = %s ORDER BY symbol", (_ledger_id(env, "bench_c200_myinvestor"),))
    assert orders == [
        ("BTC/USD", 0.2, datetime(2026, 9, 22, 12, 0, tzinfo=UTC)),    # next 12:00 UTC
        ("SPY", 0.8, datetime(2026, 9, 22, 13, 30, tzinfo=UTC)),       # next session open
    ]


def test_c_fills_and_reconciles_by_hand(env):
    env.run(MON, backfill=True)
    status, s = env.run(MON + timedelta(days=1))
    assert status in ("ok", "warn"), s
    sid = _ledger_id(env, "bench_c200_myinvestor")
    tue = MON + timedelta(days=1)
    fills = env.q("""SELECT symbol, side, qty::float8, price_usd::float8, ref_price_usd::float8,
                            fx_rate::float8, commission_eur::float8, fx_fee_eur::float8, fees_eur::float8,
                            filled_at
                     FROM fills WHERE scenario_id = %s ORDER BY filled_at""", (sid,))
    (btc, spy) = fills
    rate = fx(tue)
    # BTC: first 1-minute bar at 12:00 UTC, +5 bps slippage, 40 EUR → 1.00 EUR minimum, no FX fee.
    assert btc[0] == "BTC/USD" and btc[9] == datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    assert btc[4] == pytest.approx(price("BTC/USD", tue) * 0.998, abs=1e-4)
    assert btc[3] == pytest.approx(btc[4] * 1.0005, abs=1e-6)
    assert btc[2] * btc[3] / rate == pytest.approx(40.0, abs=1e-4)   # qty floored to 1e-10
    assert (btc[6], btc[7]) == (1.0, 0.0)
    # SPY (ETF): next session open, +2 bps, 1.00 EUR minimum, no FX fee; capped by cash.
    assert spy[0] == "SPY" and spy[9] == datetime(2026, 9, 22, 13, 30, tzinfo=UTC)
    assert spy[4] == pytest.approx(round(price("SPY", tue) * 0.999, 4))
    assert (spy[6], spy[7]) == (1.0, 0.0)

    # Hand reconciliation: equity = Σ qty × close_usd ÷ eurusd + cash.
    cash = 200.0 - sum(f[2] * f[3] / f[5] + f[8] for f in fills)
    btc_mv = btc[2] * price("BTC/USD", tue) / rate
    spy_mv = spy[2] * price("SPY", tue) / rate
    accrual = btc_mv * 0.015 / 365                       # one day of ETP fee on BTC
    ((eq, cash_db),) = env.q("SELECT equity_eur::float8, cash_eur::float8 FROM equity_daily"
                             " WHERE scenario_id = %s AND date = %s", (sid, tue))
    assert cash_db == pytest.approx(cash - accrual, abs=1e-4)
    assert eq == pytest.approx(btc_mv + spy_mv + cash - accrual, abs=1e-4)
    # Fully invested: only the day's ETP fee can take cash (slightly) below zero.
    assert cash_db >= -accrual - 1e-4
    ((acc,),) = env.q("SELECT amount_eur::float8 FROM accruals_daily WHERE scenario_id = %s"
                      " AND date = %s", (sid, tue))
    assert acc == pytest.approx(accrual, abs=1e-6)
    # zero_commission C: 0.25% crypto commission, no ETP fee.
    zid = _ledger_id(env, "bench_c200_zero_commission")
    ((zc,),) = env.q("SELECT commission_eur::float8 FROM fills WHERE scenario_id = %s"
                     " AND symbol = 'BTC/USD'", (zid,))
    assert zc == pytest.approx(0.10, abs=1e-4)
    assert env.q("SELECT count(*) FROM accruals_daily WHERE scenario_id = %s", (zid,))[0][0] == 0


def test_40_eur_us_stock_buy_through_the_fill_engine(env):
    env.run(MON, backfill=True)
    sid = _ledger_id(env, "c200_base_myinvestor")
    env.q("""INSERT INTO orders (scenario_id, portfolio, reason, symbol, target_weight,
                                 decided_at, fill_window)
             VALUES (%s, 'A', 'rebalance', 'AAPL', 0.2, %s, %s) RETURNING id""",
          (sid, daily_run_time(MON), datetime(2026, 9, 22, 13, 30, tzinfo=UTC)))
    env.conn.commit()
    env.run(MON + timedelta(days=1))
    ((qty, px, rate, comm, fxfee, fees),) = env.q(
        "SELECT qty::float8, price_usd::float8, fx_rate::float8, commission_eur::float8,"
        " fx_fee_eur::float8, fees_eur::float8 FROM fills WHERE scenario_id = %s AND portfolio = 'A'",
        (sid,))
    assert qty * px / rate == pytest.approx(40.0, abs=1e-4)   # 20% of 200 EUR
    assert (comm, fxfee, fees) == (3.00, 0.12, 3.12)


def test_missing_bar_is_not_filled_and_repairs_itself(env):
    env.run(MON, backfill=True)
    env.run(MON + timedelta(days=1))                          # C buys SPY + BTC on Tuesday
    wed = MON + timedelta(days=2)
    env.market.missing.add(("SPY", wed))                     # Wednesday's SPY bar is late
    status, s = env.run(wed)
    assert status == "error"                                  # latest mark missing → 🔴
    assert "SPY" in s["gaps"] and wed.isoformat() in s["gaps"]["SPY"]
    sid = _ledger_id(env, "bench_c200_myinvestor")
    assert env.q("SELECT count(*) FROM bars_daily WHERE symbol = 'SPY' AND date = %s", (wed,)) == [(0,)]
    assert env.q("SELECT count(*) FROM equity_daily WHERE scenario_id = %s AND date = %s",
                 (sid, wed)) == [(0,)]                        # NaN stays NaN: no row
    assert "SPY" in s["feature_nulls"]
    # A/B hold only cash, so they are still marked.
    pid = _ledger_id(env, "c200_base_myinvestor")
    assert env.q("SELECT count(*) FROM equity_daily WHERE scenario_id = %s AND date = %s",
                 (pid, wed)) == [(2,)]

    env.market.missing.clear()                                # the bar arrives a day late
    status, s = env.run(wed + timedelta(days=1))
    assert s["ingest"]["bars"]["inserted"] >= 1
    assert "SPY" not in s.get("gaps", {})
    assert env.q("SELECT count(*) FROM equity_daily WHERE scenario_id = %s AND date = %s",
                 (sid, wed)) == [(1,)]


def test_corrected_bar_is_upserted_and_marks_follow(env):
    env.run(MON, backfill=True)
    tue = MON + timedelta(days=1)
    env.run(tue)
    sid = _ledger_id(env, "bench_c200_myinvestor")
    before = env.q("SELECT equity_eur::float8 FROM equity_daily WHERE scenario_id = %s AND date = %s",
                   (sid, tue))[0][0]
    env.market.overrides[("SPY", tue)] = price("SPY", tue) + 10    # vendor correction
    status, s = env.run(tue + timedelta(days=1))
    assert f"SPY {tue}" in s["ingest"]["bars"]["changed"]
    ((close,),) = env.q("SELECT close::float8 FROM bars_daily WHERE symbol = 'SPY' AND date = %s", (tue,))
    assert close == pytest.approx(price("SPY", tue) + 10)
    after = env.q("SELECT equity_eur::float8 FROM equity_daily WHERE scenario_id = %s AND date = %s",
                  (sid, tue))[0][0]
    assert after > before


def test_weekend_marks_use_last_session_and_crypto_has_seven_bars(env):
    env.run(MON, backfill=True)
    env.run(MON + timedelta(days=7))                          # through next Monday
    sid = _ledger_id(env, "bench_c200_myinvestor")
    days = env.q("SELECT count(*) FROM equity_daily WHERE scenario_id = %s", (sid,))[0][0]
    assert days == 8                                          # every calendar day Mon..Mon
    week = (MON, MON + timedelta(days=6))
    counts = dict(env.q("SELECT symbol, count(*) FROM bars_daily WHERE date BETWEEN %s AND %s"
                        " AND symbol IN ('SPY', 'BTC/USD') GROUP BY symbol", week))
    assert counts == {"SPY": 5, "BTC/USD": 7}


def test_monthly_rebalance_on_first_run_of_month(env):
    env.run(date(2026, 9, 28), backfill=True)
    env.run(date(2026, 9, 29))
    env.run(date(2026, 9, 30))
    sid = _ledger_id(env, "bench_c200_myinvestor")
    assert env.q("SELECT count(*) FROM orders WHERE scenario_id = %s", (sid,)) == [(2,)]
    env.run(date(2026, 10, 1))
    assert env.q("SELECT count(*) FROM orders WHERE scenario_id = %s", (sid,)) == [(4,)]
    env.run(date(2026, 10, 2))
    assert env.q("SELECT status, count(*) FROM orders WHERE scenario_id = %s GROUP BY 1 ORDER BY 1",
                 (sid,))[0][0] in ("filled", "skipped")
    assert env.q("SELECT count(*) FROM orders WHERE scenario_id = %s AND status = 'pending'",
                 (sid,)) == [(0,)]


def test_scenario_added_to_yaml_creates_its_ledgers_with_no_code_change(env):
    env.run(MON, backfill=True)
    raw = yaml.safe_load(env.path.read_text())
    raw["scenarios"].append({"capital_eur": 2000, "risk_profile": "aggressive",
                             "cost_profile": "zero_commission"})
    env.path.write_text(yaml.safe_dump(raw, sort_keys=False))

    status, s = env.run(MON + timedelta(days=1))
    assert sorted(s["scenarios"]["created"]) == ["bench_c2000_zero_commission",
                                                 "c2000_aggressive_zero_commission"]
    tue = MON + timedelta(days=1)
    rows = env.q("""SELECT s.name, e.portfolio, e.equity_eur::float8 FROM equity_daily e
                    JOIN scenarios s ON s.id = e.scenario_id
                    WHERE s.capital_eur = 2000 AND e.date = %s ORDER BY 1, 2""", (tue,))
    assert rows == [("bench_c2000_zero_commission", "C", 2000.0),
                    ("c2000_aggressive_zero_commission", "A", 2000.0),
                    ("c2000_aggressive_zero_commission", "B", 2000.0)]
    assert env.q("SELECT count(*) FROM orders o JOIN scenarios s ON s.id = o.scenario_id"
                 " WHERE s.name = 'bench_c2000_zero_commission'") == [(2,)]


def test_changing_a_parameter_deactivates_and_recreates(env):
    env.run(MON, backfill=True)
    raw = yaml.safe_load(env.path.read_text())
    raw["risk_profiles"]["conservative"]["max_positions"] = 5
    env.path.write_text(yaml.safe_dump(raw, sort_keys=False))
    status, s = env.run(MON + timedelta(days=1))
    assert sorted(s["scenarios"]["deactivated"]) == sorted(s["scenarios"]["created"]) == [
        "c1000_conservative_myinvestor", "c200_conservative_myinvestor",
        "c500_conservative_myinvestor"]
    assert env.q("SELECT count(*) FROM scenarios WHERE name = 'c200_conservative_myinvestor'") == [(2,)]
    assert env.q("SELECT count(*) FROM scenarios WHERE active_to IS NULL") == [(14,)]


def test_time_only_moves_forward(env):
    env.run(MON + timedelta(days=1), backfill=True)
    with pytest.raises(SystemExit, match="time only moves forward"):
        env.run(MON)


def test_nan_cannot_be_stored(env):
    with pytest.raises(psycopg2.errors.CheckViolation):
        env.q("INSERT INTO bars_daily (symbol, date, open, high, low, close, volume, source)"
              " VALUES ('SPY', '2026-01-02', 1, 1, 1, 'NaN', 1, 'x')")
    env.conn.rollback()
    with pytest.raises(psycopg2.errors.CheckViolation):
        env.q("INSERT INTO fx_daily (date, eurusd, source) VALUES ('2026-01-02', 'NaN', 'x')")


def test_split_like_move_is_flagged(env):
    env.run(MON, backfill=True)
    tue = MON + timedelta(days=1)
    env.market.overrides[("NVDA", tue)] = price("NVDA", tue) / 2     # a 2:1 split in raw prices
    status, s = env.run(tue)
    assert status == "warn"
    assert any(m.startswith(f"NVDA {tue}") for m in s["big_moves"])
