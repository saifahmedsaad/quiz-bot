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
    # drop_pending_updates clears any stale updates; if another instance is
    # briefly polling the same token we retry instead of crashing.
    port = int(os.environ.get("PORT", 8080))
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=token,
        webhook_url=f"https://quiz-bot.up.railway.app/{token}",
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
