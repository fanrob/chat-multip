import sqlite3
import logging
from datetime import datetime
from typing import Optional

from config import FULL_DB
from models import Ticket

logger = logging.getLogger(__name__)


class TicketStorage:
    def __init__(self, db_path: str = FULL_DB):
        self.db_path = db_path
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
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
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tickets_workshop ON tickets(workshop_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tickets_owner ON tickets(owner_master_uid)"
            )

    def get_all_ids(self) -> set[str]:
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute("SELECT id FROM tickets").fetchall()
        return {row[0] for row in rows if row[0]}

    @staticmethod
    def from_row(row: sqlite3.Row) -> Ticket:
        """Заявка из строки таблицы. Нужен и хранилищу, и поиску по теме в mail_bot."""
        return Ticket(
            id=row['id'],
            source=row['source'],
            client_id=row['client_id'],
            client_name=row['client_name'],
            text=row['text'],
            status=row['status'],
            created_at=datetime.fromisoformat(row['created_at']),
            message_id=row['message_id'],
            subject=row['subject'],
            workshop_id=row['workshop_id'],
        )

    def add(self, ticket: Ticket):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO tickets
                    (id, source, client_id, client_name, text, status, created_at, message_id, subject, workshop_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    client_id = excluded.client_id,
                    client_name = excluded.client_name,
                    text = excluded.text,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    message_id = excluded.message_id,
                    subject = excluded.subject,
                    workshop_id = excluded.workshop_id
                """,
                (ticket.id, ticket.source, ticket.client_id, ticket.client_name,
                 ticket.text, ticket.status, ticket.created_at.isoformat(),
                 ticket.message_id, ticket.subject, ticket.workshop_id),
            )


    def get(self, ticket_id: str) -> Optional[Ticket]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
        return self.from_row(row) if row else None

    def update(self, ticket: Ticket):
        self.add(ticket)

    def get_open_ticket(self, client_id: str) -> Optional[Ticket]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM tickets WHERE client_id = ? AND status != 'closed' ORDER BY created_at DESC LIMIT 1",
                (client_id,)
            ).fetchone()
        return self.from_row(row) if row else None


ticket_storage = TicketStorage()


class OperatorMessageStorage:
    """Карта message_id сообщения клиенту -> заявка.

    Ключ — message_id отправленного клиенту сообщения: по реплаю на него бот
    понимает, в какую заявку попадёт ответ клиента.
    """

    def __init__(self, db_path: str = FULL_DB):
        self.db_path = db_path
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operator_messages (
                    message_id INTEGER PRIMARY KEY,
                    ticket_id TEXT NOT NULL
                )
                """
            )

    def add(self, message_id: int, ticket_id: str):
        """Запоминает, что сообщение (message_id) относится к заявке."""
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO operator_messages (message_id, ticket_id) "
                "VALUES (?, ?)",
                (message_id, ticket_id),
            )


    def get_ticket_id(self, message_id: int) -> Optional[str]:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT ticket_id FROM operator_messages WHERE message_id = ?",
                (message_id,)
            ).fetchone()
        return row[0] if row else None


operator_message_storage = OperatorMessageStorage()


class WorkshopStorage:
    """Цеха. Мастера живут в api_masters и управляются из приложения,
    в таблице masters бот больше не нуждается."""

    def __init__(self, db_path: str = FULL_DB):
        self.db_path = db_path
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workshops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL
                )
                """
            )

    # ==================== ЦЕХА ====================

    def add_workshop(self, name: str) -> int:
        """Создаёт цех с указанным названием и возвращает его ID."""
        name = name.strip()
        with sqlite3.connect(self.db_path) as connection:
            cursor = connection.execute(
                "INSERT INTO workshops (name) VALUES (?)", (name,)
            )
        return cursor.lastrowid

    def delete_workshop(self, workshop_id: int) -> int:
        """Удаляет цех. Мастера этого цеха получают нулевой цех (NULL).

        Возвращает количество мастеров, у которых был сброшен цех,
        либо -1, если цех с таким ID не найден.
        """
        with sqlite3.connect(self.db_path) as connection:
            reset = connection.execute(
                "UPDATE api_masters SET workshop_id = NULL WHERE workshop_id = ?",
                (workshop_id,)
            )
            deleted = connection.execute(
                "DELETE FROM workshops WHERE id = ?", (workshop_id,)
            )
        if deleted.rowcount == 0:
            return -1
        return reset.rowcount

    def get_workshops(self):
        """Возвращает список всех цехов."""
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                "SELECT * FROM workshops ORDER BY id"
            ).fetchall()    #сортируем по id, то есть в порядке создания

    def get_workshop_name(self, workshop_id: Optional[int]) -> Optional[str]:
        """Возвращает название цеха по ID (None, если цех не задан или не найден)."""
        if not workshop_id:
            return None
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT name FROM workshops WHERE id = ?", (workshop_id,)
            ).fetchone()
        return row[0] if row else None


workshop_storage = WorkshopStorage()


def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором."""
    with sqlite3.connect(workshop_storage.db_path) as connection:
        db = connection.execute(
            "SELECT value FROM settings WHERE key = 'admin_id'",
        ).fetchone()
        admin_id = db[0]
    return str(user_id) == admin_id


def get_admin_id() -> int:
    """Возвращает текущий ID администратора из настроек."""
    with sqlite3.connect(workshop_storage.db_path) as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = 'admin_id'",
        ).fetchone()
    return int(row[0]) if row else 0


# ==================== Мастера (api_masters) ====================
# Таблица masters (Telegram) удалена: мастеры работают только в приложении
# и живут в api_masters. Бот читает её для админ-панели и гейта по цеху.


def list_api_masters(active_only: bool = True):
    """Мастера для админ-панели бота (с названием цеха)."""
    with sqlite3.connect(workshop_storage.db_path) as connection:
        connection.row_factory = sqlite3.Row
        query = (
            "SELECT m.master_uid, m.full_name, m.workshop_id, m.is_active, "
            "m.last_seen_at, w.name AS workshop_name "
            "FROM api_masters m LEFT JOIN workshops w ON m.workshop_id = w.id"
        )
        if active_only:
            return connection.execute(
                query + " WHERE m.is_active = 1 ORDER BY m.full_name, m.master_uid"
            ).fetchall()
        return connection.execute(
            query + " ORDER BY m.full_name, m.master_uid"
        ).fetchall()


def workshop_has_masters(workshop_id: int) -> bool:
    """Есть ли в цехе хотя бы один активный мастер (из приложения)."""
    with sqlite3.connect(workshop_storage.db_path) as connection:
        return connection.execute(
            "SELECT 1 FROM api_masters WHERE workshop_id = ? AND is_active = 1",
            (workshop_id,),
        ).fetchone() is not None


def set_master_active(master_uid: str, active: bool) -> bool:
    """Отключает (или включает) мастера по master_uid. True, если состояние изменилось."""
    value = 1 if active else 0
    with sqlite3.connect(workshop_storage.db_path) as connection:
        cursor = connection.execute(
            "UPDATE api_masters SET is_active = ? WHERE master_uid = ? AND is_active != ?",
            (value, master_uid, value),
        )
    return cursor.rowcount > 0
