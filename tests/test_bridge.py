"""Тесты моста ботов -> API.

Мост пишет в messages/events то, что клиент уже увидел в Telegram или в почте.
Здесь проверяется, что события доходят до мастеров, повторные апдейты не дублируются,
а поломка синхронизации не ломает обработчик апдейта.
"""

import pytest
from conftest import seed_ticket

from api import auth, store
from api.db import reading, transaction
from bridge import (
    record_client_message,
    record_email_message,
    record_ticket_closed,
    record_ticket_created,
    source_message_id,
)

CHAT_ID = 555000111


def messages_of(ticket_id: str) -> list:
    with reading() as connection:
        return connection.execute(
            "SELECT * FROM messages WHERE ticket_id = ? ORDER BY seq", (ticket_id,)
        ).fetchall()


def events_of(master_uid: str, ticket_id: str) -> list:
    with reading() as connection:
        return connection.execute(
            "SELECT * FROM events WHERE master_uid = ? AND ticket_id = ? ORDER BY seq",
            (master_uid, ticket_id),
        ).fetchall()


def channels_of(ticket_id: str) -> list[str]:
    with reading() as connection:
        return [
            row["channel"]
            for row in connection.execute(
                "SELECT channel FROM messages WHERE ticket_id = ? ORDER BY seq", (ticket_id,)
            )
        ]


def master_uid_of(headers: dict) -> str:
    with reading() as connection:
        return connection.execute(
            "SELECT master_uid FROM api_masters WHERE instance_id = ?",
            (headers[auth.HEADER_NAME],),
        ).fetchone()["master_uid"]


# ==================== Заявка из Telegram ====================


def test_ticket_created_reaches_active_masters(client, headers, second_headers):
    ticket_id = seed_ticket(client, headers)

    assert record_ticket_created(ticket_id) is None

    for master_headers in (headers, second_headers):
        master = master_uid_of(master_headers)
        types = [e["type"] for e in events_of(master, ticket_id)]
        assert types == ["ticket.created"], "новый мастер должен узнать о заявке"


def test_ticket_created_skips_inactive_masters(client, headers, second_headers):
    """Выключенное рабочее место не должно получать уведомлений."""
    ticket_id = seed_ticket(client, headers)
    with transaction() as connection:
        connection.execute(
            "UPDATE api_masters SET is_active = 0 WHERE master_uid = ?",
            (master_uid_of(second_headers),),
        )

    record_ticket_created(ticket_id)

    assert events_of(master_uid_of(second_headers), ticket_id) == []


def test_ticket_created_skipped_masters(client, headers, second_headers):
    ticket_id = seed_ticket(client, headers)
    store.decline_ticket(_master(second_headers), ticket_id)

    record_ticket_created(ticket_id)

    assert events_of(master_uid_of(second_headers), ticket_id) == []


def test_unknown_ticket_does_not_raise(client, headers):
    assert record_ticket_created("t_nope") is None


# ==================== Сообщение клиента ====================


def test_client_message_lands_in_history(client, headers):
    ticket_id = seed_ticket(client, headers)
    store.accept_ticket(_master(headers), ticket_id)

    message = record_client_message(
        ticket_id, "Иван П.", "Подъедем в 14:00", chat_id=CHAT_ID, message_id=9001
    )

    assert message is not None
    assert message["sender"] == "client"
    assert message["sender_name"] == "Иван П."
    assert message["delivery"] == "delivered", "клиенту уже отправили это в Telegram"
    assert message["seq"] == 1

    types = [e["type"] for e in events_of(master_uid_of(headers), ticket_id)]
    assert "message.created" in types


def test_client_message_is_idempotent_per_update(client, headers):
    """Telegram повторно доставит апдейт после реконнекта — дубля быть не должно."""
    ticket_id = seed_ticket(client, headers)
    store.accept_ticket(_master(headers), ticket_id)

    first = record_client_message(ticket_id, "Иван П.", "текст", chat_id=CHAT_ID, message_id=9002)
    second = record_client_message(ticket_id, "Иван П.", "текст", chat_id=CHAT_ID, message_id=9002)

    assert first["id"] == second["id"]
    assert len(messages_of(ticket_id)) == 1


def test_same_message_id_in_other_chat_is_new_message(client, headers):
    """message_id уникален только внутри чата: у другого клиента это другое сообщение."""
    ticket_id = seed_ticket(client, headers)
    store.accept_ticket(_master(headers), ticket_id)

    record_client_message(ticket_id, "Иван П.", "текст", chat_id=CHAT_ID, message_id=9003)
    record_client_message(ticket_id, "Пётр П.", "текст", chat_id=777000222, message_id=9003)

    assert len(messages_of(ticket_id)) == 2


