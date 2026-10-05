"""Схема для мессенджера: миграции поверх существующего full.db.

Существующие таблицы (tickets, masters, workshops, operator_messages, settings)
не трогаем — бот продолжает ими пользоваться. Здесь только то, чего для
мессенджера не хватает:

    api_masters        экземпляры приложений мастеров (instance_id)
    ticket_members     второй мастер, подключённый к заявке
    messages           переписка, независимая от Telegram message_id
    api_attachments    файлы клиента и мастера
    events             лента событий с курсором (seq)
    api_outbox         сообщения, ждущие отправки в Telegram/Viber

Версия схемы хранится в PRAGMA user_version. Миграции применяются по порядку
и идемпотентны, поэтому безопасно вызывать apply() при каждом старте.

API обязан подниматься без Telegram-бота, поэтому базовые legacy-таблицы
создаёт он сам (см. LEGACY_TABLES): иначе миграции, которые только ALTERят
`tickets`, падают на пустой базе. У бота та же DDL остаётся через
CREATE TABLE IF NOT EXISTS — на общей базе это безвредно.
"""

import logging
import os
import sqlite3
from typing import Callable, List, Tuple

from api.db import db_path, transaction

logger = logging.getLogger(__name__)

#: Версия схемы = номер последней миграции. Считается из списка MIGRATIONS,
#: чтобы добавление миграции не требовало ручной правки константы.
Migration = Tuple[int, str, Callable[[], List[str]]]

