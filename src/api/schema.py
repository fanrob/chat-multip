"""Схема для мессенджера: миграции поверх существующего full.db.

Существующие таблицы (tickets, workshops, operator_messages, settings)
не трогаем — бот продолжает ими пользоваться. Здесь только то, чего для
мессенджера не хватает:

    api_masters        экземпляры приложений мастеров (instance_id)
    ticket_members     второй мастер, подключённый к заявке
    messages           переписка, независимая от Telegram message_id
    api_attachments    файлы клиента и мастера
    events             лента событий с курсором (seq)
    api_outbox         сообщения, ждущие отправки в Telegram/Viber

Старая таблица masters (Telegram) выпилена миграцией 14: мастеры работают
только в приложении и живут в api_masters.

Версия схемы хранится в PRAGMA user_version. Миграции применяются по порядку
и идемпотентны, поэтому безопасно вызывать apply() при каждом старте.

Нумерация шагов идёт с пропусками (7, 11, 12, 13 сняты слитно в финальные
DDL ниже) — это позволяет не понижать user_version уже применённым базам.

API обязан подниматься без Telegram-бота, поэтому базовые legacy-таблицы
создаёт он сам (см. LEGACY_TABLES) — сразу в финальной форме, без цепочки
ALTER. У бота та же DDL остаётся через CREATE TABLE IF NOT EXISTS — на общей
базе это безвредно.
"""

import logging
import os
import sqlite3
from typing import Callable, List, Tuple

from api.db import db_path, transaction

logger = logging.getLogger(__name__)

#: Версия схемы = номер последней миграции. Считается из списка, чтобы
#: добавление миграции не требовало ручной правки константы.
Migration = Tuple[int, str, Callable[[], List[str]]]

#: Базовые таблицы бота. API создаёт их сам (в финальной форме), чтобы
#: подняться на пустой базе. DDL совпадает с src/storage.py: на общей базе
#: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS для второй стороны
#: не делает ничего.
LEGACY_TABLES: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        client_id TEXT NOT NULL,
        client_name TEXT NOT NULL,
        text TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        message_id TEXT,
        subject TEXT NOT NULL DEFAULT '',
        workshop_id INTEGER,
        updated_at TEXT,
        closed_at TEXT,
        close_reason TEXT,
        owner_master_uid TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_workshop ON tickets(workshop_id)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_owner ON tickets(owner_master_uid)",
    """
    CREATE TABLE IF NOT EXISTS operator_messages (
        message_id INTEGER PRIMARY KEY,
        ticket_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workshops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
]


def _create_legacy_tables(connection: sqlite3.Connection) -> None:
    """Создаёт базовые таблицы бота, если их ещё нет.

    Идемпотентно и безопасно на боевой базе: CREATE TABLE IF NOT EXISTS
    не трогает уже существующие таблицы.
    """
    for statement in LEGACY_TABLES:
        connection.execute(statement)


def _masters_v1() -> List[str]:
    """Экземпляры приложений мастеров.

    Связь с заявками идёт по api_masters.master_uid ('m_...'), который не
    зависит ни от Telegram, ни от Viber.
    """
    return [
        """
        CREATE TABLE IF NOT EXISTS api_masters (
            master_uid   TEXT PRIMARY KEY,
            instance_id  TEXT UNIQUE NOT NULL,
            full_name    TEXT NOT NULL DEFAULT '',
            workshop_id  INTEGER,
            is_active    INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT NOT NULL,
            last_seen_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_api_masters_workshop ON api_masters(workshop_id)",
        "CREATE INDEX IF NOT EXISTS idx_api_masters_active ON api_masters(is_active)",
    ]


