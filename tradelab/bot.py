"""TradeLab Telegram bot (long polling, no inbound port).

Phase 0: answers /ping with a liveness line that includes a database check.
It only talks to TELEGRAM_CHAT_ID; messages from any other chat are ignored.

Run: python -m tradelab.bot
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from tradelab import db, settings

log = logging.getLogger("tradelab.bot")


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: settings.Settings = context.bot_data["settings"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        n = db.migration_count(cfg.database_url)
        db_line = f"db ok, {n} migrations applied"
    except Exception as exc:  # report, don't crash the bot
        log.exception("db check failed")
        db_line = f"🔴 db error: {type(exc).__name__}"
    await update.effective_message.reply_text(f"pong · {now}\n{db_line}")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every getUpdates call, and the URL contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = settings.load()
    cfg.require("telegram_bot_token", "telegram_chat_id", "database_url")

    app = Application.builder().token(cfg.telegram_bot_token).build()
    app.bot_data["settings"] = cfg
    only_owner = filters.Chat(chat_id=cfg.telegram_chat_id)
    app.add_handler(CommandHandler("ping", ping, filters=only_owner))

    log.info("bot starting (long polling), chat_id=%s", cfg.telegram_chat_id)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
