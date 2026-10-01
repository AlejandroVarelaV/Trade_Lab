"""Phase 0 gate check: Alpaca paper account reachable.

Calls GET {ALPACA_BASE_URL}/v2/account (the base URL is forced to the paper
endpoint by settings.load()) and prints booleans for the gate:
    http_200, paper_endpoint, paper_account (account_number starts with "PA").
Exit code 0 only if all three are true. Secrets are never printed.

Run: python -m tradelab.checks.alpaca
"""
from __future__ import annotations

import sys

import httpx

from tradelab import settings


def main() -> int:
    cfg = settings.load()
    cfg.require("alpaca_key_id", "alpaca_secret_key")

    url = f"{cfg.alpaca_base_url}/v2/account"
    resp = httpx.get(
        url,
        headers={
            "APCA-API-KEY-ID": cfg.alpaca_key_id,
            "APCA-API-SECRET-KEY": cfg.alpaca_secret_key,
        },
        timeout=15,
    )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}

    http_200 = resp.status_code == 200
    paper_endpoint = httpx.URL(url).host == settings.PAPER_HOST
    paper_account = str(body.get("account_number", "")).startswith("PA")

    print(f"GET {url}")
    print(f"status_code={resp.status_code}")
    print(f"http_200={http_200}")
    print(f"paper_endpoint={paper_endpoint}")
    print(f"paper_account={paper_account}")
    if http_200:
        print(f"account_status={body.get('status')} currency={body.get('currency')} "
              f"cash={body.get('cash')} crypto_status={body.get('crypto_status')}")
    return 0 if (http_200 and paper_endpoint and paper_account) else 1


if __name__ == "__main__":
    sys.exit(main())
