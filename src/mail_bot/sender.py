"""Отправка писем клиентам через SMTP."""

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

from config import get_email, get_email_password, get_smtp_port, get_smtp_server

logger = logging.getLogger(__name__)


def _send_email_reply_sync(to_address: str, ticket_id: str, reply_text: str,
                           message_id: Optional[str] = None,
                           subject: str = ''):
    """Синхронная отправка письма через SMTP (выполняется в отдельном потоке)."""
    msg = EmailMessage()
    msg["From"] = get_email()
    msg["To"] = to_address
    msg["Subject"] = subject or f"Re: [заявка #{ticket_id}]"
    msg.set_content(reply_text, charset="utf-8")

    if message_id:
        msg["In-Reply-To"] = message_id
        msg["References"] = message_id

    if get_smtp_port() == 465:
        with smtplib.SMTP_SSL(get_smtp_server(), get_smtp_port()) as server:
            server.login(get_email(), get_email_password())
            server.send_message(msg)
    else:
        with smtplib.SMTP(get_smtp_server(), get_smtp_port()) as server:
            server.starttls()
            server.login(get_email(), get_email_password())
            server.send_message(msg)


async def send_email_reply(to_address: str, ticket_id: str, reply_text: str,
                           message_id: Optional[str] = None,
                           subject: str = ''):
    """Отправляет ответ клиенту на email через SMTP."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, _send_email_reply_sync, to_address, ticket_id, reply_text, message_id, subject
    )
