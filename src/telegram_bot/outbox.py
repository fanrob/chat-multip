"""Доставка сообщений из очереди API в Telegram.

Очередь сама в `outbox_queue`, там же цикл: этот модуль отвечает только за
отправку. Строка приходит с `channel='telegram'`, то есть заявка клиента
пришла из мессенджера.

Для Telegram дополнительно запоминаем message_id отправленного сообщения:
клиент отвечает реплаем на него, и без этой привязки бот не поймёт, в какую
заявку попадёт ответ, и создаст новую.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

from api import attachments as attachments_service
from outbox_queue import OutboxItem
from storage import operator_message_storage

logger = logging.getLogger(__name__)

#: Telegram отдаёт RetryAfter; ждать нужно именно столько, а не «потом».
_RETRY_AFTER = "retry_after"


def _format_text(payload: Dict[str, Any]) -> str:
    """Текст для клиента по формату README: «Ответ от {ФИО}, {время}: {текст}»."""
    author = payload.get("author") or "Мастер"
    text = payload.get("text") or ""
    stamp = datetime.now().strftime("%H:%M")
    return f"Ответ от {author}, {stamp}:\n\n{text}"


async def _send_telegram(bot, chat_id: int, text: str, attachments: List[str]) -> int:
    """Отправляет текст и вложения. Текст всегда первым: он несёт смысл,
    а файл мастер может и не открыть.

    Возвращает message_id отправленного текста: клиент может ответить на него
    реплаем, и бот обязан понять, в какую заявку попадёт этот ответ.
    """
    sent = await bot.send_message(chat_id=chat_id, text=text)
    for attachment_id in attachments:
        resolved = attachments_service.resolve_for_delivery(attachment_id)
        await _deliver_file(bot, chat_id, resolved, sent.message_id)
    return sent.message_id


async def _deliver_file(bot, chat_id: int, resolved: Dict[str, Any], reply_to: int) -> None:
    mime = resolved["mime_type"]
    path = resolved["path"]
    if mime.startswith("image/"):
        with open(path, "rb") as handle:
            await bot.send_photo(chat_id=chat_id, photo=handle, reply_to_message_id=reply_to)
        return
    with open(path, "rb") as handle:
        await bot.send_document(
            chat_id=chat_id,
            document=(resolved["filename"], handle),
            reply_to_message_id=reply_to,
        )


async def deliver(bot, item: OutboxItem, ticket: Dict[str, Any]) -> None:
    """Отправляет сообщение клиенту в Telegram."""
    client_id = int(ticket["client_id"])
    message_id = await _send_telegram(
        bot,
        client_id,
        _format_text(item.payload),
        list(item.payload.get("attachments") or []),
    )
    operator_message_storage.add(
        message_id,
        client_id,
        item.ticket_id,
        sender="op",
        text=item.payload.get("text") or "",
    )
