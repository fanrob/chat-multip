"""Лента событий и курсор синхронизации.

Как работает: любое изменение (новая заявка, сообщение, смена статуса) пишет
строку в таблицу `events` с автоинкрементным `seq`. Клиент хранит последний
обработанный seq и спрашивает `GET /v1/sync?cursor=N`.

Почему курсор, а не «есть ли новое»: опрос по признаку «есть новое» теряет
события, если два события пришли между запросами. Курсор по seq такого класса
ошибок не допускает — клиент либо обработал событие, либо увидит его в следующем
ответе.

Доставка at-least-once: событие может вернуться повторно (например, клиент
не успел записать курсор и запросил с того же места). Клиент обязан дедуплицировать
по seq — это описано в README клиента.
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from api.db import reading, transaction

logger = logging.getLogger(__name__)

#: Типы событий из README (раздел 6.2).
TICKET_CREATED = "ticket.created"
TICKET_ACCEPTED = "ticket.accepted"
TICKET_RELEASED = "ticket.released"
TICKET_CLOSED = "ticket.closed"
TICKET_REOPENED = "ticket.reopened"
TICKET_UPDATED = "ticket.updated"
MEMBER_ADDED = "ticket.member_added"
MEMBER_REMOVED = "ticket.member_removed"
MESSAGE_CREATED = "message.created"
MESSAGE_UPDATED = "message.updated"
MASTERS_DIRECTORY_CHANGED = "masters.directory_changed"

#: Максимум событий за один ответ — защита от огромного JSON при потоке.
MAX_LIMIT = 500

#: Сколько событий чистим за раз, если таблица разрослась.
PRUNE_BATCH = 1000

#: Удержание long-poll по умолчанию, сек. Ровно то, что ждёт клиент из README.
DEFAULT_WAIT_SECONDS = 25

#: Максимум удержания long-poll, сек. Клиент из README ждёт 25; больше
#: держать соединение незачем — при обрыве клиент просто переспросит.
MAX_WAIT_SECONDS = 60

#: Как часто проверять ленту, пока ждём событий. Меньше — отзывчивее и
#: дороже; 0.5 секунды для 5–7 клиентов — это меньше 15 запросов в секунду.
POLL_INTERVAL_SECONDS = 0.5


def _now() -> str:
    return datetime.now().isoformat()


def emit(
    connection: sqlite3.Connection,
    master_uids: Iterable[str],
    event_type: str,
    data: Optional[Dict[str, Any]] = None,
    ticket_id: Optional[str] = None,
) -> None:
    """Пишет событие каждому из мастеров.

    Вызывается внутри уже открытой транзакции: если действие откатится,
    события откатятся вместе с ним — клиент не увидит фантомных обновлений.

    Один и тот же тип пишется по строке на мастера: ленты у мастеров независимы,
    у одного события один seq. Так проще поддерживать курсор.
    """
    payload = json.dumps(data or {}, ensure_ascii=False, default=str)
    now = _now()

    # Дедупликация: один мастер может встретиться дважды (owner и в members).
    seen: set[str] = set()
    for master_uid in master_uids:
        if not master_uid or master_uid in seen:
            continue
        seen.add(master_uid)
        connection.execute(
            "INSERT INTO events (master_uid, type, ticket_id, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (master_uid, event_type, ticket_id, payload, now),
        )


def emit_all(
    connection: sqlite3.Connection,
    event_type: str,
    data: Optional[Dict[str, Any]] = None,
    ticket_id: Optional[str] = None,
) -> None:
    """То же, что emit, но всем активным мастерам.

    Нужно для событий общей ленты: новая заявка интересует всех, кто её видит.
    """
    rows = connection.execute(
        "SELECT master_uid FROM api_masters WHERE is_active = 1"
    ).fetchall()
    emit(connection, [row["master_uid"] for row in rows], event_type, data, ticket_id)


def emit_to_feed(
    connection: sqlite3.Connection,
    event_type: str,
    data: Optional[Dict[str, Any]] = None,
    ticket_id: Optional[str] = None,
) -> None:
    """Событие тем мастерам, у кого заявка не скрыта отказом.

    Отказавшийся мастер не должен получать события по заявке, которую он
    отклонил, — иначе в его ленте она всплывёт снова.
    """
    rows = connection.execute(
        """
        SELECT m.master_uid FROM api_masters m
        WHERE m.is_active = 1
          AND NOT EXISTS (
              SELECT 1 FROM ticket_declines d
              WHERE d.ticket_id = ? AND d.master_uid = m.master_uid
          )
        """,
        (ticket_id,),
    ).fetchall()
    emit(connection, [row["master_uid"] for row in rows], event_type, data, ticket_id)


def fetch(
    master_uid: str,
    cursor: int = 0,
    limit: int = 200,
    ticket_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Отдаёт события после курсора.

    Порядок строго по seq — клиент применяет события в том же порядке,
    в котором они произошли. Без ORDER BY SQLite вернул бы строки в
    произвольном порядке, и клиент мог бы применить «закрыта» раньше
    «принята».
    """
    limit = max(1, min(limit, MAX_LIMIT))

    clauses = ["master_uid = ?", "seq > ?"]
    params: List[Any] = [master_uid, cursor]

    if ticket_id:
        clauses.append("ticket_id = ?")
        params.append(ticket_id)

    with reading() as connection:
        rows = connection.execute(
            f"""
            SELECT seq, type, ticket_id, payload, created_at
            FROM events
            WHERE {' AND '.join(clauses)}
            ORDER BY seq
            LIMIT ?
            """,
            [*params, limit + 1],
        ).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]

    events = []
    for row in rows:
        try:
            data = json.loads(row["payload"])
        except (TypeError, ValueError):
            # Битый JSON не должен ронять весь ответ: отдаём пустой payload,
            # клиент перезапросит данные через REST.
            logger.warning("Не удалось разобрать payload события %s", row["seq"])
            data = {}
        events.append(
            {
                "seq": row["seq"],
                "type": row["type"],
                "ts": row["created_at"],
                "ticket_id": row["ticket_id"],
                "data": data,
            }
        )

    new_cursor = events[-1]["seq"] if events else cursor

    return {"events": events, "cursor": new_cursor, "has_more": has_more}


