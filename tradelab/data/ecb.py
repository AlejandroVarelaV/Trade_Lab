"""ECB EUR/USD reference rate (USD per 1 EUR) from the ECB data API."""
from __future__ import annotations

import csv
import io
import math
from datetime import date

import httpx

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


def fetch(start: date, end: date, http: httpx.Client | None = None
          ) -> tuple[list[tuple[date, float]], list[str]]:
    http = http or httpx.Client(timeout=30)
    resp = http.get(ECB_URL, params={"format": "csvdata", "startPeriod": start.isoformat(),
                                     "endPeriod": end.isoformat()})
    if resp.status_code == 404:          # the ECB answers 404 when there is no data
        return [], []
    resp.raise_for_status()
    return parse_csv(resp.text)
