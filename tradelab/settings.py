"""Environment-backed settings and the paper-only safety guard.

Every entrypoint calls `load()` at startup. It hard-fails (SystemExit) if the
Alpaca trading base URL is anything other than the paper endpoint, so a live
URL can never reach the code that places orders.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

PAPER_HOST = "paper-api.alpaca.markets"
PAPER_BASE_URL = f"https://{PAPER_HOST}"


class UnsafeConfigError(Exception):
    """Raised when configuration could route orders anywhere but Alpaca paper."""


def check_paper_url(url: str) -> str:
    """Return the normalised paper base URL, or raise UnsafeConfigError.

    Accepts only https://paper-api.alpaca.markets (optionally with a trailing
    slash or /v2). Scheme, host, port, credentials and path are all checked,
    so look-alikes such as https://paper-api.alpaca.markets.evil.com or
    https://api.alpaca.markets are rejected.
    """
    parts = urlsplit((url or "").strip())
    if (
        parts.scheme != "https"
        or parts.hostname != PAPER_HOST
        or parts.port is not None
        or parts.username is not None
        or parts.query
        or parts.fragment
        or parts.path.rstrip("/") not in ("", "/v2")
    ):
        raise UnsafeConfigError(
            f"ALPACA_BASE_URL must be {PAPER_BASE_URL} (paper trading only); got {url!r}"
        )
    return PAPER_BASE_URL


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    alpaca_base_url: str
    alpaca_key_id: str | None
    alpaca_secret_key: str | None
    telegram_bot_token: str | None
    telegram_chat_id: int | None
    anthropic_api_key: str | None
    healthcheck_url: str | None

    def require(self, *names: str) -> None:
        """Exit with a clear message if any of the named settings is empty."""
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            env_names = ", ".join(n.upper() for n in missing)
            sys.exit(f"ERROR: missing required environment variables: {env_names}")


def _opt(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def load() -> Settings:
    """Read settings from the environment. Hard-fails on a non-paper Alpaca URL."""
    try:
        alpaca_base_url = check_paper_url(os.environ.get("ALPACA_BASE_URL", PAPER_BASE_URL))
    except UnsafeConfigError as exc:
        sys.exit(f"FATAL: {exc}")

    chat_id = _opt("TELEGRAM_CHAT_ID")
    return Settings(
        database_url=_opt("DATABASE_URL"),
        alpaca_base_url=alpaca_base_url,
        alpaca_key_id=_opt("ALPACA_KEY_ID"),
        alpaca_secret_key=_opt("ALPACA_SECRET_KEY"),
        telegram_bot_token=_opt("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=int(chat_id) if chat_id else None,
        anthropic_api_key=_opt("ANTHROPIC_API_KEY"),
        healthcheck_url=_opt("HEALTHCHECK_URL"),
    )
