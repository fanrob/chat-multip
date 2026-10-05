"""Доставка ответов мастеров клиентам на почту.

Симметрично `telegram_bot.outbox`: та же очередь `api_outbox`, но строки
`channel='email'` и доставка через SMTP. Тема и Message-ID письма берутся из
заявки, поэтому ответ уходит в ту же ветку переписки.
"""

import logging
from typing import Any, Dict

from mail_bot.sender import send_email_reply
from outbox_queue import OutboxItem

logger = logging.getLogger(__name__)


async def deliver(item: OutboxItem, ticket: Dict[str, Any]) -> None:
    """Отправляет ответ клиенту письмом.

    Привязку к operator_messages здесь не делаем: клиент пишет ответ на почту,
    а не реплаем в Telegram, поэтому входящее письмо опознаётся по теме.
    """
    await send_email_reply(
        ticket["client_id"],
        item.ticket_id,
        item.payload.get("text") or "",
        ticket.get("message_id"),
        ticket.get("subject"),
    )
