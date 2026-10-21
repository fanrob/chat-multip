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
from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from api import attachments as attachments_service
from outbox_queue import OutboxItem, is_admin_message
from storage import get_admin_id, operator_message_storage

logger = logging.getLogger(__name__)

#: Telegram отдаёт RetryAfter; ждать нужно именно столько, а не «потом».
_RETRY_AFTER = "retry_after"


def _format_text(payload: Dict[str, Any]) -> str:
    """Текст для клиента по формату README: «Ответ от {ФИО}, {время}: {текст}».

    Системные уведомления (принятие, закрытие, переоткрытие) отправляем как
    есть: их текст уже готов для клиента, префикс «Ответ от...» тут неуместен.
    """
    text = payload.get("text") or ""
    if payload.get("system"):
        return text
    author = payload.get("author") or "Мастер"
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


async def _deliver_admin(bot, item: OutboxItem) -> None:
    """Отправляет уведомление администратору (мастер, просящий вход).

    Адресат — admin_id из settings, а не клиент заявки: такие строки в
    очереди к заявкам не привязаны. Кнопки (Принять/Отказать) приходят
    в payload — бот их только переносит в клавиатуру, а обрабатывает
    telegram_bot.bot.admin_callback.
    """
    payload = item.payload
    keyboard = None
    buttons = payload.get("buttons") or []
    if buttons:
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])]
                for b in buttons
            ]
        )
    await bot.send_message(
        chat_id=get_admin_id(),
        text=payload.get("text") or "🔔 Новое уведомление для администратора.",
        reply_markup=keyboard,
    )


async def deliver(
    bot, item: OutboxItem, ticket: Optional[Dict[str, Any]]
) -> None:
    """Отправляет сообщение клиенту в Telegram (или админу, если это уведомление)."""
    if is_admin_message(item):
        await _deliver_admin(bot, item)
        return
    client_id = int(ticket["client_id"])
    message_id = await _send_telegram(
        bot,
        client_id,
        _format_text(item.payload),
        list(item.payload.get("attachments") or []),
    )
    operator_message_storage.add(message_id, item.ticket_id)
