"""Cost profiles from config/scenarios.yaml (SPEC §5)."""
import copy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

from tradelab import config
from tradelab.costs import cost_profile_from_config
from tradelab.ledger import CASH_SYMBOL, Fill, Ledger, _free_orders_used, _fx_converted_eur, walk

CFG = config.load()
P = CFG.cost_profiles
IBKR, TR, REVOLUT = P["ibkr"], P["trade_republic"], P["revolut_standard"]
MYINVESTOR, ZERO = P["myinvestor"], P["zero_commission"]
FX = 1.10          # EUR/USD used in the SPEC §5 worked example
AAPL_USD = 200.0   # any price under ~100 shares leaves IBKR at its minimum
UTC = timezone.utc


def cost(profile, cls, notional, **kw):
    return profile.trade_cost(cls, notional, fx_rate=FX, price_usd=AAPL_USD, **kw)


# ─── SPEC §5 worked example: a 40 EUR US-stock buy, every profile ──────────

FORTY_EUR_STOCK = {                  # (commission EUR, FX fee EUR), at EUR/USD 1.10
    "ibkr":             (0.35 / 1.10, 0.012),   # USD 0.35 minimum; 0.03% FX
    "trade_republic":   (1.00, 0.14),           # EUR 1 flat; 0.35% FX
    "revolut_standard": (1.00, 0.00),           # paid order: max(0.25%, EUR 1); FX within allowance
    "myinvestor":       (3.00, 0.12),           # EUR 3 minimum, not 0.12%; 0.30% FX
    "zero_commission":  (0.00, 0.00),
}


def test_worked_example_covers_every_profile():
    assert set(FORTY_EUR_STOCK) == set(P)


@pytest.mark.parametrize("name", sorted(FORTY_EUR_STOCK))
def test_40_eur_us_stock_buy_costs_what_spec_section_5_says(name):
    commission, fx_fee = FORTY_EUR_STOCK[name]
    c = cost(P[name], "stock", 40.0)
    assert c.commission_eur == pytest.approx(commission, abs=1e-9)
    assert c.fx_fee_eur == pytest.approx(fx_fee, abs=1e-9)
    assert c.total_eur == pytest.approx(commission + fx_fee, abs=1e-9)


def test_spec_rounded_totals():
    totals = {n: round(cost(P[n], "stock", 40.0).total_eur, 2) for n in P}
    assert totals == {"ibkr": 0.33, "trade_republic": 1.14, "revolut_standard": 1.00,
                      "myinvestor": 3.12, "zero_commission": 0.0}
    assert round(cost(REVOLUT, "stock", 40.0, free=True).total_eur, 2) == 0.00


# ─── Fee schedule mechanics ─────────────────────────────────────────────────

def test_ibkr_max_pct_wins_over_minimum_on_a_tiny_trade():
    # 20 EUR = 22 USD; 1% of it (0.22 USD) is below the 0.35 USD minimum.
    assert cost(IBKR, "stock", 20.0).commission_eur == pytest.approx(0.22 / FX)


def test_ibkr_per_share_fee_above_the_minimum():
    # 10,000 EUR = 11,000 USD of a 10 USD stock = 1,100 shares × 0.0035 = 3.85 USD,
    # above the 0.35 minimum and below the 1% maximum (110 USD).
    c = IBKR.trade_cost("stock", 10_000.0, fx_rate=FX, price_usd=10.0)
    assert c.commission_eur == pytest.approx(3.85 / FX)


def test_usd_fee_is_converted_at_the_fills_rate():
    assert IBKR.trade_cost("stock", 40.0, fx_rate=1.25, price_usd=AAPL_USD).commission_eur == \
        pytest.approx(0.35 / 1.25)


def test_per_share_fee_needs_a_price():
    with pytest.raises(ValueError, match="price_usd"):
        IBKR.trade_cost("stock", 40.0, fx_rate=FX)


