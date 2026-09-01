"""Telegram quiz bot — main entry point.

A Kahoot-style quiz bot for Saif's programming groups (الصف الأول الثانوي).
Students answer inside the Telegram group; scores and a live leaderboard
are shown. Fully free, runs forever, owned by you.

Layout (modular, clean separation):
  - quiz_data.py   : question bank loader (shared with Blooket exporter)
  - game.py        : per-chat quiz session state machine
  - handlers.py    : Telegram update handlers
  - bot.py         : wires everything together + runs the app
"""
from __future__ import annotations

import logging
import os
from dotenv import load_dotenv
from telegram.ext import Application

import handlers

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("quiz_bot")

def main() -> None:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "your_bot_token_here":
        raise SystemExit(
            "❌ TELEGRAM_BOT_TOKEN غير موجود. انسخ .env.example إلى .env "
            "و حط التوكن من @BotFather."
        )

    app = Application.builder().token(token).build()
    # job_queue is required (installed via python-telegram-bot[job-queue])
    if app.job_queue is None:
        raise SystemExit(
            "❌ JobQueue غير متاح. ثبّت: pip install \"python-telegram-bot[job-queue]\""
        )

    # Register handlers
    app.add_handler(handlers.start_handler())
    app.add_handler(handlers.sets_handler())
    app.add_handler(handlers.help_handler())
    app.add_handler(handlers.myscore_handler())
    app.add_handler(handlers.myid_handler())
    app.add_handler(handlers.schedule_handler())
    app.add_handler(handlers.join_handler())
    app.add_handler(handlers.answer_handler())
    app.add_handler(handlers.startnow_handler())
    app.add_handler(handlers.callback_handler_reg())
    app.add_handler(handlers.fallback_handler())

    handlers._ensure_weeks()  # make sure every weekly set has a state entry

    logger.info("Quiz bot started ✅")

    import asyncio

    async def setup_webhook():
        # Use the auto-assigned Railway Static URL, or derive from app name
        public_url = (
            os.getenv("RAILWAY_STATIC_URL")
            or os.getenv("RAILWAY_BACKEND_URL")
            or f"https://{(os.getenv('RAILWAY_APP_NAME') or 'quiz-bot')}.up.railway.app"
        )

        # Strip protocol to avoid duplication if RAILWAY_STATIC_URL includes https://
        if public_url.startswith("https://"):
            public_url = public_url[8:]

        webhook_url = f"https://{public_url}/{token}"
        await app.bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook set to: {webhook_url}")

        PORT = int(os.getenv("PORT", 8080))
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=token,
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )

    asyncio.run(setup_webhook())

if __name__ == "__main__":
    main()
