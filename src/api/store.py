"""Работа с заявками, сообщениями и событиями.

Ключевая особенность этого файла: он НЕ создаёт свои копии заявок.
Читает и меняет существующую таблицу `tickets`, которой пользуется телеграм-бот.
Иначе появились бы две несовместимые ленты: бот видит одни заявки, клиент — другие.

Статусы единые для всех: `new` / `in_progress` / `closed` хранятся в tickets
как есть (бот создаёт `new`, закрывает в `closed`, API переводит в
`in_progress`). Никаких перекодировок.
"""

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from api import errors, events
from api.auth import Master
from api.db import reading, transaction, unit_of_work

logger = logging.getLogger(__name__)

# ==================== Статусы ====================

STATUS_NEW = "new"
STATUS_IN_PROGRESS = "in_progress"
STATUS_CLOSED = "closed"

#: Статусы, при которых заявка видна всем в общей ленте.
FEED_STATUS = (STATUS_NEW,)
#: Статусы заявок, к которым мастер имеет отношение.
ACTIVE_STATUSES = (STATUS_NEW, STATUS_IN_PROGRESS)


def _now() -> str:
    return datetime.now().isoformat()


# ==================== Идентификаторы ====================


def new_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:12]


def new_attachment_id() -> str:
    return "a_" + uuid.uuid4().hex[:12]


# ==================== Цеха ====================


def list_workshops() -> List[Dict[str, Any]]:
    """Цеха с количеством активных мастеров (api_masters)."""
    with reading() as connection:
        rows = connection.execute(
            """
            SELECT w.id, w.name, COUNT(m.master_uid) AS masters_count
            FROM workshops w
            LEFT JOIN api_masters m ON m.workshop_id = w.id AND m.is_active = 1
            GROUP BY w.id, w.name
            ORDER BY w.name
            """
        ).fetchall()
    return [
        {"id": f"w_{row['id']}", "name": row["name"], "masters_count": row["masters_count"]}
        for row in rows
    ]


def workshop_id_to_db(api_workshop_id: Optional[str]) -> Optional[int]:
    """'w_3' -> 3. None, если не задан."""
    if not api_workshop_id:
        return None
    return int(api_workshop_id.removeprefix("w_"))


def workshop_id_to_api(db_workshop_id: Optional[int]) -> Optional[str]:
    return f"w_{db_workshop_id}" if db_workshop_id is not None else None


# ==================== Мастера ====================