@pytest.mark.parametrize("cls", ["etf", "crypto"])
def test_ibkr_xetra_products(cls):
    assert cost(IBKR, cls, 40.0).total_eur == pytest.approx(1.25)        # minimum, no FX
    assert cost(IBKR, cls, 10_000.0).commission_eur == pytest.approx(5.00)   # 0.05%
    assert cost(IBKR, cls, 100_000.0).commission_eur == pytest.approx(29.00)


@pytest.mark.parametrize("cls", ["stock", "etf", "crypto"])
def test_trade_republic_is_one_euro_flat(cls):
    assert cost(TR, cls, 40.0).commission_eur == 1.00
    assert cost(TR, cls, 50_000.0).commission_eur == 1.00


def test_revolut_percentage_above_the_minimum():
    assert cost(REVOLUT, "stock", 1_000.0).commission_eur == pytest.approx(2.50)


def test_free_order_pays_no_commission_but_pays_fx():
    c = cost(REVOLUT, "stock", 40.0, free=True, fx_used_eur=1_000.0)   # allowance used up
    assert (c.commission_eur, c.fx_fee_eur) == (0.0, pytest.approx(0.20))
    assert REVOLUT.is_free_eligible("stock") and REVOLUT.is_free_eligible("etf")
    assert not REVOLUT.is_free_eligible("crypto")
    assert not IBKR.is_free_eligible("stock")


@pytest.mark.parametrize("cls", ["stock", "etf", "crypto"])
def test_myinvestor_large_trade_commission_is_capped_at_25(cls):
    assert cost(MYINVESTOR, cls, 50_000.0).commission_eur == pytest.approx(25.00)


def test_fx_fee_is_not_capped():
    assert cost(MYINVESTOR, "stock", 50_000.0).fx_fee_eur == pytest.approx(150.0)


def test_sell_is_charged_like_a_buy():
    assert cost(MYINVESTOR, "stock", -40.0).total_eur == pytest.approx(3.12)


def test_zero_commission_profile():
    assert cost(ZERO, "stock", 40.0).total_eur == 0.0
    assert cost(ZERO, "etf", 50_000.0).total_eur == 0.0
    assert cost(ZERO, "crypto", 40.0).commission_eur == pytest.approx(0.10)          # 0.25%, no min
    assert cost(ZERO, "crypto", 1_000_000.0).commission_eur == pytest.approx(2_500.0)  # no max


# ─── Fractional flags, slippage + spread, ETP fee, interest ────────────────

def test_fractional_flags():
    assert MYINVESTOR.fractional == {"stock": False, "etf": True, "crypto": True}
    for name in ("ibkr", "trade_republic", "revolut_standard", "zero_commission"):
        assert all(P[name].fractional.values()), name


def test_slippage_moves_price_against_us():
    assert MYINVESTOR.exec_price("stock", 100.0, "buy") == pytest.approx(100.02)   # 2 bps
    assert MYINVESTOR.exec_price("etf", 100.0, "sell") == pytest.approx(99.98)
    assert MYINVESTOR.exec_price("crypto", 100.0, "buy") == pytest.approx(100.05)  # 5 bps


def test_spread_is_added_to_slippage():
    assert TR.exec_price("crypto", 100.0, "buy") == pytest.approx(101.05)    # 5 + 100 bps
    assert TR.exec_price("crypto", 100.0, "sell") == pytest.approx(98.95)
    assert TR.exec_price("stock", 100.0, "buy") == pytest.approx(100.02)


def test_etp_fee_accrues_over_365_calendar_days():
    daily = MYINVESTOR.etp_daily_fee("crypto", 1000.0)
    assert daily == pytest.approx(1000.0 * 0.015 / 365)
    assert daily * 365 == pytest.approx(15.0)
    assert MYINVESTOR.etp_daily_fee("etf", 1000.0) == 0.0
    assert ZERO.etp_daily_fee("crypto", 1000.0) == 0.0
    assert TR.etp_daily_fee("crypto", 1000.0) == 0.0     # crypto held directly


def test_daily_interest_on_positive_cash_only():
    assert TR.daily_interest(1000.0) == pytest.approx(1000.0 * 0.023 / 365)
    assert TR.daily_interest(-5.0) == 0.0
    assert IBKR.daily_interest(1000.0) == 0.0


