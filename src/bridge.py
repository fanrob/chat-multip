"""Мост между Telegram-ботом и таблицами API.

Бот по-прежнему пишет в свою операторскую память и рассылает сообщения через
Telegram, но вызовы проходят через этот модуль: так события из мессенджера
попадают в messages/events, и клиент видит ту же заявку, что и мастер.

Мост не должен ронять обработчик апдейта: любая ошибка синхронизации
логируется, а апдейт считается обработанным — иначе клиент не сможет закрыть
заявку из-за неработающего API.
"""

import logging
from typing import Any, Dict, Optional

from api import store

logger = logging.getLogger(__name__)


def source_message_id(chat_id: int, message_id: int) -> str:
    """Ключ идемпотентности для апдейта Telegram.

    message_id уникален только внутри чата, поэтому чат входит в ключ.
    """
    return f"tg:{chat_id}:{message_id}"


def record_ticket_closed(ticket_id: str) -> None:
    """Клиент закрыл заявку в Telegram — сообщаем об этом мастерам.

    Статус уже записан ботом в tickets, но без события приложение мастера
    узнает о закрытии только при полной перезагрузке ленты.
    """
    try:
        store.ticket_closed(ticket_id)
    except Exception:
        logger.exception("Не удалось разослать ticket.closed для %s", ticket_id)


def record_email_message(
    ticket_id: str,
    sender_name: str,
    text: str,
    *,
    message_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Клиент написал письмо в существующую заявку.

    Ключ идемпотентности — Message-ID письма: при повторной обработке почты
    (реконнект IMAP) одно и то же письмо не должно задваиваться в истории.
    """
    try:
        return store.append_incoming_message(
            ticket_id,
            text,
            sender_name,
            channel="email",
            client_msg_id=f"eml:{message_id}" if message_id else None,
        )
    except Exception:
        logger.exception("Не удалось записать письмо клиента в %s", ticket_id)
        return None


def record_ticket_created(ticket_id: str) -> None:
    """Клиент создал заявку в Telegram — сообщаем об этом мастерам."""
    try:
        count = store.new_ticket_created(ticket_id)
        logger.debug("Заявка %s отдана мастерам (%s получателей)", ticket_id, count)
    except Exception:
        logger.exception("Не удалось разослать ticket.created для %s", ticket_id)


def record_client_message(
    ticket_id: str,
    sender_name: str,
    text: str,
    *,
    chat_id: int,
    message_id: int,
) -> Optional[Dict[str, Any]]:
    """Клиент написал мастеру в заявку — дописываем это в историю.

    Вызывается только после успешной отправки сообщения мастеру: если
    Telegram не принял сообщение, в истории клиента его быть не должно.
    """
    try:
        message = store.append_incoming_message(
            ticket_id,
            text,
            sender_name,
            channel="telegram",
            client_msg_id=source_message_id(chat_id, message_id),
        )
        if message is None:
            logger.info("Заявка %s закрыта или удалена, сообщение клиента не записано", ticket_id)
        return message
    except Exception:
        logger.exception("Не удалось записать сообщение клиента в %s", ticket_id)
        return None