def _ticket_members_v1() -> List[str]:
    """Кто имеет доступ к заявке.

    Владелец заявки хранится в tickets.owner_master_uid и tickets.status.
    Здесь — участники сверх владельца.
    """
    return [
        """
        CREATE TABLE IF NOT EXISTS ticket_members (
            ticket_id    TEXT NOT NULL,
            master_uid   TEXT NOT NULL,
            role         TEXT NOT NULL DEFAULT 'collaborator',
            joined_at    TEXT NOT NULL,
            PRIMARY KEY (ticket_id, master_uid)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_members_master ON ticket_members(master_uid)",
    ]


def _messages_v1() -> List[str]:
    """Переписка по заявке.

    operator_messages привязана к Telegram message_id и без Telegram не работает.
    Здесь своя история: работает одинаково для любого канала (Telegram, Viber).
    """
    return [
        """
        CREATE TABLE IF NOT EXISTS messages (
            id             TEXT PRIMARY KEY,
            ticket_id      TEXT NOT NULL,
            seq            INTEGER NOT NULL,
            sender         TEXT NOT NULL,
            master_uid     TEXT,
            sender_name    TEXT NOT NULL DEFAULT '',
            text           TEXT NOT NULL DEFAULT '',
            channel        TEXT NOT NULL DEFAULT 'telegram',
            delivery       TEXT NOT NULL DEFAULT 'queued',
            client_msg_id  TEXT,
            created_at     TEXT NOT NULL,
            read_at        TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_messages_ticket ON messages(ticket_id, seq)",
        "CREATE INDEX IF NOT EXISTS idx_messages_dedupe ON messages(client_msg_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client_msg "
        "ON messages(master_uid, client_msg_id) WHERE client_msg_id IS NOT NULL",
    ]


def _attachments_v1() -> List[str]:
    """Вложения клиента и мастера.

    master_uid — кому файл принадлежит до привязки к заявке: мастер грузит
    файл отдельным запросом и лишь потом отправляет сообщение, и в промежутке
    вложение видно только загрузившему, а не всем участникам заявки.
    """
    return [
        """
        CREATE TABLE IF NOT EXISTS api_attachments (
            id          TEXT PRIMARY KEY,
            ticket_id   TEXT,
            message_id  TEXT,
            master_uid  TEXT,
            source      TEXT NOT NULL,
            channel     TEXT NOT NULL DEFAULT 'telegram',
            filename    TEXT NOT NULL DEFAULT '',
            mime_type   TEXT NOT NULL DEFAULT '',
            size_bytes  INTEGER NOT NULL DEFAULT 0,
            storage_key TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_attachments_message ON api_attachments(message_id)",
        "CREATE INDEX IF NOT EXISTS idx_attachments_ticket ON api_attachments(ticket_id)",
        "CREATE INDEX IF NOT EXISTS idx_attachments_master ON api_attachments(master_uid)",
    ]


def _events_v1() -> List[str]:
    """Лента событий — основа синхронизации клиента.

    seq из AUTOINCREMENT сквозной и монотонный: клиент присылает курсор и
    получает всё, что появилось после него. Ничего не теряется при обрыве связи.
    """
    return [
        """
        CREATE TABLE IF NOT EXISTS events (
            seq        INTEGER PRIMARY KEY AUTOINCREMENT,
            master_uid TEXT NOT NULL,
            type       TEXT NOT NULL,
            ticket_id  TEXT,
            payload    TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_events_master ON events(master_uid, seq)",
        "CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_id, seq)",
    ]


def _outbox_v1() -> List[str]:
    """Очередь отправки в мессенджеры.

    Клиент вызывает POST /messages — запись попадает сюда со статусом pending.
    Процесс бота забирает, отправляет и пишет результат. API не знает про
    Telegram Bot API и не ждёт сеть — поэтому быстро отвечает мастеру.
    """
    return [
        """
        CREATE TABLE IF NOT EXISTS api_outbox (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id   TEXT NOT NULL,
            message_id  TEXT NOT NULL,
            channel     TEXT NOT NULL DEFAULT 'telegram',
            payload     TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            attempts    INTEGER NOT NULL DEFAULT 0,
            last_error  TEXT,
            created_at  TEXT NOT NULL,
            sent_at     TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_outbox_status ON api_outbox(status, id)",
    ]


def _ticket_seq_v1() -> List[str]:
    """Сквозная нумерация сообщений внутри заявки.

    Нужна для пагинации истории и для порядка при одновременной записи
    двух мастеров в одну заявку.
    """
    return [
        """
        CREATE TABLE IF NOT EXISTS ticket_seq (
            ticket_id TEXT PRIMARY KEY,
            last_seq  INTEGER NOT NULL DEFAULT 0
        )
        """
    ]


def _read_state_v1() -> List[str]:
    """Прочитанное — у каждого мастера своё.

    Общий read_at в messages не годится: у заявки несколько участников,
    и «прочитал» относится к конкретному человеку, а не к заявке.
    """
    return [
        """
        CREATE TABLE IF NOT EXISTS ticket_read_state (
            ticket_id    TEXT NOT NULL,
            master_uid   TEXT NOT NULL,
            read_cursor  INTEGER NOT NULL DEFAULT 0,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (ticket_id, master_uid)
        )
        """
    ]


def _declines_v1() -> List[str]:
    """Отказы от заявок (кнопка «не наш профиль»).

    Отказ скрывает заявку только у одного мастера, поэтому он хранится
    отдельно от статуса заявки: status трогать нельзя.
    """
    return [
        """
        CREATE TABLE IF NOT EXISTS ticket_declines (
            ticket_id  TEXT NOT NULL,
            master_uid TEXT NOT NULL,
            reason     TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (ticket_id, master_uid)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_declines_master ON ticket_declines(master_uid)",
    ]


def _drop_legacy_v14() -> List[str]:
    """Выпиливает хвосты Telegram-мастеров.

    masters — таблица операторов Telegram-бота: мастеры перешли в приложение
    (api_masters), таблица больше не нужна. Колонки taken_by (tickets) и
    legacy_user_id (api_masters) были сняты той же миграцией в её ранней
    редакции — на уже обновлённых базах их нет, на свежих их не существует.
    """
    return [
        "DROP TABLE IF EXISTS masters",
    ]


def _status_vocabulary_v15() -> List[str]:
    """Переводит оставшиеся 'taken' в единый словарь 'in_progress'.

    Хвосты миграции на статусы могли уцелеть на базах, обновлённых до
    единого словаря (заявки, взятые старым API). Больше 'taken' ничего не
    пишет: боты пишут 'new'/'closed', API — 'in_progress'.
    """
    return [
        "UPDATE tickets SET status = 'in_progress' WHERE status = 'taken'",
    ]


def _slim_operator_messages_v16() -> List[str]:
    """Сужает operator_messages до связки message_id -> ticket_id.

    Раньше хранились operator_id/sent_at/sender/text — под диалог, которого
    нет: бот использует только get_ticket_id(). UNIQUE-колонок именно это
    сужение и есть следующая ячейка; историю ответов держит messages.

    Пересборка через новую таблицу: штатный DROP COLUMN требует SQLite >= 3.35
    и спотыкается на inline-комментариях. Идемпотентно на любой базе — на
    свежей (уже slim) перенос пуст, на боевой — сохраняет message_id/ticket_id.
    """
    return [
        "DROP TABLE IF EXISTS operator_messages_new",
        """
        CREATE TABLE operator_messages_new (
            message_id INTEGER PRIMARY KEY,
            ticket_id TEXT NOT NULL
        )
        """,
        "INSERT INTO operator_messages_new (message_id, ticket_id) "
        "SELECT message_id, ticket_id FROM operator_messages",
        "DROP TABLE operator_messages",
        "ALTER TABLE operator_messages_new RENAME TO operator_messages",
    ]


MIGRATIONS: List[Migration] = [
    (1, "api_masters", _masters_v1),
    (2, "ticket_members", _ticket_members_v1),
    (3, "messages", _messages_v1),
    (4, "attachments", _attachments_v1),
    (5, "events", _events_v1),
    (6, "outbox", _outbox_v1),
    (8, "ticket_seq", _ticket_seq_v1),
    (9, "read_state", _read_state_v1),
    (10, "declines", _declines_v1),
    (14, "drop_legacy", _drop_legacy_v14),
    (15, "status_vocabulary", _status_vocabulary_v15),
    (16, "slim_operator_messages", _slim_operator_messages_v16),
]

#: Версия схемы = номер последней миграции. Считается из списка, чтобы
#: добавление миграции не требовало ручной правки константы.
SCHEMA_VERSION: int = max(step for step, _, _ in MIGRATIONS)


def _current_version(connection: sqlite3.Connection) -> int:
    return connection.execute("PRAGMA user_version").fetchone()[0]


def apply(path: str | None = None) -> int:
    """Применяет недостающие миграции. Идемпотентно.

    Возвращает версию схемы после применения.
    """
    target = path or db_path()
    parent = os.path.dirname(os.path.abspath(target))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    with transaction(target) as connection:
        version = _current_version(connection)
        # База может быть пустой (сервер без бота) — сначала legacy-таблицы
        # в финальной форме, иначе миграции упадут на отсутствующей tickets.
        _create_legacy_tables(connection)

        for step, name, statements in MIGRATIONS:
            if step <= version:
                continue
            for statement in statements():
                connection.execute(statement)
            logger.info("Миграция %d (%s) применена", step, name)

        version = _current_version(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    logger.info("Схема мессенджера готова, версия %d", SCHEMA_VERSION)
    return SCHEMA_VERSION


def reset_for_tests(path: str) -> None:
    """Удаляет таблицы мессенджера. Только для тестов."""
    tables = [
        "api_outbox",
        "events",
        "api_attachments",
        "messages",
        "ticket_declines",
        "ticket_read_state",
        "ticket_seq",
        "ticket_members",
        "api_masters",
    ]
    with transaction(path) as connection:
        for table in tables:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        # Колонки в tickets не трогаем: таблица принадлежит боту.
        connection.execute("PRAGMA user_version = 0")
