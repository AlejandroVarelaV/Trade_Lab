"""Alpaca client retries (fake httpx transport, no network)."""
from datetime import date

import httpx
import pytest

from tradelab.data import alpaca

TRADING_URL = "https://paper-api.alpaca.markets"
CALENDAR = [{"date": "2026-10-01", "open": "09:30", "close": "16:00"}]


@pytest.fixture()
def sleeps(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(alpaca.time, "sleep", waits.append)
    return waits


def _client(responses):
    """Each request takes the next item: an exception to raise or a (status, json) pair."""
    calls = []

    def handler(request):
        calls.append(request)
        item = responses[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item[0], json=item[1])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return alpaca.AlpacaClient("key", "secret", TRADING_URL, http=http), calls


def test_times_out_twice_then_succeeds(sleeps):
    client, calls = _client([httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"),
                             (200, CALENDAR)])
    sessions = client.calendar(date(2026, 10, 1), date(2026, 10, 1))
    assert [s.date for s in sessions] == [date(2026, 10, 1)]
    assert len(calls) == 3 and sleeps == [1, 2]               # exponential backoff
    assert calls[-1].headers["APCA-API-KEY-ID"] == "key"


def test_retries_429_and_5xx(sleeps):
    client, calls = _client([(429, {}), (503, {}), (500, {}), (200, CALENDAR)])
    assert len(client.calendar(date(2026, 10, 1), date(2026, 10, 1))) == 1
    assert len(calls) == 4 and sleeps == [1, 2, 4]


def test_gives_up_after_four_attempts(sleeps):
    client, calls = _client([httpx.ReadTimeout("slow")] * 4)
    with pytest.raises(httpx.ReadTimeout):
        client.calendar(date(2026, 10, 1), date(2026, 10, 1))
    assert len(calls) == 4 and sleeps == [1, 2, 4]            # no sleep after the last attempt

    client, calls = _client([(503, {})] * 4)
    with pytest.raises(httpx.HTTPStatusError):
        client.calendar(date(2026, 10, 1), date(2026, 10, 1))
    assert len(calls) == 4


def test_no_retry_on_other_4xx(sleeps):
    client, calls = _client([(403, {})])
    with pytest.raises(httpx.HTTPStatusError):
        client.calendar(date(2026, 10, 1), date(2026, 10, 1))
    assert len(calls) == 1 and sleeps == []