def test_seq_continues_after_master_message(client, headers):
    ticket_id = seed_ticket(client, headers)
    master = _master(headers)
    store.accept_ticket(master, ticket_id)
    store.create_message(master, ticket_id, "api-1", "От мастера", [])

    client_message = record_client_message(
        ticket_id, "Иван П.", "Спасибо", chat_id=CHAT_ID, message_id=9004
    )

    assert client_message["seq"] == 2, "нумерация сквозная по заявке"


def test_closed_ticket_ignores_client_message(client, headers):
    ticket_id = seed_ticket(client, headers)
    master = _master(headers)
    store.accept_ticket(master, ticket_id)
    store.close_ticket(master, ticket_id)

    message = record_client_message(
        ticket_id, "Иван П.", "Поздно", chat_id=CHAT_ID, message_id=9005
    )

    assert message is None
    assert messages_of(ticket_id) == []


def test_deleted_ticket_ignores_client_message(client, headers):
    ticket_id = seed_ticket(client, headers)
    with transaction() as connection:
        connection.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))

    assert record_client_message(
        ticket_id, "Иван", "тест", chat_id=CHAT_ID, message_id=9006
    ) is None


def test_sync_error_does_not_break_update_handler(client, headers, monkeypatch):
    """Мост не имеет права ронять апдейт: иначе клиент не сможет закрыть заявку."""
    ticket_id = seed_ticket(client, headers)

    def boom(*args, **kwargs):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(store, "append_incoming_message", boom)

    assert record_client_message(
        ticket_id, "Иван", "текст", chat_id=CHAT_ID, message_id=9007
    ) is None


def test_source_message_id_scopes_by_chat():
    assert source_message_id(1, 2) == "tg:1:2"
    assert source_message_id(1, 2) != source_message_id(3, 2)


# ==================== Письмо клиента ====================


def test_email_message_lands_in_history(client, headers):
    ticket_id = seed_ticket(client, headers)
    store.accept_ticket(_master(headers), ticket_id)

    message = record_email_message(ticket_id, "Иван П.", "Уточнение", message_id="<abc@mail>")

    assert message is not None
    assert message["sender"] == "client"
    assert message["delivery"] == "delivered"
    assert channels_of(ticket_id) == ["email"], "канал хранится, чтобы приложение видело источник"
    assert "message.created" in [e["type"] for e in events_of(master_uid_of(headers), ticket_id)]


def test_email_message_is_idempotent_by_message_id(client, headers):
    """Реконнект к IMAP повторно отдаёт уже прочитанные письма."""
    ticket_id = seed_ticket(client, headers)
    store.accept_ticket(_master(headers), ticket_id)

    first = record_email_message(ticket_id, "Иван", "текст", message_id="<dup@mail>")
    second = record_email_message(ticket_id, "Иван", "текст", message_id="<dup@mail>")

    assert first["id"] == second["id"]
    assert len(messages_of(ticket_id)) == 1


def test_email_messages_without_id_are_not_deduplicated(client, headers):
    """Без Message-ID дедупликация невозможна — письма должны попадать в историю."""
    ticket_id = seed_ticket(client, headers)
    store.accept_ticket(_master(headers), ticket_id)

    record_email_message(ticket_id, "Иван", "первое")
    record_email_message(ticket_id, "Иван", "второе")

    assert len(messages_of(ticket_id)) == 2


def test_email_error_does_not_break_mail_loop(client, headers, monkeypatch):
    """Ошибка синхронизации не должна обрывать проверку почты."""
    ticket_id = seed_ticket(client, headers)

    def boom(*args, **kwargs):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(store, "append_incoming_message", boom)

    assert record_email_message(ticket_id, "Иван", "текст", message_id="<x@mail>") is None


# ==================== Закрытие заявки клиентом ====================


def test_ticket_closed_reaches_masters(client, headers, second_headers):
    ticket_id = seed_ticket(client, headers)
    store.accept_ticket(_master(headers), ticket_id)

    record_ticket_closed(ticket_id)

    for master_headers in (headers, second_headers):
        types = [e["type"] for e in events_of(master_uid_of(master_headers), ticket_id)]
        assert types[-1] == "ticket.closed", "приложение должно увидеть закрытие сразу"


def test_ticket_closed_for_unknown_id_is_noop(client, headers):
    record_ticket_closed("t_nope")

    with reading() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM events WHERE ticket_id = 't_nope'"
        ).fetchone()["n"] == 0


def _master(headers: dict):
    return auth.get_or_create(headers[auth.HEADER_NAME], "Фёдор Семёнов", None)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