def list_masters(
    query: Optional[str] = None,
    workshop_id: Optional[str] = None,
    include_self: bool = False,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Справочник мастеров для подключения второго участника."""
    sql = [
        """
        SELECT m.*, w.name AS workshop_name,
               (SELECT COUNT(*) FROM tickets t
                 WHERE t.status = 'in_progress'
                   AND t.owner_master_uid = m.master_uid
               ) AS active_tickets,
               m.last_seen_at AS seen
        FROM api_masters m
        LEFT JOIN workshops w ON w.id = m.workshop_id
        WHERE m.is_active = 1
        """
    ]
    params: List[Any] = []

    if query:
        sql.append("AND m.full_name LIKE ?")
        params.append(f"%{query}%")
    db_workshop = workshop_id_to_db(workshop_id)
    if db_workshop is not None:
        sql.append("AND m.workshop_id = ?")
        params.append(db_workshop)
    sql.append("ORDER BY m.full_name LIMIT ?")
    params.append(limit)

    with reading() as connection:
        rows = connection.execute(" ".join(sql), params).fetchall()

    return [_master_to_brief(row) for row in rows]


def _master_to_brief(row: sqlite3.Row, role: Optional[str] = None) -> Dict[str, Any]:
    """Мастер -> MasterBrief.

    online считаем по last_seen_at: мастер активен, если виден в последние 2 минуты.
    Точный heartbeat не нужен — достаточно отличать «приложение открыто» от
    «рабочее место выключено», а heartbeat в README ещё не формализован.
    """
    return {
        "id": row["master_uid"],
        "full_name": row["full_name"] or "",
        "workshop_id": workshop_id_to_api(row["workshop_id"]),
        "workshop_name": row["workshop_name"],
        "online": is_online(row["last_seen_at"]),
        "active_tickets": row["active_tickets"] if "active_tickets" in row.keys() else 0,
        "role": role,
    }


ONLINE_WINDOW_SECONDS = 120


def is_online(last_seen_at: Optional[str]) -> bool:
    if not last_seen_at:
        return False
    try:
        seen = datetime.fromisoformat(last_seen_at)
    except ValueError:
        return False
    return (datetime.now() - seen).total_seconds() <= ONLINE_WINDOW_SECONDS


def get_master_brief(master_uid: str) -> Optional[Dict[str, Any]]:
    """Карточка мастера или None."""
    with reading() as connection:
        row = connection.execute(
            """
            SELECT m.*, w.name AS workshop_name, 0 AS active_tickets
            FROM api_masters m
            LEFT JOIN workshops w ON w.id = m.workshop_id
            WHERE m.master_uid = ? AND m.is_active = 1
            """,
            (master_uid,),
        ).fetchone()
    return _master_to_brief(row) if row else None


def _owner_of(connection: sqlite3.Connection, ticket_id: str) -> Optional[Dict[str, str]]:
    """Текущий владелец заявки: {id, full_name} или None."""
    row = connection.execute(
        """
        SELECT t.owner_master_uid, m.master_uid, m.full_name
        FROM tickets t
        LEFT JOIN api_masters m ON m.master_uid = t.owner_master_uid
        WHERE t.id = ?
        """,
        (ticket_id,),
    ).fetchone()
    if not row:
        return None
    if row["owner_master_uid"]:
        return {"id": row["owner_master_uid"], "full_name": row["full_name"] or ""}
    return None


# ==================== Заявки ====================


#: JOIN на владельца заявки — мастеру из api_masters по owner_master_uid.
_OWNER_JOIN = (
    "LEFT JOIN api_masters o ON o.master_uid = t.owner_master_uid"
)


def _owner_uid_sql(ticket_alias: str = "t") -> str:
    """Выражение, дающее master_uid владельца заявки (или NULL)."""
    return f"{ticket_alias}.owner_master_uid"


def _ticket_row_sql(master_uid: str, ticket_id: Optional[str] = None) -> str:
    """Базовый SELECT заявки с owner и участниками.

    Сделано одним запросом с LEFT JOIN, чтобы не делать N+1 запросов
    на каждую заявку списка: при 50 заявках это 100+ обращений к БД вместо 1.
    """
    return f"""
        SELECT t.*,
               o.master_uid AS owner_uid,
               o.full_name  AS owner_name,
               w.name       AS workshop_name,
               (SELECT COUNT(*) FROM ticket_members tm WHERE tm.ticket_id = t.id) AS members_count
        FROM tickets t
        {_OWNER_JOIN}
        LEFT JOIN workshops w   ON w.id = COALESCE(t.workshop_id, o.workshop_id)
        WHERE t.id = ?
    """


def _hydrate_ticket(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    master_uid: str,
    *,
    detailed: bool = False,
) -> Dict[str, Any]:
    """Превращает строку tickets в структуру для ответа API."""
    ticket_id = row["id"]
    members = _ticket_members(connection, ticket_id)
    last = _last_message(connection, ticket_id)
    unread = _unread_count(connection, ticket_id, master_uid)

    owner = None
    if row["owner_uid"]:
        owner_row = connection.execute(
            """
            SELECT m.*, w.name AS workshop_name, 0 AS active_tickets
            FROM api_masters m LEFT JOIN workshops w ON w.id = m.workshop_id
            WHERE m.master_uid = ?
            """,
            (row["owner_uid"],),
        ).fetchone()
        if owner_row:
            owner = _master_to_brief(owner_row, role="owner")

    data: Dict[str, Any] = {
        "id": ticket_id,
        "status": row["status"],
        "workshop_id": workshop_id_to_api(row["workshop_id"]),
        "workshop_name": row["workshop_name"],
        "subject": row["subject"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"] or row["created_at"],
        "client": {
            "id": row["client_id"],
            "display_name": row["client_name"] or "Клиент",
            "phone": None,
        },
        "owner": owner,
        "members": members,
        "last_message": last,
        "unread_count": unread,
    }
    if detailed:
        data["text"] = row["text"] or ""
        data["source"] = row["source"]
        data["closed_at"] = row["closed_at"]
        data["close_reason"] = row["close_reason"]
    return data


def _ticket_members(connection: sqlite3.Connection, ticket_id: str) -> List[Dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT tm.master_uid, tm.role, tm.joined_at, m.full_name
        FROM ticket_members tm
        LEFT JOIN api_masters m ON m.master_uid = tm.master_uid
        WHERE tm.ticket_id = ?
        ORDER BY tm.joined_at
        """,
        (ticket_id,),
    ).fetchall()
    return [
        {
            "master_id": row["master_uid"],
            "full_name": row["full_name"] or "",
            "role": row["role"],
            "joined_at": row["joined_at"],
        }
        for row in rows
    ]


def _last_message(connection: sqlite3.Connection, ticket_id: str) -> Optional[Dict[str, Any]]:
    row = connection.execute(
        "SELECT seq, sender, text FROM messages WHERE ticket_id = ? ORDER BY seq DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()
    if not row:
        return None
    preview = row["text"] or ""
    return {"seq": row["seq"], "sender": row["sender"], "preview": preview[:200]}


def _unread_count(connection: sqlite3.Connection, ticket_id: str, master_uid: str) -> int:
    """Сколько сообщений клиента мастер ещё не отметил прочитанным.

    Считаем по read_cursor мастера, а не по глобальному read_at: у каждого
    своё состояние прочтения.
    """
    row = connection.execute(
        "SELECT read_cursor FROM ticket_read_state WHERE ticket_id = ? AND master_uid = ?",
        (ticket_id, master_uid),
    ).fetchone()
    cursor = row["read_cursor"] if row else 0
    counted = connection.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE ticket_id = ? AND seq > ? AND sender = 'client'",
        (ticket_id, cursor),
    ).fetchone()
    return counted["n"]


def _load_ticket(
    connection: sqlite3.Connection, master_uid: str, ticket_id: str
) -> Dict[str, Any]:
    """Читает и наполняет карточку заявки в уже открытой транзакции.

    Вызывается сразу после UPDATE, чтобы в событие ушло актуальное состояние.
    """
    row = connection.execute(_ticket_row_sql(master_uid, ticket_id), (ticket_id,)).fetchone()
    if not row:
        raise errors.ticket_not_found(ticket_id)
    return _hydrate_ticket(connection, row, master_uid, detailed=True)


