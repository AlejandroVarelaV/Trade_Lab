"""Feature math (SPEC §3.3) and the no-forward-fill rule."""
import math
from datetime import date, timedelta

import pytest

from tradelab import features


def _dates(n):
    return [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]


def test_returns_and_moving_average_distance():
    closes = [100.0 + i for i in range(250)]
    rows = features.compute(_dates(250), closes, [1.0] * 250, "crypto")
    last = rows[-1]
    assert last["close"] == 349.0
    assert last["ret_1d"] == pytest.approx(349 / 348 - 1)
    assert last["ret_63d"] == pytest.approx(349 / 286 - 1)
    assert last["dist_ma50"] == pytest.approx(349 / (sum(range(300, 350)) / 50) - 1)
    assert last["dist_ma200"] == pytest.approx(349 / (sum(range(150, 350)) / 200) - 1)
    assert last["volume_z"] is None              # crypto: no volume z-score
    assert rows[198]["dist_ma200"] is None       # needs 200 closes
    assert rows[199]["dist_ma200"] is not None


def test_volatility_is_annualized_per_asset_class():
    closes = [100.0]
    for i in range(29):
        closes.append(closes[-1] * (1.01 if i % 2 else 0.99))
    logs = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 21, len(closes))]
    m = sum(logs) / 21
    sd = math.sqrt(sum((x - m) ** 2 for x in logs) / 20)
    stock = features.compute(_dates(30), closes, [1.0] * 30, "stock")[-1]["vol_21d"]
    crypto = features.compute(_dates(30), closes, [1.0] * 30, "crypto")[-1]["vol_21d"]
    assert stock == pytest.approx(sd * math.sqrt(252))
    assert crypto == pytest.approx(sd * math.sqrt(365))


def test_rsi_extremes_and_known_value():
    assert features.wilder_rsi([float(i) for i in range(20)])[-1] == 100.0
    assert features.wilder_rsi([float(20 - i) for i in range(20)])[-1] == 0.0
    # 14 alternating +1/-1 changes: avg gain = avg loss → RSI 50.
    flat = [100.0 + (i % 2) for i in range(15)]
    assert features.wilder_rsi(flat)[-1] == pytest.approx(50.0, abs=1e-9)
    assert features.wilder_rsi(flat)[13] is None


def test_volume_z_score():
    vols = [100.0, 200.0] * 10 + [400.0]
    closes = [10.0] * 21
    z = features.compute(_dates(21), closes, vols, "stock")[-1]["volume_z"]
    past = vols[:20]
    m = sum(past) / 20
    sd = math.sqrt(sum((v - m) ** 2 for v in past) / 19)
    assert z == pytest.approx((400 - m) / sd)


def test_a_gap_stays_nan_and_is_never_filled():
    closes = [100.0 + i for i in range(80)]
    closes[70] = None                          # a missing bar
    rows = {r["date"]: r for r in features.compute(_dates(80), closes, [1.0] * 80, "crypto")}
    d = _dates(80)
    assert d[70] not in rows                   # no row is invented for the missing day
    after = rows[d[71]]
    assert after["ret_1d"] is None             # its previous close is missing
    assert after["ret_5d"] is not None         # endpoints exist, so the return does
    assert after["vol_21d"] is None            # window contains the gap
    assert after["dist_ma50"] is None
    assert after["rsi_14"] is None             # Wilder restarts after the gap
    assert rows[d[79]]["ret_1d"] is not None
    assert rows[d[79]]["dist_ma50"] is None    # still within 50 days of the gap