#: Базовые таблицы бота. API создаёт их сам, чтобы подняться на пустой базе.
#: DDL совпадает с src/storage.py и src/config.py: на общей базе CREATE TABLE
#: IF NOT EXISTS для второй стороны не делает ничего.
LEGACY_TABLES: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        client_id TEXT NOT NULL,
        client_name TEXT NOT NULL,
        text TEXT NOT NULL,
        status TEXT NOT NULL,
        taken_by INTEGER,
        created_at TEXT NOT NULL,
        message_id TEXT,
        subject TEXT NOT NULL DEFAULT '',
        workshop_id INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS operator_messages (
        message_id INTEGER PRIMARY KEY,
        operator_id INTEGER NOT NULL,
        ticket_id TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        sender TEXT NOT NULL DEFAULT '',
        sender_id TEXT,
        text TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS masters (
        user_id INTEGER PRIMARY KEY,
        full_name TEXT,
        added_at TEXT NOT NULL,
        is_deleted INTEGER DEFAULT 0,
        workshop_id INTEGER
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

    Отдельная таблица, а не колонка в masters: masters.user_id — это Telegram ID,
    а в мессенджере мастер работает без Telegram-аккаунта. Связь с заявками идёт
    по api_masters.master_uid, который не зависит ни от Telegram, ни от Viber.
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
            last_seen_at TEXT,
            -- null, пока мастер не привязан к мастеру из бота
            legacy_user_id INTEGER
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_api_masters_workshop ON api_masters(workshop_id)",
        "CREATE INDEX IF NOT EXISTS idx_api_masters_active ON api_masters(is_active)",
    ]


def _ticket_members_v1() -> List[str]:
    """Кто имеет доступ к заявке.

    owner хранится в tickets.taken_by (уже есть, бот его использует) и в
    tickets.status. Здесь — участники сверх владельца.
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
    return [
        """
        CREATE TABLE IF NOT EXISTS api_attachments (
            id          TEXT PRIMARY KEY,
            ticket_id   TEXT,
            message_id  TEXT,
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


def _tickets_extra_v1() -> List[str]:
    """Дополнительные поля заявки, которых нет в существующей таблице.

    ALTER TABLE с проверкой: если колонка уже есть, CREATE INDEX на неё
    не падает, а ALTER не выполняется.
    """
    return [
        "ALTER TABLE tickets ADD COLUMN updated_at TEXT",
        "ALTER TABLE tickets ADD COLUMN closed_at TEXT",
        "ALTER TABLE tickets ADD COLUMN close_reason TEXT",
        "CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status, created_at)",
    ]


def _tickets_workshop_v11() -> List[str]:
    """workshop_id для заявок — отдельной миграцией, а не в составе 7-й.

    storage.py объявляет tickets с workshop_id, но CREATE TABLE IF NOT EXISTS
    на уже существующей таблице ничего не меняет: в боевой full.db колонки нет.
    Из-за этого любой запрос, её читающий, падал с "no such column: t.workshop_id".

    Вынесено отдельным шагом, потому что к этому моменту миграция 7 уже была
    применена на части установок — там user_version = 10, и новый шаг 11
    дойдёт до них, а изменение старой миграции — нет.
    """
    return [
        "ALTER TABLE tickets ADD COLUMN workshop_id INTEGER",
        "CREATE INDEX IF NOT EXISTS idx_tickets_workshop ON tickets(workshop_id)",
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


def _ticket_owner_v12() -> List[str]:
    """owner_master_uid — владелец заявки по данным API.

    Почему не переиспользуем tickets.taken_by: это INTEGER с Telegram ID,
    и бот сравнивает его с целым числом (WHERE taken_by = ?). Положив туда
    строку 'm_...', мы бы сломали его выборки — SQLite по INTEGER-аффинности
    оставил бы значение текстом и сравнение перестало бы совпадать.

    Поэтому владелец из приложения лежит здесь, а taken_by продолжает
    означать Telegram ID. Если заявку взял бот — заполнен taken_by,
    если приложение — owner_master_uid. Владелец один из двух.
    """
    return [
        "ALTER TABLE tickets ADD COLUMN owner_master_uid TEXT",
        "CREATE INDEX IF NOT EXISTS idx_tickets_owner ON tickets(owner_master_uid)",
    ]


def _attachment_owner_v13() -> List[str]:
    """master_uid во вложениях — кому файл принадлежит до привязки к заявке.

    Мастер грузит файл отдельным запросом и лишь потом отправляет сообщение.
    В промежутке вложение не привязано ни к заявке, ни к сообщению, и правило
    «видно участникам заявки» к нему неприменимо: заявки-то ещё нет.

    Раньше такой файл возвращал can_access() = True любому мастеру, кто знает
    id. id — это 12 hex-символов (48 бит), перебор вряд ли практичен, но
    правильность доступа не должна держаться на секретности id: по этой колонке
    файл виден только загрузившему, а после отправки сообщения — участникам
    заявки.
    """
    return [
        "ALTER TABLE api_attachments ADD COLUMN master_uid TEXT",
        "CREATE INDEX IF NOT EXISTS idx_attachments_master ON api_attachments(master_uid)",
    ]


MIGRATIONS: List[Migration] = [
    (1, "masters", _masters_v1),
    (2, "ticket_members", _ticket_members_v1),
    (3, "messages", _messages_v1),
    (4, "attachments", _attachments_v1),
    (5, "events", _events_v1),
    (6, "outbox", _outbox_v1),
    (7, "tickets_extra", _tickets_extra_v1),
    (8, "ticket_seq", _ticket_seq_v1),
    (9, "read_state", _read_state_v1),
    (10, "declines", _declines_v1),
    (11, "tickets_workshop", _tickets_workshop_v11),
    (12, "ticket_owner", _ticket_owner_v12),
    (13, "attachment_owner", _attachment_owner_v13),
]

#: Версия схемы = номер последней миграции. Считается из списка, чтобы
#: добавление миграции не требовало ручной правки константы.
SCHEMA_VERSION: int = max(step for step, _, _ in MIGRATIONS)


def _current_version(connection: sqlite3.Connection) -> int:
    return connection.execute("PRAGMA user_version").fetchone()[0]


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


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
        # База может быть пустой (сервер без бота) — сначала legacy-таблицы,
        # иначе миграции с ALTER TABLE упадут на отсутствующей tickets.
        _create_legacy_tables(connection)

        for step, name, statements in MIGRATIONS:
            if step <= version:
                continue
            for statement in statements():
                _run_migration(connection, statement)
            logger.info("Миграция %d (%s) применена", step, name)

        version = _current_version(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    logger.info("Схема мессенджера готова, версия %d", SCHEMA_VERSION)
    return SCHEMA_VERSION


def _run_migration(connection: sqlite3.Connection, statement: str) -> None:
    """Выполняет одну инструкцию, пропуская повторные ALTER TABLE."""
    stripped = statement.strip()
    if stripped.upper().startswith("ALTER TABLE"):
        table, column = _parse_alter_add_column(stripped)
        if table and column and _column_exists(connection, table, column):
            logger.debug("Колонка %s.%s уже есть, пропуск", table, column)
            return
    connection.execute(stripped)


def _parse_alter_add_column(statement: str) -> Tuple[str, str]:
    """Достаёт имя таблицы и колонки из 'ALTER TABLE t ADD COLUMN c TYPE'.

    Порядок слов не фиксирован (можно ALTER TABLE t ADD c TYPE), поэтому
    ищем по ключевым словам, а не по индексам.
    """
    parts = [part for part in statement.split() if part]
    if len(parts) < 4:
        return "", ""
    try:
        table = parts[parts.index("TABLE") + 1]
    except (ValueError, IndexError):
        return "", ""
    column = ""
    for keyword in ("COLUMN", "ADD"):
        if keyword in parts:
            after = parts.index(keyword) + 1
            if after < len(parts):
                column = parts[after]
                break
    return table, column


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
        # Колонки в tickets не удаляем: SQLite не умеет DROP COLUMN.
        connection.execute("PRAGMA user_version = 0")