def get_ticket(master_uid: str, ticket_id: str, *, detailed: bool = True) -> Dict[str, Any]:
    """Карточка заявки. Бросает 404, если заявки нет или она скрыта от мастера."""
    with reading() as connection:
        row = connection.execute(_ticket_row_sql(master_uid, ticket_id), (ticket_id,)).fetchone()
        if not row:
            raise errors.ticket_not_found(ticket_id)
        if not is_visible(connection, ticket_id, master_uid):
            raise errors.ticket_not_found(ticket_id)
        return _hydrate_ticket(connection, row, master_uid, detailed=detailed)


def is_visible(connection: sqlite3.Connection, ticket_id: str, master_uid: str) -> bool:
    """Видна ли заявка мастеру.

    Видно всё, кроме скрытых самим мастером (decline) и заявок участников
    чужого мастера, если тот скрыл их у себя.
    """
    row = connection.execute(
        """
        SELECT COUNT(*) AS n FROM ticket_declines
        WHERE ticket_id = ? AND master_uid = ?
        """,
        (ticket_id, master_uid),
    ).fetchone()
    if row["n"]:
        return False

    ticket = connection.execute(
        "SELECT status FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    if not ticket:
        return False

    # Закрытые заявки показываем всем — по ним может понадобиться история.
    if ticket["status"] == STATUS_CLOSED:
        return True

    # Заявку в работе показываем только её участникам (владельцу и подключённым).
    if ticket["status"] == STATUS_IN_PROGRESS:
        return is_member(connection, ticket_id, master_uid)

    return True


def is_member(connection: sqlite3.Connection, ticket_id: str, master_uid: str) -> bool:
    """Участник ли мастер заявки.

    Два случая: мастер в ticket_members либо мастер — владелец по
    owner_master_uid.
    """
    row = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM ticket_members tm
              WHERE tm.ticket_id = t.id AND tm.master_uid = ?) AS in_members,
            (SELECT COUNT(*) FROM api_masters m
              WHERE m.master_uid = ? AND m.master_uid = t.owner_master_uid) AS is_api_owner
        FROM tickets t WHERE t.id = ?
        """,
        (master_uid, master_uid, ticket_id),
    ).fetchone()
    if not row:
        return False
    return bool(row["in_members"] or row["is_api_owner"])


def require_member(connection: sqlite3.Connection, ticket_id: str, master_uid: str) -> None:
    if not is_member(connection, ticket_id, master_uid):
        raise errors.not_ticket_member(ticket_id)


def require_open(connection: sqlite3.Connection, ticket_id: str) -> None:
    """Запрещает действия с закрытой заявкой."""
    row = connection.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not row:
        raise errors.ticket_not_found(ticket_id)
    if row["status"] == STATUS_CLOSED:
        raise errors.ticket_closed(ticket_id)


def list_tickets(
    master_uid: str,
    scope: str = "feed",
    status_filter: Optional[str] = None,
    workshop_id: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 50,
    before: Optional[str] = None,
) -> Dict[str, Any]:
    """Список заявок: лента / мои / все.

    scope:
      feed — status=new и не скрытые мастером (то, что можно взять),
      mine — заявки, где мастер участник,
      all  — всё, что мастеру видно.
    """
    clauses: List[str] = []
    params: List[Any] = []

    if scope == "feed":
        # Лента: заявки в статусе new, которые этот мастер не отклонил.
        clauses.append("t.status = 'new'")
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM ticket_declines d "
            "WHERE d.ticket_id = t.id AND d.master_uid = ?)"
        )
        params.append(master_uid)
    elif scope == "mine":
        # Мои: мастер в ticket_members либо владелец заявки.
        clauses.append(
            "(EXISTS (SELECT 1 FROM ticket_members tm "
            "    WHERE tm.ticket_id = t.id AND tm.master_uid = ?) "
            " OR t.owner_master_uid = ?)"
        )
        params.extend([master_uid, master_uid])
    # scope == 'all' — без дополнительных условий, видимость фильтруется ниже

    if status_filter:
        clauses.append("t.status = ?")
        params.append(status_filter)

    db_workshop = workshop_id_to_db(workshop_id)
    if db_workshop is not None:
        # Цех заявки: собственный, иначе цех её владельца.
        clauses.append(
            "COALESCE(t.workshop_id, (SELECT m.workshop_id FROM api_masters m "
            "  WHERE m.master_uid = t.owner_master_uid)) = ?"
        )
        params.append(db_workshop)

    if query:
        clauses.append("(t.subject LIKE ? OR t.text LIKE ? OR t.client_name LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like, like])

    if before:
        clauses.append("t.id < ?")
        params.append(before)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with reading() as connection:
        rows = connection.execute(
            f"""
