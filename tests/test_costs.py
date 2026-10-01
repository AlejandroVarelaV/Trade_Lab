"""Cost profiles from config/scenarios.yaml (SPEC §5)."""
import pytest

from tradelab import config

CFG = config.load()
MYINVESTOR = CFG.cost_profiles["myinvestor"]
ZERO = CFG.cost_profiles["zero_commission"]


def test_40_eur_us_stock_buy_pays_minimum_commission_plus_fx():
    cost = MYINVESTOR.trade_cost("stock", 40.0)
    assert cost.commission_eur == pytest.approx(3.00)   # the minimum, not 0.12% (0.048)
    assert cost.fx_fee_eur == pytest.approx(0.12)       # 0.30% FX conversion
    assert cost.total_eur == pytest.approx(3.12)


def test_40_eur_etf_buy_pays_one_euro_and_no_fx():
    cost = MYINVESTOR.trade_cost("etf", 40.0)
    assert cost.commission_eur == pytest.approx(1.00)
    assert cost.fx_fee_eur == 0.0


def test_40_eur_crypto_buy_pays_one_euro_and_no_fx():
    cost = MYINVESTOR.trade_cost("crypto", 40.0)
    assert cost.commission_eur == pytest.approx(1.00)
    assert cost.fx_fee_eur == 0.0


@pytest.mark.parametrize("cls", ["stock", "etf", "crypto"])
def test_large_trade_commission_is_capped_at_25(cls):
    # 0.12% of 50,000 EUR would be 60 EUR.
    assert MYINVESTOR.trade_cost(cls, 50_000.0).commission_eur == pytest.approx(25.00)


def test_fx_fee_is_not_capped():
    assert MYINVESTOR.trade_cost("stock", 50_000.0).fx_fee_eur == pytest.approx(150.0)


def test_mid_size_trade_pays_the_percentage():
    # 0.12% of 5,000 = 6.00, between the 3.00 minimum and the 25.00 maximum.
    assert MYINVESTOR.trade_cost("stock", 5_000.0).commission_eur == pytest.approx(6.00)


def test_sell_is_charged_like_a_buy():
    assert MYINVESTOR.trade_cost("stock", -40.0).total_eur == pytest.approx(3.12)


def test_zero_commission_profile():
    assert ZERO.trade_cost("stock", 40.0).total_eur == 0.0
    assert ZERO.trade_cost("etf", 50_000.0).total_eur == 0.0
    assert ZERO.trade_cost("crypto", 40.0).commission_eur == pytest.approx(0.10)  # 0.25%, no min
    assert ZERO.trade_cost("crypto", 1_000_000.0).commission_eur == pytest.approx(2_500.0)  # no max


def test_slippage_moves_price_against_us():
    assert MYINVESTOR.exec_price("stock", 100.0, "buy") == pytest.approx(100.02)   # 2 bps
    assert MYINVESTOR.exec_price("etf", 100.0, "sell") == pytest.approx(99.98)
    assert MYINVESTOR.exec_price("crypto", 100.0, "buy") == pytest.approx(100.05)  # 5 bps


def test_etp_fee_accrues_over_365_calendar_days():
    daily = MYINVESTOR.etp_daily_fee("crypto", 1000.0)
    assert daily == pytest.approx(1000.0 * 0.015 / 365)
    assert daily * 365 == pytest.approx(15.0)
    assert MYINVESTOR.etp_daily_fee("etf", 1000.0) == 0.0
    assert ZERO.etp_daily_fee("crypto", 1000.0) == 0.0


def test_max_affordable_notional_leaves_room_for_fees():
    n = MYINVESTOR.max_affordable_notional("stock", 100.0)
    assert n + MYINVESTOR.trade_cost("stock", n).total_eur == pytest.approx(100.0, abs=1e-6)
    assert n == pytest.approx((100.0 - 3.0) / 1.003, abs=1e-6)
    assert MYINVESTOR.max_affordable_notional("stock", 3.0) == 0.0
