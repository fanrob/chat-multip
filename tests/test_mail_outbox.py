"""Тесты почтовой части: канал в очереди и доставка ответа мастера письмом.

SMTP не используется: подменяем mail_bot.sender.send_email_reply и проверяем,
что заявка с source='email' уходит в канал 'email' (и только его обслуживает
mail_bot), ответ отправляется с темой и Message-ID заявки, а лишних привязок
к Telegram-сообщениям не создаётся.
"""

import asyncio
import sqlite3

import pytest
from conftest import seed_ticket
from test_outbox import (
    _clear_operator_messages,
    delivery_of,
    outbox_status,
    queue_message,
)

from api.db import reading, transaction
from mail_bot import outbox as mail_outbox
from outbox_queue import (
    CHANNEL_EMAIL,
    CHANNEL_TELEGRAM,
    DELIVERY_FAILED,
    DELIVERY_SENT,
    MAX_ATTEMPTS,
    claim_next,
    pending_count,
    process_one,
    recover_stale,
)


@pytest.fixture()
def sent(monkeypatch) -> list:
    """Заглушка SMTP: собирает аргументы вместо отправки письма."""
    calls = []

    async def fake_send(to_address, ticket_id, reply_text, message_id=None, subject=""):
        calls.append(
            {
                "to": to_address,
                "ticket_id": ticket_id,
                "text": reply_text,
                "message_id": message_id,
                "subject": subject,
            }
        )

    monkeypatch.setattr(mail_outbox, "send_email_reply", fake_send)
    return calls


def make_email_ticket(client, headers, ticket_id="t_mail01") -> str:
    """Заявка, пришедшая письмом: адрес клиента и Message-ID переписки."""
    from datetime import datetime

    now = datetime.now().isoformat()
    with transaction() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO tickets
                (id, source, client_id, client_name, text, status, created_at,
                 message_id, subject)
            VALUES (?, 'email', 'ivan@mail.ru', 'Иван П.', 'Не могу пригнать машину',
                    'new', ?, '<msg-1@mail>', 'Тема письма')
            """,
            (ticket_id, now),
        )
    return ticket_id


# ==================== Канал в очереди ====================


def test_email_reply_is_queued_for_email_channel(client, headers):
    """API кладёт ответ в канал заявки: почта — в почту, Telegram — в Telegram."""
    email_ticket = make_email_ticket(client, headers)
    queue_message(headers, email_ticket)
    tg_ticket = seed_ticket(client, headers, "t_test02")
    queue_message(headers, tg_ticket)

    with reading() as connection:
        rows = connection.execute(
            "SELECT ticket_id, channel FROM api_outbox WHERE status = 'pending' ORDER BY id"
        ).fetchall()
    assert {row["ticket_id"]: row["channel"] for row in rows} == {
        email_ticket: CHANNEL_EMAIL,
        tg_ticket: CHANNEL_TELEGRAM,
    }


def test_mail_consumer_ignores_telegram_ticket(client, headers, sent):
    """Мастер ответил в мессенджере — почта его не трогает."""
    queue_message(headers, seed_ticket(client, headers))

    assert asyncio.run(process_one(CHANNEL_EMAIL, mail_outbox.deliver)) is False
    assert sent == [], "SMTP не должен использоваться для telegram-заявок"
    assert pending_count(CHANNEL_TELEGRAM) == 1


# ==================== Доставка ====================


def test_email_reply_is_sent_with_ticket_thread(client, headers, sent):
    """Ответ уходит на адрес клиента в ту же ветку переписки."""
    ticket_id = make_email_ticket(client, headers)
    message = queue_message(headers, ticket_id, "Подъедем в 14:00")

    assert asyncio.run(process_one(CHANNEL_EMAIL, mail_outbox.deliver)) is True

    assert len(sent) == 1
    assert sent[0]["to"] == "ivan@mail.ru"
    assert sent[0]["ticket_id"] == ticket_id
    assert sent[0]["text"] == "Подъедем в 14:00"
    assert sent[0]["subject"] == "Тема письма", "ответ должен идти в ту же тему"
    assert sent[0]["message_id"] == "<msg-1@mail>", "In-Reply-To берётся из заявки"
    assert delivery_of(message["id"]) == DELIVERY_SENT
    assert outbox_status(message["id"]) == "sent"


def test_email_reply_is_not_bound_to_telegram_message(client, headers, db_path, sent):
    """Письма не проходят через Telegram, лишних привязок создавать нельзя."""
    _clear_operator_messages(db_path)
    ticket_id = make_email_ticket(client, headers)
    queue_message(headers, ticket_id)

    asyncio.run(process_one(CHANNEL_EMAIL, mail_outbox.deliver))

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM operator_messages WHERE ticket_id = ?", (ticket_id,)
        ).fetchall()
    assert rows == [], "для email-заявок привязка к message_id не нужна"


def test_smtp_failure_returns_message_to_queue(client, headers, monkeypatch):
    """Письмо не ушло — сообщение должно остаться в очереди, а не пропасть."""

    async def broken_send(*args, **kwargs):
        raise RuntimeError("SMTP connection reset")

    monkeypatch.setattr(mail_outbox, "send_email_reply", broken_send)

    ticket_id = make_email_ticket(client, headers)
    message = queue_message(headers, ticket_id)

    assert asyncio.run(process_one(CHANNEL_EMAIL, mail_outbox.deliver)) is True
    assert pending_count(CHANNEL_EMAIL) == 1
    assert delivery_of(message["id"]) != DELIVERY_SENT


def test_message_gives_up_after_max_attempts(client, headers, monkeypatch):
    async def broken_send(*args, **kwargs):
        raise RuntimeError("SMTP unreachable")

    monkeypatch.setattr(mail_outbox, "send_email_reply", broken_send)

    ticket_id = make_email_ticket(client, headers)
    message = queue_message(headers, ticket_id)

    for _ in range(MAX_ATTEMPTS):
        asyncio.run(process_one(CHANNEL_EMAIL, mail_outbox.deliver))

    assert outbox_status(message["id"]) == "failed"
    assert delivery_of(message["id"]) == DELIVERY_FAILED


def test_mail_recover_stale_does_not_touch_telegram(client, headers):
    """Зависшая отправка в Telegram — не дело почтового процесса."""
    tg_ticket = seed_ticket(client, headers)
    tg_message = queue_message(headers, tg_ticket)
    email_ticket = make_email_ticket(client, headers)
    email_message = queue_message(headers, email_ticket)

    assert claim_next(CHANNEL_TELEGRAM) is not None
    assert claim_next(CHANNEL_EMAIL) is not None

    assert recover_stale(CHANNEL_EMAIL) == 1
    assert outbox_status(email_message["id"]) == "pending"
    assert outbox_status(tg_message["id"]) == "sending", "чужой канал не трогаем"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