SELECT t.id, t.status, t.workshop_id, t.subject, t.created_at, t.updated_at,
               t.client_id, t.client_name, t.owner_master_uid,
               o.master_uid AS owner_uid,
               w.name AS workshop_name
            FROM tickets t
            {_OWNER_JOIN}
            LEFT JOIN workshops w   ON w.id = COALESCE(t.workshop_id, o.workshop_id)
            {where}
            ORDER BY t.created_at DESC
            LIMIT ?
            """,
            [*params, limit + 1],
        ).fetchall()

        # scope=all ограничен видимостью: чужие заявки в работе и скрытые — не показываем.
        visible = [row for row in rows if is_visible(connection, row["id"], master_uid)]
        page = visible[:limit]

        tickets = [_hydrate_ticket(connection, row, master_uid) for row in page]

    return {
        "tickets": tickets,
        "next_before": page[-1]["id"] if len(visible) > limit and page else None,
    }


# ==================== Сообщения ====================


def _next_seq(connection: sqlite3.Connection, ticket_id: str) -> int:
    """Сквозной номер сообщения внутри заявки.

    Счётчик в отдельной таблице ticket_seq: при двух мастерах, пишущих
    одновременно, MAX(seq)+1 у обоих дал бы один и тот же номер.
    """
    connection.execute(
        "INSERT INTO ticket_seq (ticket_id, last_seq) VALUES (?, 0) "
        "ON CONFLICT(ticket_id) DO NOTHING",
        (ticket_id,),
    )
    connection.execute(
        "UPDATE ticket_seq SET last_seq = last_seq + 1 WHERE ticket_id = ?",
        (ticket_id,),
    )
    row = connection.execute(
        "SELECT last_seq FROM ticket_seq WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    return row["last_seq"]


def list_messages(
    master_uid: str,
    ticket_id: str,
    after_seq: Optional[int] = None,
    before_seq: Optional[int] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """История сообщений с пагинацией в обе стороны."""
    require_member_or_visible(ticket_id, master_uid)

    clauses = ["ticket_id = ?"]
    params: List[Any] = [ticket_id]
    if after_seq is not None:
        clauses.append("seq > ?")
        params.append(after_seq)
    if before_seq is not None:
        clauses.append("seq < ?")
        params.append(before_seq)

    with reading() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM messages WHERE {' AND '.join(clauses)}
            ORDER BY seq DESC LIMIT ?
            """,
            [*params, limit + 1],
        ).fetchall()
        has_more_before = len(rows) > limit
        messages = [_hydrate_message(connection, row) for row in reversed(rows[:limit])]

        last = connection.execute(
            "SELECT last_seq FROM ticket_seq WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        last_seq = last["last_seq"] if last else 0

    # has_more_after: есть ли что-то новее запрошенного курсора.
    has_more_after = after_seq is not None and last_seq > after_seq

    return {
        "messages": messages,
        "has_more_before": has_more_before,
        "has_more_after": has_more_after,
    }


def require_member_or_visible(ticket_id: str, master_uid: str) -> None:
    """История доступна участникам; остальным — только если заявка в ленте."""
    with reading() as connection:
        exists = connection.execute(
            "SELECT 1 FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not exists:
            raise errors.ticket_not_found(ticket_id)
        if is_member(connection, ticket_id, master_uid):
            return
        if not is_visible(connection, ticket_id, master_uid):
            raise errors.ticket_not_found(ticket_id)


def _hydrate_message(connection: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    attachments = connection.execute(
        """
        SELECT id, filename, mime_type, size_bytes, source, created_at
        FROM api_attachments WHERE message_id = ? ORDER BY created_at
        """,
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "seq": row["seq"],
        "ticket_id": row["ticket_id"],
        "sender": row["sender"],
        "sender_master_id": row["master_uid"],
        "sender_name": row["sender_name"] or "",
        "text": row["text"] or "",
        "attachments": [
            {
                "attachment_id": item["id"],
                "kind": _attachment_kind(item["mime_type"]),
                "filename": item["filename"] or "",
                "mime_type": item["mime_type"] or "",
                "size": item["size_bytes"] or 0,
                "source": item["source"],
                "created_at": item["created_at"],
            }
            for item in attachments
        ],
        "created_at": row["created_at"],
        "delivery": row["delivery"],
        "read_at": row["read_at"],
    }


def _attachment_kind(mime_type: str) -> str:
    return "photo" if (mime_type or "").startswith("image/") else "file"


def get_message_by_client_id(master_uid: str, client_msg_id: str) -> Optional[Dict[str, Any]]:
    """Поиск ранее отправленного сообщения — для идемпотентности."""
    with reading() as connection:
        row = connection.execute(
            "SELECT * FROM messages WHERE master_uid = ? AND client_msg_id = ?",
            (master_uid, client_msg_id),
        ).fetchone()
        return _hydrate_message(connection, row) if row else None


def get_source_message(client_msg_id: str) -> Optional[Dict[str, Any]]:
    """Поиск сообщения, пришедшего из мессенджера, — для идемпотентности моста."""
    with reading() as connection:
        row = connection.execute(
            "SELECT * FROM messages WHERE sender = 'client' AND client_msg_id = ?",
            (client_msg_id,),
        ).fetchone()
        return _hydrate_message(connection, row) if row else None


def ticket_channel(connection: sqlite3.Connection, ticket_id: str) -> str:
    """Канал доставки ответа клиенту: почта для email-заявок, иначе Telegram.

    Заявка пришла по почте — и ответ должен уйти по почте, тем же процессом
    mail_bot. Канал хранится в очереди, поэтому каждый консьюмер забирает
    только свои строки и не трогает чужие.
    """
    row = connection.execute(
        "SELECT source FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    return "email" if row and row["source"] == "email" else "telegram"


def create_message(
    master: Master,
    ticket_id: str,
    client_msg_id: str,
    text: str,
    attachment_ids: Sequence[str],
) -> Dict[str, Any]:
    """Сохраняет сообщение мастера и ставит его в очередь доставки.

    Идемпотентно по (master_uid, client_msg_id): повторный POST вернёт то же
    сообщение, а не создаст дубль в канале. Это важно: клиент может
    переотправить запрос при потере ответа.
    """
    existing = get_message_by_client_id(master.master_uid, client_msg_id)
    if existing:
        logger.debug("Повторная отправка client_msg_id=%s, вернули существующее", client_msg_id)
        return existing

    now = _now()
    message_id = new_message_id()

    with transaction() as connection:
        require_open(connection, ticket_id)
        require_member(connection, ticket_id, master.master_uid)
        channel = ticket_channel(connection, ticket_id)

        # Вложения должны существовать и принадлежать этой заявке.
        for attachment_id in attachment_ids:
            row = connection.execute(
                "SELECT ticket_id, master_uid FROM api_attachments WHERE id = ?",
                (attachment_id,),
            ).fetchone()
            if not row:
                raise errors.attachment_missing(message_id, attachment_id)
            if row["ticket_id"] is None:
                # Файл ещё не привязан: оформить его в сообщении может только
                # тот мастер, который загрузил. Иначе любой, узнав id, смог бы
                # отправить чужой файл клиенту от своего имени.
                if row["master_uid"] != master.master_uid:
                    raise errors.attachment_missing(message_id, attachment_id)
            elif row["ticket_id"] != ticket_id:
                raise errors.attachment_missing(message_id, attachment_id)

        seq = _next_seq(connection, ticket_id)

        try:
            connection.execute(
                """
                INSERT INTO messages
                    (id, ticket_id, seq, sender, master_uid, sender_name, text,
                     channel, delivery, client_msg_id, created_at)
                VALUES (?, ?, ?, 'master', ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    message_id,
                    ticket_id,
                    seq,
                    master.master_uid,
                    master.full_name,
                    text,
                    channel,
                    client_msg_id,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            # Гонка: параллельный запрос с тем же client_msg_id выиграл первым.
            row = connection.execute(
                "SELECT * FROM messages WHERE master_uid = ? AND client_msg_id = ?",
                (master.master_uid, client_msg_id),
            ).fetchone()
            if row:
                return _hydrate_message(connection, row)
            raise

        # Привязываем вложения к сообщению.
        for attachment_id in attachment_ids:
            connection.execute(
                "UPDATE api_attachments SET message_id = ?, ticket_id = ? WHERE id = ?",
                (message_id, ticket_id, attachment_id),
            )

        connection.execute(
            """
            INSERT INTO api_outbox (ticket_id, message_id, channel, payload, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (
                ticket_id,
                message_id,
                channel,
                json.dumps(
                    {
                        "message_id": message_id,
                        "ticket_id": ticket_id,
                        "text": text,
                        "author": master.full_name,
                        "master_uid": master.master_uid,
                        "attachments": list(attachment_ids),
                    },
                    ensure_ascii=False,
                ),
                now,
            ),
        )

        connection.execute(
            "UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id)
        )

        row = connection.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        payload = _hydrate_message(connection, row)

        # Событие уходит участникам заявки. Отправитель тоже должен увидеть
        # сообщение в своей ленте, если не видит заявку (редко, но бывает при
        # гонке с принятием) — иначе его клиент не покажет собственное сообщение.
        recipients = set(members_with_owner(connection, ticket_id))
        if not is_visible(connection, ticket_id, master.master_uid):
            recipients.add(master.master_uid)
        events.emit(
            connection,
            recipients,
            events.MESSAGE_CREATED,
            {"message": payload},
            ticket_id,
        )

    return payload


def append_incoming_message(
    ticket_id: str,
    text: str,
    sender_name: str,
    *,
    channel: str = "telegram",
    client_msg_id: Optional[str] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Дописывает в заявку сообщение, пришедшее из мессенджера.

    Путь для бота: клиент написал в Telegram, сообщение уже ушло мастеру,
    поэтому delivery сразу delivered и в api_outbox ничего не попадает —
    повторная отправка в Telegram была бы дублем.

    Идемпотентно по client_msg_id (мост передаёт "tg:<chat_id>:<message_id>"):
    при перезапуске или повторной доставке апдейта дубль в истории клиента
    не появится. Если заявки нет или она закрыта, возвращается None — мост не
    должен ронять обработчик апдейта.
    """
    if client_msg_id:
        existing = get_source_message(client_msg_id)
        if existing:
            return existing

    def _work(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        ticket = conn.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not ticket or ticket["status"] == STATUS_CLOSED:
            return None

        now = _now()
        message_id = new_message_id()
        seq = _next_seq(conn, ticket_id)

        conn.execute(
            """
            INSERT INTO messages
                (id, ticket_id, seq, sender, sender_name, text,
                 channel, delivery, client_msg_id, created_at)
            VALUES (?, ?, ?, 'client', ?, ?, ?, 'delivered', ?, ?)
            """,
            (message_id, ticket_id, seq, sender_name, text, channel, client_msg_id, now),
        )
        conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id))

        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        payload = _hydrate_message(conn, row)

        events.emit(
            conn,
            set(members_with_owner(conn, ticket_id)),
            events.MESSAGE_CREATED,
            {"message": payload},
            ticket_id,
        )
        return payload

    if connection is not None:
        return _work(connection)
    with transaction() as own:
        return _work(own)


def new_ticket_created(ticket_id: str) -> int:
    """Оповещает мастеров о заявке, созданной в мессенджере.

    Рассылаем только тем, кому заявка видна: мастер своего цеха и те, кому
    видны заявки без цеха. Событие персональное, поэтому чужим оно не уйдёт.
    """
    with transaction() as connection:
        ticket = connection.execute("SELECT id FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not ticket:
            return 0

        masters = connection.execute(
            "SELECT master_uid FROM api_masters WHERE is_active = 1"
        ).fetchall()

        count = 0
        for master in masters:
            if not is_visible(connection, ticket_id, master["master_uid"]):
                continue
            events.emit(
                connection,
                {master["master_uid"]},
                events.TICKET_CREATED,
                {"ticket_id": ticket_id},
                ticket_id,
            )
            count += 1
        return count


# ==================== Прочтение ====================


def mark_read(master_uid: str, ticket_id: str, up_to_seq: Optional[int] = None) -> int:
    """Отмечает прочитанным до seq. Возвращает количество отмеченных."""
    with transaction() as connection:
        require_member_or_visible_tx(connection, ticket_id, master_uid)

        if up_to_seq is None:
            row = connection.execute(
                "SELECT MAX(seq) AS last FROM messages WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            up_to_seq = row["last"] or 0

        connection.execute(
            """
            INSERT INTO ticket_read_state (ticket_id, master_uid, read_cursor, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ticket_id, master_uid) DO UPDATE SET
                read_cursor = MAX(read_cursor, excluded.read_cursor),
                updated_at = excluded.updated_at
            """,
            (ticket_id, master_uid, up_to_seq, _now()),
        )

        connection.execute(
            "UPDATE messages SET read_at = ? WHERE ticket_id = ? AND seq <= ? "
            "AND sender = 'client' AND read_at IS NULL",
            (_now(), ticket_id, up_to_seq),
        )

        changed = connection.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE ticket_id = ? AND sender = 'client' "
            "AND read_at IS NOT NULL AND seq <= ?",
            (ticket_id, up_to_seq),
        ).fetchone()["n"]

    return changed


def require_member_or_visible_tx(
    connection: sqlite3.Connection, ticket_id: str, master_uid: str
) -> None:
    exists = connection.execute("SELECT 1 FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not exists:
        raise errors.ticket_not_found(ticket_id)
    if is_member(connection, ticket_id, master_uid):
        return
    if not is_visible(connection, ticket_id, master_uid):
        raise errors.ticket_not_found(ticket_id)


# ==================== Действия с заявкой ====================


def accept_ticket(
    master: Master, ticket_id: str, *, connection: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """Принять заявку.

    Атомарность — главное требование: «кто первый принял, тот и делает».
    Реализовано условным UPDATE `WHERE status = 'new'`: если параллельный запрос
    уже сменил статус, наш UPDATE не затронет ни одной строки, и мы вернём 409.
    Без этого условия второй мастер мог бы перехватить заявку.

    Событие пишется здесь же, в той же транзакции: состояние «взята» без
    события означало бы, что остальные мастера её не увидят.
    """
    now = _now()
    with unit_of_work(connection) as connection:
        row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            raise errors.ticket_not_found(ticket_id)

        if row["status"] == STATUS_CLOSED:
            raise errors.ticket_closed(ticket_id)

        if row["status"] == STATUS_IN_PROGRESS:
            # Кто-то уже взял — сообщаем имя, чтобы клиент показал
            # «Заявку уже взял Фёдор Семёнов».
            owner = _owner_of(connection, ticket_id)
            raise errors.already_accepted(
                ticket_id,
                owner["id"] if owner else "",
                owner["full_name"] if owner else "другой мастер",
            )

        cursor = connection.execute(
            """
            UPDATE tickets
               SET status = 'in_progress',
                   owner_master_uid = ?,
                   updated_at = ?
             WHERE id = ? AND status = 'new'
            """,
            (master.master_uid, now, ticket_id),
        )

        if cursor.rowcount == 0:
            # Гонка: параллельный запрос успел раньше. Выясняем, кто теперь владелец.
            owner = _owner_of(connection, ticket_id)
            raise errors.already_accepted(
                ticket_id,
                owner["id"] if owner else "",
                owner["full_name"] if owner else "другой мастер",
            )

        connection.execute(
            """
            INSERT INTO ticket_members (ticket_id, master_uid, role, joined_at)
            VALUES (?, ?, 'owner', ?)
            ON CONFLICT(ticket_id, master_uid) DO UPDATE SET role = 'owner'
            """,
            (ticket_id, master.master_uid, now),
        )

        ticket = _load_ticket(connection, master.master_uid, ticket_id)

        events.emit_all(
            connection,
            events.TICKET_ACCEPTED,
            {
                "owner_master_id": master.master_uid,
                "owner_name": master.full_name,
                "ticket": ticket,
            },
            ticket_id,
        )

    return ticket


def release_ticket(
    master: Master,
    ticket_id: str,
    reason: Optional[str] = None,
    *,
    connection: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Вернуть заявку в общую ленту."""
    now = _now()
    with unit_of_work(connection) as connection:
        require_member(connection, ticket_id, master.master_uid)
        require_open(connection, ticket_id)

        row = connection.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row["status"] == STATUS_NEW:
            raise errors.ticket_not_in_feed(ticket_id)

        connection.execute(
            """
            UPDATE tickets
               SET status = 'new', owner_master_uid = NULL, updated_at = ?
             WHERE id = ?
            """,
            (now, ticket_id),
        )
        connection.execute(
            "DELETE FROM ticket_members WHERE ticket_id = ?", (ticket_id,)
        )
        ticket = _load_ticket(connection, master.master_uid, ticket_id)

        # Событие видят и бывшие участники (у них снимаются права), и все
        # активные мастера: заявка снова в общей ленте.
        events.emit_all(
            connection,
            events.TICKET_RELEASED,
            {
                "by_master_id": master.master_uid,
                "by_master_name": master.full_name,
                "reason": reason,
                "ticket": ticket,
            },
            ticket_id,
        )
    return ticket


def close_ticket(
    master: Master,
    ticket_id: str,
    reason: Optional[str] = None,
    *,
    connection: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Закрыть заявку. Закрыть может любой участник — по требованию заказчика."""
    now = _now()
    with unit_of_work(connection) as connection:
        require_member(connection, ticket_id, master.master_uid)
        require_open(connection, ticket_id)

        connection.execute(
            """
            UPDATE tickets
               SET status = 'closed', closed_at = ?, close_reason = ?, updated_at = ?
             WHERE id = ?
            """,
            (now, reason, now, ticket_id),
        )
        ticket = _load_ticket(connection, master.master_uid, ticket_id)

        events.emit_all(
            connection,
            events.TICKET_CLOSED,
            {
                "by_master_id": master.master_uid,
                "by_master_name": master.full_name,
                "reason": reason,
                "ticket": ticket,
            },
            ticket_id,
        )
    return ticket


def ticket_closed(ticket_id: str) -> None:
    """Оповещает о закрытии, сделанном вне API — из Telegram бота.

    Статус к этому моменту уже записан ботом, поэтому здесь только событие:
    иначе приложение мастера узнает о закрытии лишь при полной перезагрузке.
    """
    with transaction() as connection:
        row = connection.execute(
            "SELECT id FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not row:
            return
        events.emit_all(
            connection,
            events.TICKET_CLOSED,
            {"by_master_id": None, "by_client": True, "ticket_id": ticket_id},
            ticket_id,
        )


def reopen_ticket(
    master: Master, ticket_id: str, *, connection: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """Переоткрыть закрытую заявку."""
    now = _now()
    with unit_of_work(connection) as connection:
        require_member(connection, ticket_id, master.master_uid)

        row = connection.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            raise errors.ticket_not_found(ticket_id)
        if row["status"] != STATUS_CLOSED:
            raise errors.forbidden("Заявка и так не закрыта", ticket_id=ticket_id)

        connection.execute(
            """
            UPDATE tickets
               SET status = 'in_progress', closed_at = NULL, close_reason = NULL, updated_at = ?
             WHERE id = ?
            """,
            (now, ticket_id),
        )
        ticket = _load_ticket(connection, master.master_uid, ticket_id)

        events.emit_all(
            connection,
            events.TICKET_REOPENED,
            {
                "by_master_id": master.master_uid,
                "by_master_name": master.full_name,
                "ticket": ticket,
            },
            ticket_id,
        )
    return ticket


def decline_ticket(master: Master, ticket_id: str, reason: Optional[str] = None) -> bool:
    """Скрыть заявку из своей ленты. Идемпотентно.

    Заявка уходит только у этого мастера: у остальных и у Telegram-бота
    она остаётся. Отказ хранится в ticket_declines, а не меняет status,
    иначе исчезла бы из ленты сразу у всех.
    """
    with transaction() as connection:
        exists = connection.execute("SELECT 1 FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not exists:
            raise errors.ticket_not_found(ticket_id)

        row = connection.execute(
            """
            INSERT INTO ticket_declines (ticket_id, master_uid, reason, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ticket_id, master_uid) DO UPDATE SET reason = excluded.reason
            """,
            (ticket_id, master.master_uid, reason, _now()),
        )
        return row.rowcount >= 0


# ==================== Участники ====================


def add_member(
    master: Master,
    ticket_id: str,
    other_uid: str,
    *,
    connection: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Подключить второго мастера к заявке."""
    now = _now()
    with unit_of_work(connection) as connection:
        require_member(connection, ticket_id, master.master_uid)
        require_open(connection, ticket_id)

        other = connection.execute(
            "SELECT * FROM api_masters WHERE master_uid = ? AND is_active = 1", (other_uid,)
        ).fetchone()
        if not other:
            raise errors.master_not_found(other_uid)

        ticket = connection.execute(
            "SELECT workshop_id FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        ticket_workshop = ticket["workshop_id"] if "workshop_id" in ticket.keys() else None

        # Правило цехов: если заявка привязана к цеху, подключить можно только
        # мастера того же цеха. Не проверяем, если цех не задан.
        if ticket_workshop is not None and other["workshop_id"] != ticket_workshop:
            raise errors.workshop_mismatch(
                ticket_id,
                workshop_id_to_api(ticket_workshop) or "",
                workshop_id_to_api(other["workshop_id"]) or "",
            )

        exists = connection.execute(
            "SELECT 1 FROM ticket_members WHERE ticket_id = ? AND master_uid = ?",
            (ticket_id, other_uid),
        ).fetchone()
        if exists:
            raise errors.already_member(ticket_id, other_uid)

        connection.execute(
            """
            INSERT INTO ticket_members (ticket_id, master_uid, role, joined_at)
            VALUES (?, ?, 'collaborator', ?)
            """,
            (ticket_id, other_uid, now),
        )

        member = {
            "master_id": other_uid,
            "full_name": other["full_name"] or "",
            "role": "collaborator",
            "joined_at": now,
        }

        # Событие получают все: у текущих участников — новость, у нового —
        # сигнал догрузить заявку и её сообщения.
        events.emit_all(
            connection,
            events.MEMBER_ADDED,
            {"member": member},
            ticket_id,
        )
    return member


def remove_member(
    master: Master,
    ticket_id: str,
    other_uid: str,
    *,
    connection: Optional[sqlite3.Connection] = None,
) -> bool:
    """Отключить мастера от заявки.

    Владельца отключить нельзя: заявка осталась бы без ответственного.
    """
    with unit_of_work(connection) as connection:
        require_member(connection, ticket_id, master.master_uid)
        require_open(connection, ticket_id)

        role = connection.execute(
            "SELECT role FROM ticket_members WHERE ticket_id = ? AND master_uid = ?",
            (ticket_id, other_uid),
        ).fetchone()
        if not role:
            raise errors.master_not_found(other_uid)
        if role["role"] == "owner":
            raise errors.forbidden(
                "Нельзя отключить владельца заявки. Сначала верните её в общую ленту.",
                ticket_id=ticket_id,
                master_id=other_uid,
            )

        connection.execute(
            "DELETE FROM ticket_members WHERE ticket_id = ? AND master_uid = ?",
            (ticket_id, other_uid),
        )

        events.emit_all(
            connection,
            events.MEMBER_REMOVED,
            {"master_id": other_uid, "by_master_id": master.master_uid},
            ticket_id,
        )
        return True


def list_member_uids(connection: sqlite3.Connection, ticket_id: str) -> List[str]:
    """Все master_uid, которым нужно слать события по заявке."""
    rows = connection.execute(
        "SELECT master_uid FROM ticket_members WHERE ticket_id = ?", (ticket_id,)
    ).fetchall()
    return [row["master_uid"] for row in rows]


def members_with_owner(connection: sqlite3.Connection, ticket_id: str) -> List[str]:
    """Участники плюс текущий владелец (по любой из двух колонок)."""
    uids = set(list_member_uids(connection, ticket_id))
    row = connection.execute(
        f"SELECT {_owner_uid_sql()} AS owner_uid FROM tickets t WHERE t.id = ?",
        (ticket_id,),
    ).fetchone()
    if row and row["owner_uid"]:
        uids.add(row["owner_uid"])
    return list(uids)


def update_profile(
    master_uid: str,
    full_name: Optional[str],
    workshop_id: Optional[str],
    *,
    connection: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Смена ФИО или цеха мастером."""
    with unit_of_work(connection) as connection:
        row = connection.execute(
            "SELECT * FROM api_masters WHERE master_uid = ?", (master_uid,)
        ).fetchone()
        if not row:
            raise errors.master_not_found(master_uid)

        name = (full_name or "").strip() or row["full_name"]
        if workshop_id is not None:
            db_workshop = workshop_id_to_db(workshop_id)
        else:
            db_workshop = row["workshop_id"]
        if db_workshop is not None:
            exists = connection.execute(
                "SELECT 1 FROM workshops WHERE id = ?", (db_workshop,)
            ).fetchone()
            if not exists:
                raise errors.validation_error("Цех не найден", workshop_id=workshop_id)

        connection.execute(
            "UPDATE api_masters SET full_name = ?, workshop_id = ? WHERE master_uid = ?",
            (name, db_workshop, master_uid),
        )
        fresh = connection.execute(
            """
            SELECT m.*, w.name AS workshop_name FROM api_masters m
            LEFT JOIN workshops w ON w.id = m.workshop_id
            WHERE m.master_uid = ?
            """,
            (master_uid,),
        ).fetchone()

        # Справочник мастеров лежит у клиентов в кэше, поэтому обновление
        # профиля рассылаем всем активным: иначе в чатах остались бы старые имена.
        events.emit_all(
            connection,
            events.MASTERS_DIRECTORY_CHANGED,
            {"master_id": master_uid, "full_name": name},
            None,
        )

    return {
        "id": fresh["master_uid"],
        "full_name": fresh["full_name"] or "",
        "workshop_id": workshop_id_to_api(fresh["workshop_id"]),
        "workshop_name": fresh["workshop_name"],
        "is_active": bool(fresh["is_active"]),
        "online": is_online(fresh["last_seen_at"]),
    }