def test_interest_accrues_over_a_weekend_on_calendar_days():
    # Ledger starts with Thursday 1 Oct 2026's mark (at capital, no interest);
    # then Fri, Sat, Sun and Mon each accrue one day.
    thu = date(2026, 10, 1)
    lg = Ledger(1, "c200_base_trade_republic", "A", 200.0, TR,
                datetime(2026, 10, 1, 22, 30, tzinfo=UTC))
    marks = walk(lg, [], SimpleNamespace(asset_class={}), thu + timedelta(days=4))
    assert [m.date.weekday() for m in marks] == [3, 4, 5, 6, 0]
    assert marks[0].accruals == [] and marks[0].cash == 200.0
    daily = 0.023 / 365
    cash = 200.0
    for m in marks[1:]:                             # one accrual per calendar day, compounding
        ((sym, kind, base, amount),) = m.accruals
        assert (sym, kind) == (CASH_SYMBOL, "interest")
        assert base == pytest.approx(cash)
        assert amount == pytest.approx(cash * daily)
        cash += amount
        assert m.cash == pytest.approx(cash)
    assert marks[-1].cash == pytest.approx(200.0 * (1 + daily) ** 4)
    assert marks[-1].equity == pytest.approx(200.0 * (1 + daily) ** 4)


def test_no_interest_where_not_modeled():
    lg = Ledger(1, "x", "A", 200.0, IBKR, datetime(2026, 10, 2, 22, 30, tzinfo=UTC))
    marks = walk(lg, [], SimpleNamespace(asset_class={}), date(2026, 10, 5))
    assert all(m.accruals == [] and m.cash == 200.0 for m in marks)


# ─── Free-order counter ─────────────────────────────────────────────────────

def _fill(sym, ts):
    return Fill(sym, "buy", 1.0, 100.0, 1.1, 0.0, ts)


def test_free_order_counter_resets_each_calendar_month():
    market = SimpleNamespace(asset_class={"AAPL": "stock", "SPY": "etf", "BTC/USD": "crypto"})
    sep30 = datetime(2026, 9, 30, 13, 30, tzinfo=UTC)
    oct1 = datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
    fills = [_fill("AAPL", datetime(2026, 9, 29, 13, 30, tzinfo=UTC)),
             _fill("BTC/USD", datetime(2026, 9, 29, 12, 0, tzinfo=UTC))]   # crypto: not counted
    assert _free_orders_used(fills, market, REVOLUT, sep30) == 1           # September used up
    assert _free_orders_used(fills, market, REVOLUT, oct1) == 0            # October starts fresh
    fills.append(_fill("SPY", oct1))
    assert _free_orders_used(fills, market, REVOLUT, oct1 + timedelta(days=1)) == 1
    # Month boundary is UTC: 23:59 UTC on 30 Sep is still September.
    assert _free_orders_used(fills, market, REVOLUT,
                             datetime(2026, 9, 30, 23, 59, tzinfo=UTC)) == 1


# ─── FX allowance (revolut_standard: 1,000 EUR/month free, then 0.5%) ───────

def test_revolut_fx_allowance_and_rates():
    assert REVOLUT.fx_free_eur_per_month == 1_000.0
    assert REVOLUT.fx_fee_pct == {"stock": 0.005, "etf": 0.0, "crypto": 0.0}
    assert REVOLUT.converts("stock") and not REVOLUT.converts("etf")
    assert IBKR.fx_free_eur_per_month == 0.0


def test_fx_fee_applies_only_above_the_allowance():
    assert cost(REVOLUT, "stock", 1_000.0).fx_fee_eur == 0.0
    assert cost(REVOLUT, "stock", 1_200.0).fx_fee_eur == pytest.approx(0.005 * 200)
    assert cost(REVOLUT, "stock", 300.0, fx_used_eur=900.0).fx_fee_eur == pytest.approx(0.005 * 200)
    assert cost(REVOLUT, "stock", 300.0, fx_used_eur=1_500.0).fx_fee_eur == pytest.approx(0.005 * 300)
    assert cost(IBKR, "stock", 40.0, fx_used_eur=0.0).fx_fee_eur == pytest.approx(0.012)   # no allowance


