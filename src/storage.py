import sqlite3
import logging
from datetime import datetime
from typing import Optional

from config import FULL_DB, DEFAULT_OPERATOR_IDS
from models import Ticket
from email_utils import normalize_email_subject

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
                    taken_by INTEGER,
                    created_at TEXT NOT NULL,
                    message_id TEXT,
                    subject TEXT NOT NULL DEFAULT '',
                    close_prompted_at TEXT,
                    close_prompt_message_id INTEGER,
                    close_no_at TEXT,
                    workshop_id INTEGER
                )
                """
            )

    def get_all_ids(self) -> set[str]:
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute("SELECT id FROM tickets").fetchall()
        return {row[0] for row in rows if row[0]}

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Ticket:
        return Ticket(
            id=row['id'],
            source=row['source'],
            client_id=row['client_id'],
            client_name=row['client_name'],
            text=row['text'],
            status=row['status'],
            taken_by=row['taken_by'],
            created_at=datetime.fromisoformat(row['created_at']),
            message_id=row['message_id'],
            subject=row['subject'],
            close_prompted_at=row['close_prompted_at'],
            close_prompt_message_id=row['close_prompt_message_id'],
            close_no_at=row['close_no_at'],
            workshop_id=row['workshop_id'],
        )

    def add(self, ticket: Ticket):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO tickets
                    (id, source, client_id, client_name, text, status, taken_by, created_at, message_id, subject, close_prompted_at, close_prompt_message_id, close_no_at, workshop_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    client_id = excluded.client_id,
                    client_name = excluded.client_name,
                    text = excluded.text,
                    status = excluded.status,
                    taken_by = excluded.taken_by,
                    created_at = excluded.created_at,
                    message_id = excluded.message_id,
                    subject = excluded.subject,
                    close_prompted_at = excluded.close_prompted_at,
                    close_prompt_message_id = excluded.close_prompt_message_id,
                    close_no_at = excluded.close_no_at,
                    workshop_id = excluded.workshop_id
                """,
                (ticket.id, ticket.source, ticket.client_id, ticket.client_name,
                 ticket.text, ticket.status, ticket.taken_by, ticket.created_at.isoformat(),
                 ticket.message_id, ticket.subject, ticket.close_prompted_at,
                 ticket.close_prompt_message_id, ticket.close_no_at, ticket.workshop_id),
            )


    def get(self, ticket_id: str) -> Optional[Ticket]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def update(self, ticket: Ticket):
        self.add(ticket)

    def get_open_ticket(self, client_id: str) -> Optional[Ticket]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM tickets WHERE client_id = ? AND status IN ('new', 'taken') ORDER BY created_at DESC LIMIT 1",
                (client_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def get_operator_tickets(self, operator_id: int) -> list[Ticket]:
        """Получает все активные (взятые в работу) заявки мастера."""
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM tickets WHERE taken_by = ? AND status IN ('new', 'taken') ORDER BY created_at DESC",
                (operator_id,)
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get_operator_all_tickets(self, operator_id: int, limit: int = 20) -> list[Ticket]:
        """Все заявки мастера для выбора: сначала открытые, затем недавно закрытые."""
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM tickets WHERE taken_by = ? "
                "ORDER BY (status IN ('new', 'taken')) DESC, created_at DESC LIMIT ?",
                (operator_id, limit)
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get_open_ticket_by_id(self, ticket_id: str) -> Optional[Ticket]:
        ticket = self.get(ticket_id)
        if ticket and ticket.status in {'new', 'taken'}:
            return ticket
        return None

    def get_open_email_ticket_by_subject(self, subject: str) -> Optional[Ticket]:
        normalized_subject = normalize_email_subject(subject)
        if not normalized_subject:
            return None

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM tickets WHERE source = 'email' "
                "AND status IN ('new', 'taken') ORDER BY created_at DESC"
            ).fetchall()
        for row in rows:
            if normalize_email_subject(row['subject']) == normalized_subject:
                return self._from_row(row)
        return None

    def get_ticket_by_message_id(self, message_id: int) -> Optional[Ticket]:
        """Получает заявку по ID сообщения мастера."""
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT t.* FROM tickets t "
                "INNER JOIN operator_messages om ON t.id = om.ticket_id "
                "WHERE om.message_id = ?",
                (message_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def get_active_tickets(self) -> list[Ticket]:
        """Возвращает все открытые заявки (статус new или taken)."""
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM tickets WHERE status IN ('new', 'taken') ORDER BY created_at"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get_last_activity_time(self, ticket_id: str) -> Optional[datetime]:
        """Возвращает время последнего сообщения в диалоге по заявке.

        Учитываются и создание заявки, и все сообщения (мастеров и клиентов),
        и последнее нажатие «Нет» (перезапускает отсчёт 14 дней).
        """
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT MAX(ts) AS last_ts FROM (
                    SELECT created_at AS ts FROM tickets WHERE id = ?
                    UNION ALL
                    SELECT sent_at AS ts FROM operator_messages WHERE ticket_id = ?
                    UNION ALL
                    SELECT close_no_at AS ts FROM tickets WHERE id = ? AND close_no_at IS NOT NULL
                )
                """,
                (ticket_id, ticket_id, ticket_id)
            ).fetchone()
        raw = row[0] if row else None
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None


ticket_storage = TicketStorage()


class OperatorMessageStorage:
    """Отслеживание сообщений мастеров для определения заявки при ответе."""

    def __init__(self, db_path: str = FULL_DB):
        self.db_path = db_path
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
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
                """
            )

    def add(self, message_id: int, operator_id: int, ticket_id: str,
            sender: str = '', sender_id: str = None, text: str = ''):
        """Сохраняет сообщение.
        sender: 'op' или 'client' — кто автор сообщения (для диалога).
        Если sender пустой, сообщение считается служебным и в диалог не попадает.
        """
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO operator_messages "
                "(message_id, operator_id, ticket_id, sent_at, sender, sender_id, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_id, operator_id, ticket_id, datetime.now().isoformat(),
                 sender, sender_id, text),
            )

    def get_ticket_dialog(self, ticket: Ticket) -> list[dict]:
        """Возвращает переписку по заявке: [{'role','author','time','text'}, ...].

        Первое сообщение — текст самой заявки (от клиента).
        Дальше — сохранённые сообщения клиента и мастера (sender='client'/'op').
        """
        dialog = [{
            'role': 'client',
            'author': ticket.client_name,
            'time': ticket.created_at,
            'text': ticket.text,
        }]
        name_map = {}
        for row in master_storage.all(include_deleted=True):
            name_map[str(row['user_id'])] = row['full_name'] or str(row['user_id'])

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT sender, sender_id, text, sent_at FROM operator_messages "
                "WHERE ticket_id = ? AND sender IN ('op', 'client') AND text != '' "
                "ORDER BY sent_at, message_id",
                (ticket.id,)
            ).fetchall()

        for row in rows:
            role = row['sender']
            if role == 'op':
                author = name_map.get(str(row['sender_id']), f"Мастер {row['sender_id']}")
            else:
                author = ticket.client_name
            try:
                time = datetime.fromisoformat(row['sent_at'])
            except (ValueError, TypeError):
                time = ticket.created_at
            dialog.append({
                'role': role,
                'author': author,
                'time': time,
                'text': row['text'],
            })
        return dialog

    def get_ticket_id(self, message_id: int) -> Optional[str]:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT ticket_id FROM operator_messages WHERE message_id = ?",
                (message_id,)
            ).fetchone()
        return row[0] if row else None

    def get_last_ticket_id(self, operator_id: int) -> Optional[str]:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT ticket_id FROM operator_messages WHERE operator_id = ? ORDER BY sent_at DESC LIMIT 1",
                (operator_id,)
            ).fetchone()
        return row[0] if row else None

    def get_last_open_ticket_id(self, operator_id: int) -> Optional[str]:
        """ Получает ID последней открытой заявки для заданного мастера """
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT om.ticket_id
                FROM operator_messages om
                INNER JOIN tickets t ON t.id = om.ticket_id
                WHERE om.operator_id = ? AND t.status = 'taken'
                ORDER BY om.sent_at DESC LIMIT 1
                """,
                (operator_id,)
            ).fetchone()
        return row[0] if row else None


operator_message_storage = OperatorMessageStorage()


class MasterStorage:
    """Хранение мастеров в БД с поддержкой мягкого удаления и цехов."""

    def __init__(self, db_path: str = FULL_DB):
        self.db_path = db_path
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS masters (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT,
                    added_at TEXT NOT NULL,
                    is_deleted INTEGER DEFAULT 0,
                    workshop_id INTEGER
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workshops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL
                )
                """
            )

            fresh_db = connection.execute(
                "SELECT COUNT(*) FROM masters"
            ).fetchone()[0] == 0

        if fresh_db:
            for user_id in DEFAULT_OPERATOR_IDS:
                self.add(user_id)

    def add(self, user_id: int, full_name: str = "", workshop_id: Optional[int] = None):
        """Добавляет мастера. Если был удален - восстанавливает."""
        with sqlite3.connect(self.db_path) as connection:
            # Проверяем, существует ли мастер
            existing = connection.execute(
                "SELECT is_deleted, workshop_id FROM masters WHERE user_id = ?", (user_id,)
            ).fetchone()
            
            if existing:
                if existing[0] == 1:  # Был удален
                    # Восстанавливаем (цех, если не передан, сохраняем старый)
                    new_workshop = workshop_id if workshop_id is not None else existing[1]
                    connection.execute(
                        "UPDATE masters SET is_deleted = 0, full_name = ?, workshop_id = ? WHERE user_id = ?",
                        (full_name or "", new_workshop, user_id)
                    )
                    logger.info(f"🔄 мастер {user_id} ({full_name}) восстановлен!")
                else:
                    # Просто обновляем имя/цех, если они переданы
                    updates = []
                    params = []
                    if full_name:
                        updates.append("full_name = ?")
                        params.append(full_name)
                    if workshop_id is not None:
                        updates.append("workshop_id = ?")
                        params.append(workshop_id)
                    if updates:
                        params.append(user_id)
                        connection.execute(
                            f"UPDATE masters SET {', '.join(updates)} WHERE user_id = ?",
                            tuple(params)
                        )
            else:
                # Новый мастер
                connection.execute(
                    "INSERT INTO masters (user_id, full_name, added_at, is_deleted, workshop_id) VALUES (?, ?, ?, 0, ?)",
                    (user_id, full_name, datetime.now().isoformat(), workshop_id),
                )

    def delete(self, user_id: int) -> bool:
        """Мягко удаляет мастера (помечает как удаленного)."""
        with sqlite3.connect(self.db_path) as connection:
            cursor = connection.execute(
                "UPDATE masters SET is_deleted = 1 WHERE user_id = ? AND is_deleted = 0",
                (user_id,)
            )
        return cursor.rowcount > 0

    def restore(self, user_id: int) -> bool:
        """Восстанавливает удаленного мастера."""
        with sqlite3.connect(self.db_path) as connection:
            cursor = connection.execute(
                "UPDATE masters SET is_deleted = 0 WHERE user_id = ? AND is_deleted = 1",
                (user_id,)
            )
        return cursor.rowcount > 0

    def all(self, include_deleted: bool = False):
        """Возвращает список мастеров (с названием цеха). По умолчанию только активных."""
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            query = (
                "SELECT m.user_id, m.full_name, m.added_at, m.is_deleted, m.workshop_id, "
                "w.name AS workshop_name "
                "FROM masters m LEFT JOIN workshops w ON m.workshop_id = w.id"
            )
            if include_deleted:
                return connection.execute(query + " ORDER BY m.added_at").fetchall()
            else:
                return connection.execute(
                    query + " WHERE m.is_deleted = 0 ORDER BY m.added_at"
                ).fetchall()

    def exists(self, user_id: int) -> bool:
        """Проверяет, существует ли активный мастер."""
        with sqlite3.connect(self.db_path) as connection:
            return connection.execute(
                "SELECT 1 FROM masters WHERE user_id = ? AND is_deleted = 0", (user_id,)
            ).fetchone() is not None

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
                "UPDATE masters SET workshop_id = NULL WHERE workshop_id = ?",
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
                "SELECT * FROM workshops ORDER BY name"
            ).fetchall()

    def get_workshop_name(self, workshop_id: Optional[int]) -> Optional[str]:
        """Возвращает название цеха по ID (None, если цех не задан или не найден)."""
        if not workshop_id:
            return None
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT name FROM workshops WHERE id = ?", (workshop_id,)
            ).fetchone()
        return row[0] if row else None


master_storage = MasterStorage()


def is_operator(user_id: int) -> bool:
    return master_storage.exists(user_id)

def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором."""
    with sqlite3.connect(master_storage.db_path) as connection:
        db = connection.execute(
            "SELECT value FROM settings WHERE key = 'admin_id'",
        ).fetchone()
        admin_id = db[0]
    return str(user_id) == admin_id


def get_admin_id() -> int:
    """Возвращает текущий ID администратора из настроек."""
    with sqlite3.connect(master_storage.db_path) as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = 'admin_id'",
        ).fetchone()
    return int(row[0]) if row else 0

def get_operator_ids(workshop_id: Optional[int] = None):
    """Возвращает ID мастеров. Если задан цех - только мастеров этого цеха."""
    if workshop_id is None:
        return [row["user_id"] for row in master_storage.all()]
    return [
        row["user_id"] for row in master_storage.all()
        if row["workshop_id"] == workshop_id
    ]