def current_cursor(master_uid: str) -> int:
    """Максимальный seq этого мастера — стартовая точка для новой сессии."""
    with reading() as connection:
        row = connection.execute(
            "SELECT MAX(seq) AS last FROM events WHERE master_uid = ?", (master_uid,)
        ).fetchone()
    return row["last"] or 0


def min_available_cursor() -> int:
    """Самый ранний курсор, с которого лента ещё цела.

    Считаем по всем мастерам, а не по конкретному: после чистки старых
    событий потерять их мог любой, и клиент должен узнать об этом сам.
    Возвращает 0, если ничего не вытеснено.
    """
    with reading() as connection:
        row = connection.execute("SELECT MIN(seq) AS first FROM events").fetchone()
    first = row["first"]
    return first - 1 if first else 0


def fetch_with_wait(
    master_uid: str,
    cursor: int = 0,
    limit: int = 200,
    ticket_id: Optional[str] = None,
    wait: int = 0,
) -> Dict[str, Any]:
    """Отдаёт события, а если их нет — ждёт до wait секунд.

    Long-poll из README: клиенту не нужно самому угадывать период опроса,
    а сервер не шлёт пустые ответы каждые пару секунд.

    Вызывается из обычного (не async) хендлера, поэтому time.sleep занимает
    поток пула, а не блокирует event loop: так же работают и 5–7 висящих
    клиентов.
    """
    deadline = time.monotonic() + max(0, wait)
    result = fetch(master_uid, cursor, limit, ticket_id)

    while not result["events"] and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        result = fetch(master_uid, cursor, limit, ticket_id)

    return result


def validate_cursor(master_uid: str, cursor: int) -> int:
    """Проверяет курсор перед выборкой.

    cursor > max — клиент присылает курсор из будущего (восстановление из бэкапа
    или баг). Отдавать данные с такого места нельзя, иначе события потеряются
    навсегда. Возвращаем 0, чтобы клиент получил всю ленту заново.
    """
    if cursor < 0:
        return 0
    latest = current_cursor(master_uid)
    if cursor > latest:
        logger.warning("Курсор %s больше последнего seq %s — нужен ресинк", cursor, latest)
        return 0
    return cursor


def prune(retention_days: int = 30, batch: int = PRUNE_BATCH) -> int:
    """Удаляет старые события. Возвращает количество удалённых строк.

    События нужны только для догоняющих клиентов; записи месячной давности
    можно уже не хранить, иначе таблица растёт без ограничений. Удаляем
    по частям, чтобы не держать долгую блокировку записи.
    """
    threshold = (datetime.now() - timedelta(days=retention_days)).isoformat()
    total = 0
    while True:
        with transaction() as connection:
            rows = connection.execute(
                "SELECT seq FROM events WHERE created_at < ? ORDER BY seq LIMIT ?",
                (threshold, batch),
            ).fetchall()
            if not rows:
                break
            last_seq = rows[-1]["seq"]
            cursor = connection.execute(
                "DELETE FROM events WHERE seq <= ?", (last_seq,)
            )
            deleted = cursor.rowcount
        total += deleted
        if deleted < batch:
            break
    if total:
        logger.info("Удалено старых событий: %d", total)
    return total


def stats() -> Dict[str, int]:
    """Счётчики для /health: сколько событий и мастеров в системе."""
    with reading() as connection:
        events = connection.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        masters = connection.execute(
            "SELECT COUNT(*) AS n FROM api_masters WHERE is_active = 1"
        ).fetchone()["n"]
        pending = connection.execute(
            "SELECT COUNT(*) AS n FROM api_outbox WHERE status = 'pending'"
        ).fetchone()["n"]
    return {"events": events, "active_masters": masters, "outbox_pending": pending}
