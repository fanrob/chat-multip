"""Тесты outbox-консьюмера Telegram: доставка ответа мастера клиенту.

Telegram не используется: подменяем бота объектом FakeBot и проверяем, что
в очередь попал текст в формату README, вложения ушли файлами, а статусы
доставки доехали до клиента через событие message.updated.

Очередь и её статусы живут в общем `outbox_queue`, здесь проверяется только
телеграммовская доставка. Почта — в test_mail_outbox.py.
"""

import asyncio
import sqlite3
import types
import uuid
from functools import partial

import pytest
from conftest import seed_ticket

from api.db import reading, transaction
from outbox_queue import (
    CHANNEL_EMAIL,
    CHANNEL_TELEGRAM,
    DELIVERY_FAILED,
    DELIVERY_QUEUED,
    DELIVERY_SENDING,
    DELIVERY_SENT,
    MAX_ATTEMPTS,
    claim_next,
    mark_failed,
    mark_sent,
    peek_next,
    pending_count,
    process_one,
    recover_stale,
)
from telegram_bot.outbox import deliver as deliver_telegram


def telegram_out(bot):
    """Консьюмер Telegram: deliver с подставленным ботом."""
    return partial(deliver_telegram, bot)


class FakeBot:
    """Заглушка Telegram Bot API: пишет вызовы в список."""

    def __init__(self) -> None:
        self.calls = []

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append(("message", chat_id, text))
        return types.SimpleNamespace(message_id=4242)

    async def send_photo(self, chat_id, photo, **kwargs):
        self.calls.append(("photo", chat_id, photo.read()))
        return types.SimpleNamespace(message_id=4243)

    async def send_document(self, chat_id, document, **kwargs):
        name, handle = document
        self.calls.append(("document", chat_id, name, handle.read()))
        return types.SimpleNamespace(message_id=4244)


def master_of(headers):
    """Объект мастера по тем же заголовкам, что использует API."""
    from api import auth

    return auth.get_or_create(headers[auth.HEADER_NAME], "Фёдор Семёнов", None)


def _clear_operator_messages(db_path: str) -> None:
    """Чистит таблицу привязок: тесты работают на копии боевой БД, где есть
    реальные строки, и их нельзя путать со строками теста."""
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM operator_messages")


def queue_message(headers, ticket_id: str, text: str = "Ваша заявка принята", attachments=()):
    """Кладёт сообщение в очередь так же, как это делает API.

    Заявка принимается, потому что отправлять сообщения может только участник:
    без accept мастера store вернёт 403. Повторный accept не считается ошибкой —
    заявка уже в работе, это нормально для второго сообщения.
    """
    from api import errors, store

    master = master_of(headers)
    try:
        store.accept_ticket(master, ticket_id)
    except errors.ApiError as exc:
        if exc.code != "already_accepted":
            raise
    return store.create_message(
        master=master,
        ticket_id=ticket_id,
        client_msg_id=f"c-{text}-{uuid.uuid4()}",
        text=text,
        attachment_ids=list(attachments),
    )


def drop_ticket(ticket_id: str) -> None:
    """Удаляет заявку, оставляя сообщение в очереди."""
    with transaction() as connection:
        connection.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))


