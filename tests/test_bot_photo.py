"""Тест обработчика фото в Telegram-боте.

Проверяем, что бот больше не игнорирует сообщения с фото: скачивает самый
крупный размер, сохраняет файл в заявку и кладёт сообщение в историю, откуда
его забирает API. Сеть не используется — Update/Context подменены заглушками.
"""

import asyncio
from datetime import datetime

import pytest
from conftest import seed_ticket  # noqa: F401  (клиент/БД-фикстуры)

pytest.importorskip("telegram")

import telegram_bot.bot as bot_module
from api.db import reading, transaction


def _seed_ticket_for(client_id: str, ticket_id: str = "t_photo01") -> str:
    """Заявка от конкретного клиента — так же, как её создаёт бот."""
    now = datetime.now().isoformat()
    with transaction() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO tickets
                (id, source, client_id, client_name, text, status, created_at,
                 message_id, subject)
            VALUES (?, 'telegram', ?, 'Иван П.', 'старая заявка', 'new', ?, 1, 'Тема')
            """,
            (ticket_id, client_id, now),
        )
    return ticket_id


def _attachments_of(ticket_id: str) -> list:
    with reading() as connection:
        return connection.execute(
            "SELECT * FROM api_attachments WHERE ticket_id = ? ORDER BY created_at",
            (ticket_id,),
        ).fetchall()


class FakeUser:
    def __init__(self, user_id: int, full_name: str = "Иван П."):
        self.id = user_id
        self.full_name = full_name
        self.username = None


class FakePhoto:
    file_id = "f1"
    file_unique_id = "uniq1"


class FakeFile:
    def __init__(self, data: bytes):
        self._data = data

    async def download_as_bytearray(self):
        return bytearray(self._data)


class FakeBot:
    def __init__(self, data: bytes):
        self._data = data
        self.requested: list = []

    async def get_file(self, file_id):
        self.requested.append(file_id)
        return FakeFile(self._data)


class FakeMessage:
    def __init__(self, *, text=None, caption=None, photo=None, message_id=1):
        self.text = text
        self.caption = caption
        self.photo = photo or []
        self.message_id = message_id
        self.reply_to_message = None
        self.replies: list = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, message: FakeMessage, user: FakeUser):
        self.message = message
        self.effective_user = user


class FakeContext:
    def __init__(self, bot):
        self.bot = bot
        self.user_data = {}


def _run_photo(user_id: int, data: bytes, *, caption=None, message_id=1, monkeypatch=None):
    import telegram_bot.bot as module

    monkeypatch.setattr(module, "is_admin", lambda uid: False)
    message = FakeMessage(caption=caption, photo=[FakePhoto()], message_id=message_id)
    update = FakeUpdate(message, FakeUser(user_id))
    context = FakeContext(FakeBot(data))
    asyncio.run(module.handle_telegram_message(update, context))
    return message, context


def test_photo_follow_up_is_saved_to_open_ticket(client, headers, monkeypatch):
    ticket_id = _seed_ticket_for("555000111")
    data = b"\xff\xd8\xff\xe0fake-jpeg"

    message, context = _run_photo(
        555000111, data, caption="Смотрите фото", message_id=777, monkeypatch=monkeypatch
    )

    assert message.replies == ["✅ Ваше сообщение отправлено мастеру."]
    assert context.bot.requested == ["f1"], "скачиваем самый крупный размер"

    history = client.get(f"/v1/tickets/{ticket_id}/messages", headers=headers).json()
    attachment = history["messages"][-1]["attachments"][0]
    assert attachment["kind"] == "photo"
    assert attachment["source"] == "client"

    saved = _attachments_of(ticket_id)
    assert len(saved) == 1
    with open(saved[0]["storage_key"], "rb") as handle:
        assert handle.read() == data


def test_photo_without_caption_is_saved(client, headers, monkeypatch):
    ticket_id = _seed_ticket_for("555000112")
    data = b"\xff\xd8\xff\xe0fake"

    _run_photo(555000112, data, message_id=778, monkeypatch=monkeypatch)

    history = client.get(f"/v1/tickets/{ticket_id}/messages", headers=headers).json()
    assert history["messages"][-1]["text"] == ""
    assert history["messages"][-1]["attachments"]


def test_first_photo_creates_ticket_with_attachment(client, headers, monkeypatch):
    """Фото как первое сообщение тоже создаёт заявку, фото не теряется."""
    from storage import ticket_storage

    data = b"\xff\xd8\xff\xe0fake"
    message, _ = _run_photo(
        555000222, data, caption="Вот фото", message_id=779, monkeypatch=monkeypatch
    )

    assert message.replies, "клиент должен получить подтверждение заявки"

    ticket = ticket_storage.get_open_ticket(555000222)
    assert ticket is not None
    assert ticket.text == "Вот фото"

    history = client.get(f"/v1/tickets/{ticket.id}/messages", headers=headers).json()
    assert history["messages"][-1]["attachments"][0]["kind"] == "photo"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
