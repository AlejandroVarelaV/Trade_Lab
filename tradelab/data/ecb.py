"""ECB EUR/USD reference rate (USD per 1 EUR) from the ECB data API.

The API is slow at times, so requests get a 60 s timeout and are retried like
the Alpaca client: 4 attempts, exponential backoff (1, 2, 4 s) on timeouts,
429 and 5xx.
"""
from __future__ import annotations

import csv
import io
import logging
import math
import time
from datetime import date

import httpx

log = logging.getLogger(__name__)

TIMEOUT_S = 60
ATTEMPTS = 4
ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
SOURCE = "ecb_exr"


def parse_csv(text: str) -> tuple[list[tuple[date, float]], list[str]]:
    """ECB csvdata → [(date, eurusd)], rejecting empty or non-finite values."""
    rows, rejected = [], []
    for rec in csv.DictReader(io.StringIO(text)):
        d = date.fromisoformat(rec["TIME_PERIOD"])
        raw = (rec.get("OBS_VALUE") or "").strip()
        try:
            v = float(raw)
        except ValueError:
            v = math.nan
        if not math.isfinite(v) or v <= 0:
            rejected.append(f"EURUSD {d}: value {raw!r} rejected")
            continue
        rows.append((d, v))
    return rows, rejected


def _get(http: httpx.Client, params: dict) -> httpx.Response:
    for attempt in range(ATTEMPTS):
        last = attempt == ATTEMPTS - 1
        wait = 2 ** attempt
        try:
            resp = http.get(ECB_URL, params=params)
        except httpx.TimeoutException as exc:
            if last:
                raise
            log.warning("ecb %s, retry in %ss", type(exc).__name__, wait)
            time.sleep(wait)
            continue
        if (resp.status_code == 429 or resp.status_code >= 500) and not last:
            log.warning("ecb %s, retry in %ss", resp.status_code, wait)
            time.sleep(wait)
            continue
        return resp
    raise AssertionError("unreachable")


def fetch(start: date, end: date, http: httpx.Client | None = None
          ) -> tuple[list[tuple[date, float]], list[str]]:
    http = http or httpx.Client(timeout=TIMEOUT_S)
    resp = _get(http, {"format": "csvdata", "startPeriod": start.isoformat(),
                       "endPeriod": end.isoformat()})
    if resp.status_code == 404:          # the ECB answers 404 when there is no data
        return [], []
    resp.raise_for_status()
    return parse_csv(resp.text)