def delivery_of(message_id: str) -> str:
    with reading() as connection:
        row = connection.execute(
            "SELECT delivery FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
    return row["delivery"]


def outbox_status(message_id: str) -> str:
    with reading() as connection:
        row = connection.execute(
            "SELECT status FROM api_outbox WHERE message_id = ?", (message_id,)
        ).fetchone()
    return row["status"]


# ==================== Захват и статусы ====================


def test_claim_marks_message_sending(client, headers):
    ticket_id = seed_ticket(client, headers)
    message = queue_message(headers, ticket_id)

    item = claim_next(CHANNEL_TELEGRAM)

    assert item is not None
    assert item.message_id == message["id"]
    assert delivery_of(message["id"]) == DELIVERY_SENDING
    assert outbox_status(message["id"]) == "sending"


def test_claim_is_fifo(client, headers):
    """Порядок доставки совпадает с порядком отправки мастером."""
    ticket_id = seed_ticket(client, headers)
    first = queue_message(headers, ticket_id, "первое")
    second = queue_message(headers, ticket_id, "второе")

    assert claim_next(CHANNEL_TELEGRAM).message_id == first["id"]
    assert claim_next(CHANNEL_TELEGRAM).message_id == second["id"]
    assert claim_next(CHANNEL_TELEGRAM) is None


def test_mark_sent_flips_both_tables(client, headers):
    ticket_id = seed_ticket(client, headers)
    message = queue_message(headers, ticket_id)

    item = claim_next(CHANNEL_TELEGRAM)
    mark_sent(item)

    assert delivery_of(message["id"]) == DELIVERY_SENT
    assert outbox_status(message["id"]) == "sent"
    assert pending_count(CHANNEL_TELEGRAM) == 0


def test_failure_returns_to_queue_until_attempts_exhausted(client, headers):
    ticket_id = seed_ticket(client, headers)
    message = queue_message(headers, ticket_id)

    item = None
    for _ in range(MAX_ATTEMPTS):
        item = claim_next(CHANNEL_TELEGRAM)
        assert item is not None, "очередь не должна опустеть раньше MAX_ATTEMPTS"
        mark_failed(item, "Telegram недоступен")

    assert delivery_of(message["id"]) == DELIVERY_FAILED
    assert outbox_status(message["id"]) == "failed"
    assert claim_next(CHANNEL_TELEGRAM) is None


def test_permanent_failure_is_not_retried(client, headers):
    """Клиент заблокировал бота: повторять бессмысленно."""
    ticket_id = seed_ticket(client, headers)
    message = queue_message(headers, ticket_id)

    item = claim_next(CHANNEL_TELEGRAM)
    mark_failed(item, "403 blocked by the user", permanent=True)

    assert outbox_status(message["id"]) == "failed"
    assert delivery_of(message["id"]) == DELIVERY_FAILED
    assert claim_next(CHANNEL_TELEGRAM) is None


def test_recover_stale_requeues_interrupted_sends(client, headers):
    """Бот упал в sending — сообщение не должно потеряться."""
    ticket_id = seed_ticket(client, headers)
    message = queue_message(headers, ticket_id)
    item = claim_next(CHANNEL_TELEGRAM)
    assert item is not None

    assert recover_stale(CHANNEL_TELEGRAM) == 1
    assert outbox_status(message["id"]) == "pending"
    assert claim_next(CHANNEL_TELEGRAM) is not None


# ==================== Отправка ====================


def test_process_one_sends_formatted_text(client, headers):
    ticket_id = seed_ticket(client, headers)
    message = queue_message(headers, ticket_id, "Подъедем в 14:00")
    bot = FakeBot()

    assert asyncio.run(process_one(CHANNEL_TELEGRAM, telegram_out(bot))) is True

    assert len(bot.calls) == 1
    kind, chat_id, text = bot.calls[0]
    assert kind == "message"
    assert chat_id == 12345, "клиент 12345 из seed_ticket"
    assert "Фёдор Семёнов" in text
    assert "Подъедем в 14:00" in text
    assert delivery_of(message["id"]) == DELIVERY_SENT


def test_process_one_returns_false_on_empty_queue(client, headers):
    assert asyncio.run(process_one(CHANNEL_TELEGRAM, telegram_out(FakeBot()))) is False


def test_delivery_binds_client_reply_to_ticket(client, headers, db_path):
    """Реплай клиента на ответ мастера должен попасть в ту же заявку."""
    import storage

    _clear_operator_messages(db_path)
    ticket_id = seed_ticket(client, headers)
    queue_message(headers, ticket_id, "Подъедем в 14:00")

    asyncio.run(process_one(CHANNEL_TELEGRAM, telegram_out(FakeBot())))

    # FakeBot вернул message_id 4242 — именно на это сообщение клиент будет реплеить.
    assert storage.operator_message_storage.get_ticket_id(4242) == ticket_id


def test_telegram_consumer_ignores_email_ticket(client, headers):
    """Письма обслуживает mail_bot: телеграммовский консьюмер их не забирает."""
    ticket_id = seed_ticket(client, headers)
    with transaction() as connection:
        connection.execute(
            "UPDATE tickets SET source = 'email', client_id = 'ivan@mail.ru' WHERE id = ?",
            (ticket_id,),
        )
    queue_message(headers, ticket_id)

    assert asyncio.run(process_one(CHANNEL_TELEGRAM, telegram_out(FakeBot()))) is False
    assert pending_count(CHANNEL_EMAIL) == 1


def test_process_one_records_failure_without_crashing(client, headers):
    ticket_id = seed_ticket(client, headers)
    queue_message(headers, ticket_id)

    class BrokenBot:
        async def send_message(self, **kwargs):
            raise RuntimeError("connection reset")

    assert asyncio.run(process_one(CHANNEL_TELEGRAM, telegram_out(BrokenBot()))) is True
    # Сообщение вернулось в очередь, а не потерялось и не упало в failed.
    assert pending_count(CHANNEL_TELEGRAM) == 1


def test_attachment_is_sent_after_text(client, headers):
    ticket_id = seed_ticket(client, headers)
    attachment_id = client.post(
        "/v1/attachments",
        files={"file": ("photo.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
        data={"ticket_id": ticket_id},
        headers=headers,
    ).json()["attachment"]["attachment_id"]

    queue_message(headers, ticket_id, "Смотрите фото", attachments=[attachment_id])
    bot = FakeBot()

    asyncio.run(process_one(CHANNEL_TELEGRAM, telegram_out(bot)))

    kinds = [call[0] for call in bot.calls]
    assert kinds == ["message", "photo"], "текст первым, потом файл"
    assert bot.calls[1][2] == b"\xff\xd8\xff\xe0fake"


def test_document_attachment_is_sent_as_file(client, headers):
    ticket_id = seed_ticket(client, headers)
    attachment_id = client.post(
        "/v1/attachments",
        files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
        data={"ticket_id": ticket_id},
        headers=headers,
    ).json()["attachment"]["attachment_id"]

    queue_message(headers, ticket_id, "Счёт", attachments=[attachment_id])
    bot = FakeBot()

    asyncio.run(process_one(CHANNEL_TELEGRAM, telegram_out(bot)))

    kinds = [call[0] for call in bot.calls]
    assert kinds == ["message", "document"]
    assert bot.calls[1][2] == "doc.pdf"
    assert bot.calls[1][3] == b"%PDF-1.4"


def test_missing_ticket_fails_message_permanently(client, headers):
    """Заявку удалили, пока сообщение ждало в очереди."""
    ticket_id = seed_ticket(client, headers)
    message = queue_message(headers, ticket_id)
    drop_ticket(ticket_id)

    asyncio.run(process_one(CHANNEL_TELEGRAM, telegram_out(FakeBot())))

    assert outbox_status(message["id"]) == "failed"
    assert delivery_of(message["id"]) == DELIVERY_FAILED


# ==================== Синхронизация с клиентом ====================


def test_delivery_change_reaches_owner_through_sync(client, headers):
    """Клиент узнаёт о смене статуса из ленты событий, а не из ответа POST."""
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)
    message = queue_message(headers, ticket_id)

    cursor = client.get("/v1/sync", headers=headers).json()["cursor"]
    item = claim_next(CHANNEL_TELEGRAM)
    mark_sent(item)

    events = client.get(f"/v1/sync?cursor={cursor}", headers=headers).json()["events"]
    updates = [e for e in events if e["type"] == "message.updated"]
    assert updates, "смена статуса должна прийти событием"
    assert updates[-1]["data"]["delivery"] == DELIVERY_SENT
    assert updates[-1]["data"]["message_id"] == message["id"]


def test_delivery_states_follow_documented_order(client, headers):
    """queued -> sending -> sent, как в README клиента (6.4)."""
    ticket_id = seed_ticket(client, headers)
    message = queue_message(headers, ticket_id)

    assert delivery_of(message["id"]) == DELIVERY_QUEUED
    item = claim_next(CHANNEL_TELEGRAM)
    assert delivery_of(message["id"]) == DELIVERY_SENDING
    mark_sent(item)
    assert delivery_of(message["id"]) == DELIVERY_SENT


def test_pending_peek_does_not_consume(client, headers):
    ticket_id = seed_ticket(client, headers)
    queue_message(headers, ticket_id)

    assert peek_next(CHANNEL_TELEGRAM) is not None
    assert peek_next(CHANNEL_TELEGRAM) is not None
    assert claim_next(CHANNEL_TELEGRAM) is not None
    assert peek_next(CHANNEL_TELEGRAM) is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
