"""Общая очередь доставки `api_outbox` для обоих ботов.

Зачем так: API не знает про мессенджеры и почту и не ждёт сеть. Мастер отправляет
сообщение, API записывает его в `messages` и кладёт строку в `api_outbox` со
статусом `pending`, после чего отвечает 202. Этот модуль — вторая половина: фоновой
цикл в процессе бота забирает строки, отправляет их клиенту и пишет результат в
`messages.delivery`.

Почему в ботах, а не в API: у Telegram-бота уже есть Application с работающим
event loop и доступ к Bot API, а у почтового — свой процесс с IMAP/SMTP.
Поднимать эти соединения из API означало бы двух конкурирующих потребителей
одного токена или одного ящика.

Строку забирает только владелец её канала: `telegram_bot` — `channel='telegram'`,
`mail_bot` — `channel='email'`. Поэтому и восстановление зависших `sending`, и
выбор следующей строки всегда с фильтром по каналу: один процесс не должен
перехватывать чужую доставку.

Порядок доставки — FIFO внутри заявки: сообщения мастера должны прийти к клиенту
в том порядке, в каком их отправили, поэтому за раз берётся только самое старое
необработанное сообщение и цикл ждёт не меньше секунды на ту же заявку.
"""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional

from api import events
from api.db import reading, transaction
from api.store import members_with_owner

logger = logging.getLogger(__name__)

#: Каналы доставки: строки с другим channel этому процессу не принадлежат.
CHANNEL_TELEGRAM = "telegram"
CHANNEL_EMAIL = "email"

#: Как долго ждать между опросами очереди, секунды.
POLL_INTERVAL_SECONDS = 1.0

#: Минимум между двумя сообщениями в одну заявку — ограничение канала доставки.
MIN_GAP_PER_TICKET_SECONDS = 1.0

#: После скольких неудач перестаём пытаться и отмечаем сообщение как failed.
MAX_ATTEMPTS = 5

#: Статусы доставки, которые понимает клиент (README клиента, 6.4).
DELIVERY_QUEUED = "queued"
DELIVERY_SENDING = "sending"
DELIVERY_SENT = "sent"
DELIVERY_FAILED = "failed"

#: Что умеет доставить канал: получает строку очереди и данные заявки.
Deliverer = Callable[["OutboxItem", Dict[str, Any]], Awaitable[None]]


class OutboxItem:
    """Строка очереди, разобранная в поля."""

    __slots__ = ("id", "ticket_id", "message_id", "channel", "payload", "attempts")

    def __init__(self, row: sqlite3.Row) -> None:
        self.id = row["id"]
        self.ticket_id = row["ticket_id"]
        self.message_id = row["message_id"]
        self.channel = row["channel"]
        self.payload: Dict[str, Any] = json.loads(row["payload"])
        self.attempts = row["attempts"]


def recover_stale(channel: str) -> int:
    """Возвращает в очередь сообщения этого канала, застрявшие в статусе sending.

    Нужно при старте бота: если процесс упал посередине отправки, строка
    осталась в sending навсегда и клиент не получил бы сообщение никогда.
    Чужие каналы не трогаем — ими занят другой процесс.
    """
    with transaction() as connection:
        cursor = connection.execute(
            """
            UPDATE api_outbox SET status = 'pending'
            WHERE status = 'sending' AND channel = ?
            """,
            (channel,),
        )
    if cursor.rowcount:
        logger.info("Возвращено в очередь зависших сообщений (%s): %d", channel, cursor.rowcount)
    return cursor.rowcount


#: Забираем только то, что не исчерпало попытки, и строго по порядку id:
#: порядок доставки в заявке должен совпадать с порядком отправки.
_PENDING_SQL = (
    "SELECT * FROM api_outbox WHERE status = 'pending' AND channel = ? AND attempts < ? "
    "ORDER BY id LIMIT 1"
)

_CLAIM_SQL = (
    "UPDATE api_outbox SET status = 'sending', attempts = attempts + 1 "
    "WHERE id = ? AND status = 'pending'"
)


def _pending_row(connection: sqlite3.Connection, channel: str) -> Optional[sqlite3.Row]:
    return connection.execute(_PENDING_SQL, (channel, MAX_ATTEMPTS)).fetchone()


def claim_next(channel: str) -> Optional[OutboxItem]:
    """Забирает самое старое сообщение канала и помечает sending.

    UPDATE с проверкой status='pending' в WHERE — защита от повторного захвата,
    если консьюмеров окажется больше одного.
    """
    with transaction() as connection:
        row = _pending_row(connection, channel)
        if not row:
            return None

        cursor = connection.execute(_CLAIM_SQL, (row["id"],))
        if cursor.rowcount != 1:
            return None

        fresh = connection.execute(
            "SELECT * FROM api_outbox WHERE id = ?", (row["id"],)
        ).fetchone()
        item = OutboxItem(fresh)
        connection.execute(
            "UPDATE messages SET delivery = ? WHERE id = ?",
            (DELIVERY_SENDING, item.message_id),
        )
        _notify(connection, item.ticket_id, item.message_id, DELIVERY_SENDING, None)

    return item


