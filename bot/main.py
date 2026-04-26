from telegram.ext import Application

from bot.handlers import register_handlers
from config import TELEGRAM_BOT_TOKEN


def create_application() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    register_handlers(app)
    return app
