"""Чтение почты: новое письмо клиента — это заявка или продолжение переписки.

Письмо сразу попадает в общую ленту через `bridge`, мастерам в Telegram ничего
не отправляем: заявку они видят в приложении, а ответ уйдёт через
`mail_bot.outbox` уже по её каналу.
"""

import asyncio
import logging
import sqlite3
from typing import Optional

from imap_tools import AND, MailBox  # type: ignore

import bridge
from config import (
    get_check_interval,
    get_email,
    get_email_password,
    get_imap_port,
    get_imap_server,
)
from mail_bot.email_utils import (
    clean_email_text,
    get_message_id,
    normalize_email_subject,
)
from models import Ticket, generate_ticket_id
from storage import ticket_storage

logger = logging.getLogger(__name__)


def find_open_email_ticket(subject: str) -> Optional[Ticket]:
    """Ищет открытую заявку по теме письма.

    Нужна, чтобы ответ в переписке не превратился в новую заявку. Тема
    сравнивается нормализованной, поэтому «Re:», «Fwd:» и регистр не мешают.
    """
    normalized_subject = normalize_email_subject(subject)
    if not normalized_subject:
        return None

    with sqlite3.connect(ticket_storage.db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM tickets WHERE source = 'email' "
            "AND status IN ('new', 'taken') ORDER BY created_at DESC"
        ).fetchall()
    for row in rows:
        if normalize_email_subject(row["subject"]) == normalized_subject:
            return ticket_storage.from_row(row)
    return None


async def check_email() -> None:
    """Проверяет ящик и раскладывает новые письма по заявкам."""
    logger.info("Checking for new emails...")
    try:
        with MailBox(get_imap_server(), get_imap_port()).login(get_email(), get_email_password()) as mailbox:
            for msg in mailbox.fetch(AND(seen=False)):
                logger.info(f"New email from {msg.from_}: {msg.subject}")

                email_body = msg.text or msg.html or ""
                clean_body = clean_email_text(email_body, 500)
                ticket_text = f"{clean_body}"

                existing_ticket = find_open_email_ticket(msg.subject or '')
                if existing_ticket:
                    # Письмо в существующую переписку: обновляем Message-ID заявки,
                    # чтобы ответ мастера уходил в эту же ветку.
                    reply_to = get_message_id(msg)
                    existing_ticket.message_id = reply_to or existing_ticket.message_id
                    ticket_storage.update(existing_ticket)
                    bridge.record_email_message(
                        existing_ticket.id,
                        existing_ticket.client_name or msg.from_,
                        ticket_text,
                        message_id=reply_to or "",
                    )
                    mailbox.flag(msg.uid, '\\Seen', True)
                    continue

                ticket = Ticket(
                    id=generate_ticket_id(ticket_storage.get_all_ids()),
                    source='email',
                    client_id=msg.from_,
                    client_name=msg.from_,
                    text=ticket_text,
                    message_id=get_message_id(msg),
                    subject=msg.subject or '',
                )
                ticket_storage.add(ticket)
                bridge.record_ticket_created(ticket.id)

                mailbox.flag(msg.uid, '\\Seen', True)

    except Exception as e:
        logger.error(f"Error checking email: {e}")


async def periodic_check() -> None:
    """Бесконечный цикл проверки почты."""
    while True:
        await check_email()
        await asyncio.sleep(get_check_interval())
