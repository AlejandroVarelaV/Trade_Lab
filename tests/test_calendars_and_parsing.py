"""Time rules and source parsing: crypto 22:00 days, fill windows, ECB days, NaN rejection."""
from datetime import date, datetime, timedelta, timezone

from tradelab import calendars
from tradelab.data import alpaca, ecb

UTC = timezone.utc


def test_target_holidays_2026():
    # Easter 2026 is 5 April: Good Friday 3 April, Easter Monday 6 April.
    assert calendars.easter(2026) == date(2026, 4, 5)
    assert not calendars.is_ecb_business_day(date(2026, 4, 3))
    assert not calendars.is_ecb_business_day(date(2026, 4, 6))
    assert not calendars.is_ecb_business_day(date(2026, 12, 25))
    assert not calendars.is_ecb_business_day(date(2026, 10, 3))   # Saturday
    assert calendars.is_ecb_business_day(date(2026, 10, 1))
    assert calendars.last_ecb_business_day(date(2026, 10, 4)) == date(2026, 10, 2)


def test_last_complete_day_switches_at_22_utc():
    assert calendars.last_complete_day(datetime(2026, 10, 1, 21, 59, tzinfo=UTC)) == date(2026, 9, 30)
    assert calendars.last_complete_day(datetime(2026, 10, 1, 22, 30, tzinfo=UTC)) == date(2026, 10, 1)


def test_crypto_fill_window_is_next_noon_utc():
    w = calendars.next_crypto_fill_window(datetime(2026, 10, 1, 22, 30, tzinfo=UTC))
    assert w == datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    w = calendars.next_crypto_fill_window(datetime(2026, 10, 1, 11, 0, tzinfo=UTC))
    assert w == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    w = calendars.next_crypto_fill_window(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    assert w == datetime(2026, 10, 2, 12, 0, tzinfo=UTC)          # strictly after


def _hour(ts, o, c, v=1.0):
    return {"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": o, "h": max(o, c) + 1, "l": min(o, c) - 1,
            "c": c, "v": v, "n": 1, "vw": o}


def test_crypto_hours_aggregate_into_22utc_days():
    start = datetime(2026, 9, 29, 22, tzinfo=UTC)     # crypto day 2026-09-30
    hours = [_hour(start + timedelta(hours=h), 100 + h, 101 + h) for h in range(24)]
    bars, issues = alpaca.aggregate_crypto_days("BTC/USD", hours, date(2026, 9, 30), date(2026, 9, 30))
    assert issues == []
    (b,) = bars
    assert (b.date, b.open, b.close, b.volume) == (date(2026, 9, 30), 100.0, 124.0, 24.0)
    assert b.high == 125.0 and b.low == 99.0


def test_crypto_day_without_its_last_hour_is_not_built():
    start = datetime(2026, 9, 29, 22, tzinfo=UTC)
    hours = [_hour(start + timedelta(hours=h), 100, 100) for h in range(23)]   # 21:00 missing
    bars, issues = alpaca.aggregate_crypto_days("BTC/USD", hours, date(2026, 9, 30), date(2026, 9, 30))
    assert bars == [] and "last hour" in issues[0]


def test_nan_and_missing_values_are_rejected_not_stored():
    payload = {"SPY": [
        {"t": "2026-09-29T04:00:00Z", "o": 1, "h": 2, "l": 1, "c": "NaN", "v": 5},
        {"t": "2026-09-30T04:00:00Z", "o": 1, "h": 2, "l": 1, "v": 5},              # no close
        {"t": "2026-10-01T04:00:00Z", "o": 1, "h": 2, "l": 1, "c": 1.5, "v": 5},
    ]}
    bars, rejected = alpaca.parse_stock_bars(payload)
    assert [b.date for b in bars] == [date(2026, 10, 1)]
    assert len(rejected) == 2


def test_stock_bar_date_is_new_york_session_date():
    bars, _ = alpaca.parse_stock_bars({"SPY": [
        {"t": "2026-12-01T05:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]})
    assert bars[0].date == date(2026, 12, 1)


def test_calendar_open_close_in_utc_across_dst():
    s = alpaca.parse_calendar([
        {"date": "2026-10-30", "open": "09:30", "close": "16:00"},   # EDT
        {"date": "2026-11-02", "open": "09:30", "close": "16:00"},   # EST
        {"date": "2026-11-27", "open": "09:30", "close": "13:00"},   # half day
    ])
    assert s[0].open_at == datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
    assert s[1].open_at == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
    assert s[2].close_at == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_ecb_csv_parsing_rejects_nan():
    text = ("KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
            "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-09-29,1.1355\n"
            "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-09-30,NaN\n"
            "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-01,\n")
    rows, rejected = ecb.parse_csv(text)
    assert rows == [(date(2026, 9, 29), 1.1355)]
    assert len(rejected) == 2
