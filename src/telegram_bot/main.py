#главный модуль телеграм бота

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Позволяет запускать файл напрямую (`python src/telegram_bot/main.py`), когда
# каталог src не в PYTHONPATH. Тот же приём, что в api/app.py.
_SRC_ROOT = Path(__file__).resolve().parent.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from functools import partial

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, LOG_FILE
from outbox_queue import CHANNEL_TELEGRAM, outbox_loop, recover_stale
from telegram_bot.bot import (
    admin_callback,
    admin_panel,
    button_callback,
    handle_telegram_message,
    start,
    transfer_admin,
)
from telegram_bot.outbox import deliver as deliver_telegram

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
    """Фоновые задачи запускаются после инициализации event loop.

    Здесь же outbox-консьюмер: он доставляет в Telegram сообщения, отправленные
    мастерами через API. Ответы по заявкам, пришедшим на почту, доставляет
    отдельный процесс mail_bot, поэтому консьюмер забирает только свой канал.
    Запускать его надо после recover_stale(), иначе он первым же запросом
    увидит строки, застрявшие в sending после прошлого падения, и не тронет их.
    """
    recover_stale(CHANNEL_TELEGRAM)
    app.create_task(
        outbox_loop(CHANNEL_TELEGRAM, partial(deliver_telegram, app.bot)), name="outbox"
    )


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("transfer_admin", transfer_admin))   #TODO переделать алгоритм, чтобы любомы можно было отправить запрос на админство

    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r'^(adm_|approve_op_|reject_op_|delete_op_|op_confirm_|transfer_admin_|del_wshop_)'))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_message))

    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Бот запущен. Начинаем прослушивание...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
