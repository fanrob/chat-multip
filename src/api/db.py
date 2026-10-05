"""Единая точка подключения к SQLite.

Почему отдельный модуль: сервер запускается тремя независимыми процессами
(API, telegram-бот, будущий viber-бот), и все они пишут в один файл БД.
Без явных PRAGMA sqlite3 работает в режиме delete, где писатель блокирует
и читателей, и писателей — процессы начнут получать "database is locked".

WAL переводит БД в режим, где читатели не блокируют писателя, а busy_timeout
заставляет ждать освобождения блокировки вместо немедленной ошибки.

ВАЖНО: этот модуль ничего не знает про схему и не создаёт соединения на
импорте — только по вызову connect().
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

#: Время ожидания освобождения блокировки записи, секунды.
BUSY_TIMEOUT_MS = 30_000


def db_path() -> str:
    """Путь к БД. Переопределяется переменной CHAT_MULTI_DB (нужно в тестах
    и когда docker монтирует том в другой точке)."""
    return os.environ.get("CHAT_MULTI_DB", "full.db")


def connect(path: Optional[str] = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Открывает соединение с нужными PRAGMA.

    read_only=True не даёт случайно что-то записать из GET-обработчика.
    """
    target = path or db_path()

    if read_only:
        # WAL всё равно нужен, иначе read-only-соединение требует shared-кеша.
        connection = sqlite3.connect(target, timeout=BUSY_TIMEOUT_MS / 1000)
    else:
        connection = sqlite3.connect(target, timeout=BUSY_TIMEOUT_MS / 1000)

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = %d" % BUSY_TIMEOUT_MS)
    connection.execute("PRAGMA journal_mode = WAL")
    # NORMAL не теряет данные при сбое приложения (теряет только при сбое
    # питания), зато не пишет fsync на каждый коммит. Для БД заявок нормально.
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def transaction(path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    """Транзакция с гарантированным закрытием соединения.

    В отличие от `with sqlite3.connect(...)`, который только коммитит и НЕ
    закрывает соединение, здесь файл всегда освобождается.
    """
    connection = connect(path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        logger.exception("Транзакция откачена")
        raise
    finally:
        connection.close()


@contextmanager
def reading(path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    """Только чтение, без коммита."""
    connection = connect(path)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def unit_of_work(connection: Optional[sqlite3.Connection] = None) -> Iterator[sqlite3.Connection]:
    """Единица работы: своя транзакция или участие в чужой.

    Нужна, чтобы изменение состояния и запись события были одним коммитом.
    Если бы store коммитил состояние, а событие писалось вторым вызовом
    transaction(), то падение между ними оставило бы заявку взятой, а
    событие — не отправленным: клиент не узнал бы об изменении до полного
    ресинка.

    С переданным connection ничего не коммитится: коммит делает внешний
    transaction(), который и владеет соединением.
    """
    if connection is not None:
        yield connection
        return
    with transaction() as own:
        yield own


def ensure_wal(path: Optional[str] = None) -> None:
    """Переводит существующий файл БД в WAL.

    WAL включается один раз и хранится в заголовке файла, поэтому достаточно
    открыть соединение с journal_mode=WAL и закрыть. Вызывается при старте API,
    чтобы работали все процессы, даже если бот запустился раньше.
    """
    target = path or db_path()
    if not os.path.exists(target):
        logger.warning("Файл БД не найден: %s — будет создан при первом запросе", target)
        return
    connection = sqlite3.connect(target, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode.lower() != "wal":
            logger.warning("Не удалось включить WAL, режим: %s", mode)
        else:
            logger.info("БД %s в режиме WAL", target)
    finally:
        connection.close()