def peek_next(channel: str) -> Optional[OutboxItem]:
    """Смотрит, что следующее в очереди канала, не забирая.

    Нужно для паузы между сообщениями в одну заявку: забирать и сразу
    откатывать было бы лишней записью в БД на каждое сообщение.
    """
    with reading() as connection:
        row = _pending_row(connection, channel)
    return OutboxItem(row) if row else None


def _notify(
    connection: sqlite3.Connection,
    ticket_id: str,
    message_id: str,
    delivery: str,
    read_at: Optional[str],
) -> None:
    """Сообщает участникам заявки о смене статуса доставки."""
    recipients = set(members_with_owner(connection, ticket_id))
    events.emit(
        connection,
        recipients,
        events.MESSAGE_UPDATED,
        {"message_id": message_id, "delivery": delivery, "read_at": read_at},
        ticket_id,
    )


def mark_sent(item: OutboxItem) -> None:
    with transaction() as connection:
        connection.execute(
            "UPDATE api_outbox SET status = 'sent', sent_at = ?, last_error = NULL WHERE id = ?",
            (datetime.now().isoformat(), item.id),
        )
        connection.execute(
            "UPDATE messages SET delivery = ? WHERE id = ?", (DELIVERY_SENT, item.message_id)
        )
        _notify(connection, item.ticket_id, item.message_id, DELIVERY_SENT, None)
    logger.info(
        "Сообщение %s доставлено в заявку %s", item.message_id, item.ticket_id
    )


def mark_failed(item: OutboxItem, error: str, *, permanent: bool = False) -> None:
    """Отмечает неудачу.

    permanent=True — ошибка не исчезнет сама (например, клиент заблокировал
    бота): сообщение уходит в failed, чтобы не крутить очередь вечно.
    Иначе возвращается в pending, пока attempts не превысит MAX_ATTEMPTS.
    """
    if permanent or item.attempts >= MAX_ATTEMPTS:
        status = "failed"
        delivery = DELIVERY_FAILED
    else:
        status = "pending"
        delivery = DELIVERY_QUEUED

    with transaction() as connection:
        connection.execute(
            "UPDATE api_outbox SET status = ?, last_error = ? WHERE id = ?",
            (status, error[:500], item.id),
        )
        connection.execute(
            "UPDATE messages SET delivery = ? WHERE id = ?", (delivery, item.message_id)
        )
        _notify(connection, item.ticket_id, item.message_id, delivery, None)

    if status == "failed":
        logger.error(
            "Сообщение %s не доставлено и больше не будет повторяться: %s",
            item.message_id,
            error,
        )
    else:
        logger.warning(
            "Сообщение %s не доставлено (попытка %d/%d): %s",
            item.message_id,
            item.attempts,
            MAX_ATTEMPTS,
            error,
        )


def mark_permanently_failed(item: OutboxItem, error: str) -> None:
    mark_failed(item, error, permanent=True)


def load_ticket(ticket_id: str) -> Optional[Dict[str, Any]]:
    """Данные заявки, нужные для отправки: канал, адрес клиента, тема."""
    with reading() as connection:
        row = connection.execute(
            "SELECT id, source, client_id, client_name, subject, message_id, status "
            "FROM tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
    return dict(row) if row else None


def pending_count(channel: str) -> int:
    with reading() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM api_outbox WHERE status = 'pending' AND channel = ?",
            (channel,),
        ).fetchone()
    return row["n"]


async def process_one(channel: str, deliver: Deliverer) -> bool:
    """Отправляет одно сообщение канала. True — было что отправлять."""
    item = claim_next(channel)
    if item is None:
        return False

    ticket = load_ticket(item.ticket_id)
    if ticket is None:
        mark_permanently_failed(item, "Заявка не найдена")
        return True

    try:
        await deliver(item, ticket)
    except asyncio.CancelledError:
        # Завершаемся: возвращаем сообщение в очередь, чтобы его отправил
        # следующий запуск бота.
        mark_failed(item, "Процесс остановлен во время отправки")
        raise
    except Exception as exc:
        mark_failed(item, f"{type(exc).__name__}: {exc}")
        return True

    mark_sent(item)
    return True


async def outbox_loop(channel: str, deliver: Deliverer) -> None:
    """Фоновая задача бота: разбирает очередь своего канала, пока бот живёт."""
    logger.info("Outbox-консьюмер (%s) запущен", channel)
    last_sent: Dict[str, float] = {}
    loop = asyncio.get_event_loop()

    while True:
        try:
            item = peek_next(channel)
            if item is not None:
                previous = last_sent.get(item.ticket_id)
                if previous is not None:
                    wait = MIN_GAP_PER_TICKET_SECONDS - (loop.time() - previous)
                    if wait > 0:
                        await asyncio.sleep(wait)

            if not await process_one(channel, deliver):
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            if item is not None:
                last_sent[item.ticket_id] = loop.time()
        except asyncio.CancelledError:
            logger.info("Outbox-консьюмер (%s) остановлен", channel)
            raise
        except Exception:
            # Цикл обязан выжить: падение здесь молча остановило бы доставку
            # до перезапуска бота.
            logger.exception("Сбой в outbox-консьюмере (%s)", channel)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
