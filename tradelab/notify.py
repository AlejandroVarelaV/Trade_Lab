"""One-way Telegram notifications (sendMessage over HTTPS, no polling)."""
from __future__ import annotations

import logging

import httpx

from tradelab.settings import Settings

log = logging.getLogger(__name__)
ICONS = {"ok": "✅", "warn": "⚠️", "error": "🔴"}


def send(cfg: Settings, text: str) -> bool:
    """Send text to TELEGRAM_CHAT_ID. Never raises; returns whether it was delivered."""
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        log.warning("telegram not configured; message not sent")
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
            json={"chat_id": cfg.telegram_chat_id, "text": text[:4000],
                  "disable_web_page_preview": True},
            timeout=15,
        )
        ok = resp.status_code == 200
        if not ok:   # never log the URL: it contains the bot token
            log.error("telegram sendMessage failed: HTTP %s", resp.status_code)
        return ok
    except httpx.HTTPError as exc:
        log.error("telegram sendMessage failed: %s", type(exc).__name__)
        return False
