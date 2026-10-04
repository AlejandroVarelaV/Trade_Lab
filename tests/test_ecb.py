"""ECB client retries (fake httpx transport, no network)."""
from datetime import date

import httpx
import pytest

from tradelab.data import ecb

CSV = ("KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
       "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-01,1.1355\n")


@pytest.fixture()
def sleeps(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(ecb.time, "sleep", waits.append)
    return waits


def _client(responses):
    """Each request takes the next item: an exception to raise or a (status, body) pair."""
    calls = []

    def handler(request):
        calls.append(request)
        item = responses[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item[0], text=item[1])

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_times_out_twice_then_succeeds(sleeps):
    http, calls = _client([httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"), (200, CSV)])
    rows, rejected = ecb.fetch(date(2026, 10, 1), date(2026, 10, 1), http)
    assert rows == [(date(2026, 10, 1), 1.1355)] and rejected == []
    assert len(calls) == 3
    assert sleeps == [1, 2]                                   # exponential backoff
    assert calls[-1].url.params["startPeriod"] == "2026-10-01"


def test_retries_429_and_5xx(sleeps):
    http, calls = _client([(429, ""), (503, ""), (500, ""), (200, CSV)])
    rows, _ = ecb.fetch(date(2026, 10, 1), date(2026, 10, 1), http)
    assert len(rows) == 1 and len(calls) == 4 and sleeps == [1, 2, 4]


def test_gives_up_after_four_attempts(sleeps):
    http, calls = _client([httpx.ReadTimeout("slow")] * 4)
    with pytest.raises(httpx.ReadTimeout):
        ecb.fetch(date(2026, 10, 1), date(2026, 10, 1), http)
    assert len(calls) == 4 and sleeps == [1, 2, 4]

    http, calls = _client([(503, "")] * 4)
    with pytest.raises(httpx.HTTPStatusError):
        ecb.fetch(date(2026, 10, 1), date(2026, 10, 1), http)
    assert len(calls) == 4


def test_no_retry_on_404_or_other_4xx(sleeps):
    http, calls = _client([(404, "")])
    assert ecb.fetch(date(2026, 10, 3), date(2026, 10, 4), http) == ([], [])
    http, calls = _client([(400, "")])
    with pytest.raises(httpx.HTTPStatusError):
        ecb.fetch(date(2026, 10, 1), date(2026, 10, 1), http)
    assert len(calls) == 1 and sleeps == []
