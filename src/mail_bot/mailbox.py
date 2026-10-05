import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

from imap_tools import MailBox, AND  # type: ignore
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from config import (
    get_email, get_email_password, get_imap_server, get_imap_port,
    get_smtp_server, get_smtp_port, get_check_interval,
)
from models import Ticket, generate_ticket_id
from email_utils import escape_markdown, clean_email_text, get_message_id
from storage import ticket_storage, operator_message_storage, master_storage, get_operator_ids

logger = logging.getLogger(__name__)


async def notify_operators(bot, ticket: Ticket, operator_ids: Optional[list[int]] = None):
    """Отправляет уведомление о новой заявке мастерам (всем или указанным)."""
    keyboard = [[
        InlineKeyboardButton("✅ Принять заявку", callback_data=f"take_{ticket.id}"),
        InlineKeyboardButton("❌ Закрыть заявку", callback_data=f"close_op_{ticket.id}")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    workshop_line = ""
    if ticket.workshop_id is not None:
        workshop_name = master_storage.get_workshop_name(ticket.workshop_id)
        if workshop_name:
            workshop_line = f"🏭 Цех: {workshop_name}\n"

    message_text = (
        f"🔔 **Новая заявка!**\n"
        f"🆔 {ticket.id}\n"
        f"📬 Источник: {ticket.source}\n"
        f"👤 Клиент: {ticket.client_name}\n"
        f"{workshop_line}\n"
        f"```\n{escape_markdown(ticket.text)}\n```"
    )

    if operator_ids is None:
        operator_ids = get_operator_ids(ticket.workshop_id)

    for operator_id in operator_ids:
        try:
            sent_message = await bot.send_message(
                chat_id=operator_id,
                text=message_text,
                reply_markup=reply_markup,
                parse_mode='none'
            )
            operator_message_storage.add(sent_message.message_id, operator_id, ticket.id)
        except Exception as e:
            logger.error(f"Could not notify operator {operator_id}: {e}")


async def check_email(app: Application):
    """Фоновая задача для проверки новой почты."""
    logger.info("Checking for new emails...")
    try:
        with MailBox(get_imap_server(), get_imap_port()).login(get_email(), get_email_password()) as mailbox:
            for msg in mailbox.fetch(AND(seen=False)):
                logger.info(f"New email from {msg.from_}: {msg.subject}")

                email_body = msg.text or msg.html or ""
                clean_body = clean_email_text(email_body, 500)
                ticket_text = f"{clean_body}"

                existing_ticket = ticket_storage.get_open_email_ticket_by_subject(msg.subject or '')
                if existing_ticket:
                    existing_ticket.message_id = get_message_id(msg) or existing_ticket.message_id
                    ticket_storage.update(existing_ticket)
                    if existing_ticket.taken_by:
                        reply_text = (
                            f"📨 **Сообщение от клиента** (ID заявки: {existing_ticket.id}):\n\n"
                            f"{ticket_text}"
                        )
                        sent_message = await app.bot.send_message(
                            chat_id=existing_ticket.taken_by,
                            text=reply_text,
                            parse_mode='Markdown',
                        )
                        operator_message_storage.add(
                            sent_message.message_id,
                            existing_ticket.taken_by,
                            existing_ticket.id,
                            sender='client',
                            sender_id=existing_ticket.client_id,
                            text=ticket_text,
                        )
                    else:
                        await notify_operators(app.bot, existing_ticket)
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

                await notify_operators(app.bot, ticket)

                mailbox.flag(msg.uid, '\\Seen', True)

    except Exception as e:
        logger.error(f"Error checking email: {e}")


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


async def periodic_email_check(app: Application):
    """Запускает бесконечный цикл проверки почты."""
    while True:
        await check_email(app)
        await asyncio.sleep(get_check_interval())
