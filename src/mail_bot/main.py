#главный модуль почтового бота

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Позволяет запускать файл напрямую (`python src/mail_bot/main.py`), когда
# каталог src не в PYTHONPATH. Тот же приём, что в telegram_bot/main.py.
_SRC_ROOT = Path(__file__).resolve().parent.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from mail_bot import outbox as mail_outbox
from mail_bot.mailbox import periodic_check
from outbox_queue import CHANNEL_EMAIL, outbox_loop, recover_stale

#: Свой лог: процессов теперь два, и общий файл перетирал бы им записи.
LOG_FILE = "mail.log"

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


async def run():
    """Две независимые задачи: чтение ящика и доставка ответов мастеров.

    recover_stale() — до запуска циклов: иначе консьюмер первым же запросом
    увидит строки, застрявшие в sending после прошлого падения, и не тронет их.
    """
    recover_stale(CHANNEL_EMAIL)
    await asyncio.gather(
        periodic_check(),
        outbox_loop(CHANNEL_EMAIL, mail_outbox.deliver),
    )


def main():
    logger.info("Почтовый бот запущен")
    asyncio.run(run())


if __name__ == "__main__":
    main()