def _stock_fill(sym, eur, ts, side="buy"):
    return Fill(sym, side, eur * FX / AAPL_USD, AAPL_USD, FX, 0.0, ts)


def test_1200_eur_of_us_stock_conversions_pay_on_200_and_reset_next_month():
    market = SimpleNamespace(asset_class={"AAPL": "stock", "MSFT": "stock", "SPY": "etf",
                                          "BTC/USD": "crypto"})
    oct5 = datetime(2026, 10, 5, 13, 30, tzinfo=UTC)
    # ETF and crypto fills don't convert, so they don't use the allowance.
    fills = [_stock_fill("SPY", 500.0, oct5), _fill("BTC/USD", oct5)]
    fees = []
    for i, (sym, side) in enumerate([("AAPL", "buy"), ("MSFT", "buy"), ("AAPL", "sell")]):
        ts = oct5 + timedelta(days=i)
        used = _fx_converted_eur(fills, market, REVOLUT, ts)
        fees.append(cost(REVOLUT, "stock", 400.0, fx_used_eur=used).fx_fee_eur)
        fills.append(_stock_fill(sym, 400.0, ts, side))
    # 1,200 EUR converted in October (a sell converts too): only the last 200 EUR pays 0.5%.
    assert fees == [0.0, 0.0, pytest.approx(0.005 * 200)]
    assert _fx_converted_eur(fills, market, REVOLUT, oct5 + timedelta(days=3)) == pytest.approx(1_200.0)
    # November starts with the full allowance again.
    nov2 = datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
    assert _fx_converted_eur(fills, market, REVOLUT, nov2) == 0.0
    assert cost(REVOLUT, "stock", 400.0, fx_used_eur=0.0).fx_fee_eur == 0.0


# ─── Sizing and validation ──────────────────────────────────────────────────

def test_max_affordable_notional_leaves_room_for_fees():
    n = MYINVESTOR.max_affordable_notional("stock", 100.0, fx_rate=FX, price_usd=AAPL_USD)
    assert n + cost(MYINVESTOR, "stock", n).total_eur == pytest.approx(100.0, abs=1e-6)
    assert n == pytest.approx((100.0 - 3.0) / 1.003, abs=1e-6)
    assert MYINVESTOR.max_affordable_notional("stock", 3.0, fx_rate=FX, price_usd=AAPL_USD) == 0.0


def test_max_affordable_notional_with_a_free_order():
    n = REVOLUT.max_affordable_notional("stock", 100.0, fx_rate=FX, price_usd=AAPL_USD, free=True)
    assert n == pytest.approx(100.0, abs=1e-6)                     # no commission, FX allowance
    n = REVOLUT.max_affordable_notional("stock", 100.0, fx_rate=FX, price_usd=AAPL_USD, free=True,
                                        fx_used_eur=1_000.0)
    assert n == pytest.approx(100.0 / 1.005, abs=1e-6)


RAW = yaml.safe_load(config.DEFAULT_PATH.read_text())["cost_profiles"]


@pytest.mark.parametrize("mutate, match", [
    (lambda r: r["fractional"].update(stock="no"), "true/false"),
    (lambda r: r.pop("spread_bps"), "spread_bps missing"),
    (lambda r: r["commission"]["stock"].update(currency="GBP"), "invalid commission"),
    (lambda r: r["commission"]["stock"].update(per_shre=0.01), "unknown keys"),
    (lambda r: r.pop("free_order_classes"), "free_order_classes"),
    (lambda r: r.update(cash_interest_annual_pct=-0.01), "cash_interest"),
    (lambda r: r.update(fx_free_eur_per_month=-1), "fx_free_eur_per_month"),
])
def test_invalid_cost_profile_is_rejected(mutate, match):
    raw = copy.deepcopy(RAW["revolut_standard"])
    mutate(raw)
    with pytest.raises(ValueError, match=match):
        cost_profile_from_config("revolut_standard", raw)
