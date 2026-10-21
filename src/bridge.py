"""Мост между Telegram-ботом и таблицами API.

Бот по-прежнему пишет в свою операторскую память и рассылает сообщения через
Telegram, но вызовы проходят через этот модуль: так события из мессенджера
попадают в messages/events, и клиент видит ту же заявку, что и мастер.

Мост не должен ронять обработчик апдейта: любая ошибка синхронизации
логируется, а апдейт считается обработанным — иначе клиент не сможет закрыть
заявку из-за неработающего API.
"""

import logging
from typing import Any, Dict, Optional, Sequence

from api import attachments as attachments_service
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


def _discard_attachments(attachment_ids: Sequence[str]) -> None:
    """Убирает файлы, которые не удалось привязать к сообщению."""
    for attachment_id in attachment_ids:
        try:
            attachments_service.delete(attachment_id)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось удалить неиспользованное вложение %s", attachment_id)


def record_client_message(
    ticket_id: str,
    sender_name: str,
    text: str,
    *,
    chat_id: int,
    message_id: int,
    attachments: Sequence[Dict[str, Any]] = (),
) -> Optional[Dict[str, Any]]:
    """Клиент написал мастеру в заявку — дописываем это в историю.

    Вызывается только после успешной отправки сообщения мастеру: если
    Telegram не принял сообщение, в истории клиента его быть не должно.

    `attachments` — уже скачанные ботом файлы вида
    `{"data": bytes, "filename": str, "mime": str}`. Файлы сохраняются на диск
    и привязываются к сообщению; если запись сообщения не удалась (заявка
    закрыта/удалена или сбой БД), только что сохранённые файлы удаляются,
    чтобы не копить мусор.
    """
    client_msg_id = source_message_id(chat_id, message_id)

    # Повторная доставка апдейта Telegram: сообщение уже записано — не
    # сохраняем один и тот же файл второй раз.
    if attachments:
        existing = store.get_source_message(client_msg_id)
        if existing is not None:
            return existing

    saved: list[str] = []
    try:
        for attachment in attachments:
            meta = attachments_service.save_client_file(
                attachment["data"],
                attachment.get("filename") or "file",
                attachment.get("mime") or "application/octet-stream",
                channel="telegram",
            )
            saved.append(meta["attachment_id"])

        message = store.append_incoming_message(
            ticket_id,
            text,
            sender_name,
            channel="telegram",
            client_msg_id=client_msg_id,
            attachment_ids=saved,
        )
        if message is None:
            logger.info("Заявка %s закрыта или удалена, сообщение клиента не записано", ticket_id)
            _discard_attachments(saved)
        return message
    except Exception:
        logger.exception("Не удалось записать сообщение клиента в %s", ticket_id)
        _discard_attachments(saved)
        return None
