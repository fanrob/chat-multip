import logging
from logging.handlers import RotatingFileHandler

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from config import BOT_TOKEN, LOG_FILE
from mail import periodic_email_check
from bot import (
    start, handle_telegram_message, button_callback,
    close_ticket, admin_panel, admin_callback, get_my_tickets,
    apply_operator_request, transfer_admin, periodic_inactivity_check,
)

# Настройка логирования: вывод в консоль + запись в файл с ротацией (5 МБ, 3 резервных копии)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


async def post_init(app: Application):
    """Фоновые задачи запускаются после инициализации event loop."""
    app.create_task(periodic_email_check(app), name="email_check")
    app.create_task(periodic_inactivity_check(app), name="inactivity_check")


def main():
    """Главная функция для запуска бота."""
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("close", close_ticket))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("my_tickets", get_my_tickets))
    app.add_handler(CommandHandler("apply_operator", apply_operator_request))
    app.add_handler(CommandHandler("transfer_admin", transfer_admin))

    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r'^(adm_|approve_op_|reject_op_|delete_op_|op_confirm_|transfer_admin_|del_wshop_)'))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_message))

    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Бот запущен. Начинаем прослушивание...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
