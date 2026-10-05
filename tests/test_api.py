"""Тесты основных сценариев мессенджера: заявки, сообщения, события.

Запуск: python -m pytest tests -v
"""

import sqlite3
import time
import uuid

import pytest
from conftest import seed_ticket

# ==================== Схема ====================


def test_schema_applies_to_empty_database(tmp_path):
    """Сервер должен подниматься на пустой базе, без Telegram-бота.

    Остальные тесты работают на копии боевой full.db, где legacy-таблицы уже
    есть, поэтому этот случай проверяется отдельно.
    """
    from api import schema

    target = tmp_path / "empty.db"

    assert schema.apply(str(target)) == schema.SCHEMA_VERSION

    with sqlite3.connect(target) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert {"tickets", "masters", "workshops", "operator_messages", "settings"} <= tables
    assert version == schema.SCHEMA_VERSION


def test_schema_keeps_existing_tickets_data(tmp_path):
    """Повторный apply() не должен терять заявки, уже записанные ботом."""
    from api import schema

    target = tmp_path / "with_ticket.db"
    schema.apply(str(target))
    with sqlite3.connect(target) as connection:
        connection.execute(
            """
            INSERT INTO tickets (id, source, client_id, client_name, text, status, created_at)
            VALUES ('10000', 'telegram', '42', 'Иван', 'текст', 'new', '2026-01-01T00:00:00')
            """
        )
        connection.commit()

    schema.apply(str(target))

    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT client_name FROM tickets WHERE id='10000'").fetchone()[0] == 'Иван'


# ==================== Здоровье и сессия ====================


def test_health_without_auth(client):
    """health доступен без заголовка: его дёргает docker healthcheck."""
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_session_creates_master(client, instance_id):
    response = client.post(
        "/v1/session",
        json={"instance_id": instance_id, "full_name": "Фёдор Семёнов"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["master_id"].startswith("m_")
    assert body["profile"]["full_name"] == "Фёдор Семёнов"
    assert body["cursor"] == 0


def test_session_is_idempotent(client, instance_id):
    """Повторный вход не создаёт второго мастера."""
    first = client.post(
        "/v1/session", json={"instance_id": instance_id, "full_name": "Фёдор"}
    ).json()
    second = client.post(
        "/v1/session", json={"instance_id": instance_id, "full_name": "Фёдор Обновлён"}
    ).json()
    assert first["master_id"] == second["master_id"]
    assert second["profile"]["full_name"] == "Фёдор Обновлён"


def test_session_rejects_blank_name(client, instance_id):
    response = client.post(
        "/v1/session", json={"instance_id": instance_id, "full_name": "   "}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_requests_without_header_are_rejected(client):
    response = client.get("/v1/tickets")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_bad_instance_id_is_rejected(client):
    """Заголовок должен быть UUID; мусорные символы отсекает проверка формата."""
    response = client.get("/v1/tickets", headers={"X-Instance-Id": "not-a-uuid"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_unknown_instance_returns_404(client):
    response = client.get("/v1/tickets", headers={"X-Instance-Id": str(uuid.uuid4())})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "master_not_found"


# ==================== Заявки ====================


def test_feed_shows_new_ticket(client, headers):
    ticket_id = seed_ticket(client, headers)
    response = client.get("/v1/tickets?scope=feed", headers=headers)
    assert response.status_code == 200
    ids = [t["id"] for t in response.json()["tickets"]]
    assert ticket_id in ids


def test_feed_ticket_shape(client, headers):
    """Поля карточки соответствуют контракту README."""
    seed_ticket(client, headers)
    ticket = client.get("/v1/tickets?scope=feed", headers=headers).json()["tickets"][0]
    assert ticket["status"] == "new"
    assert ticket["owner"] is None
    assert ticket["members"] == []
    assert ticket["unread_count"] == 0
    assert ticket["client"]["display_name"] == "Иван П."
    assert ticket["subject"] == "Левый баккер"


def test_accept_ticket(client, headers):
    ticket_id = seed_ticket(client, headers)
    response = client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)
    assert response.status_code == 200
    ticket = response.json()["ticket"]
    assert ticket["status"] == "in_progress"
    assert ticket["owner"]["id"].startswith("m_")


def test_accept_twice_returns_409(client, headers):
    """Вторая попытка принять ту же заявку — 409 с именем текущего владельца."""
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    other = {"X-Instance-Id": str(uuid.uuid4())}
    client.post(
        "/v1/session",
        json={"instance_id": other["X-Instance-Id"], "full_name": "Пётр"},
    )

    response = client.post(f"/v1/tickets/{ticket_id}/accept", headers=other)
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "already_accepted"
    assert body["details"]["owner_name"]


def test_release_returns_to_feed(client, headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)
    response = client.post(f"/v1/tickets/{ticket_id}/release", headers=headers)
    assert response.status_code == 200
    assert response.json()["ticket"]["status"] == "new"

    feed = client.get("/v1/tickets?scope=feed", headers=headers).json()["tickets"]
    assert ticket_id in [t["id"] for t in feed]


def test_close_and_reopen(client, headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    closed = client.post(
        f"/v1/tickets/{ticket_id}/close",
        json={"reason": "Ремонт выполнен"},
        headers=headers,
    )
    assert closed.status_code == 200
    assert closed.json()["ticket"]["status"] == "closed"

    reopened = client.post(f"/v1/tickets/{ticket_id}/reopen", headers=headers)
    assert reopened.status_code == 200
    assert reopened.json()["ticket"]["status"] == "in_progress"


def test_close_twice_returns_403(client, headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)
    client.post(f"/v1/tickets/{ticket_id}/close", headers=headers)
    response = client.post(f"/v1/tickets/{ticket_id}/close", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ticket_closed"


def test_decline_hides_ticket_only_for_that_master(client, headers):
    """Отказ скрывает заявку у одного мастера, у второго она остаётся."""
    ticket_id = seed_ticket(client, headers)

    other_instance = str(uuid.uuid4())
    client.post("/v1/session", json={"instance_id": other_instance, "full_name": "Пётр"})
    other = {"X-Instance-Id": other_instance}

    assert client.post(f"/v1/tickets/{ticket_id}/decline", headers=headers).status_code == 200
    # Идемпотентность отказа
    assert client.post(f"/v1/tickets/{ticket_id}/decline", headers=headers).status_code == 200

    feed = client.get("/v1/tickets?scope=feed", headers=headers).json()["tickets"]
    other_feed = client.get("/v1/tickets?scope=feed", headers=other).json()["tickets"]
    mine = [t["id"] for t in feed]
    theirs = [t["id"] for t in other_feed]

    assert ticket_id not in mine
    assert ticket_id in theirs


def test_outsider_cannot_see_in_progress_ticket(client, headers):
    """Заявку в работе видит только её участник."""
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    other_instance = str(uuid.uuid4())
    client.post("/v1/session", json={"instance_id": other_instance, "full_name": "Пётр"})
    other = {"X-Instance-Id": other_instance}

    response = client.get(f"/v1/tickets/{ticket_id}", headers=other)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ticket_not_found"


def test_unknown_ticket_returns_404(client, headers):
    response = client.get("/v1/tickets/t_nope", headers=headers)
    assert response.status_code == 404


# ==================== Участники ====================


def test_add_second_member(client, headers, second_headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    masters = client.get("/v1/masters", headers=headers).json()["masters"]
    other_id = next(m["id"] for m in masters if m["full_name"] == "Пётр Кузнецов")

    response = client.post(
        f"/v1/tickets/{ticket_id}/members", json={"master_id": other_id}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["member"]["role"] == "collaborator"

    # Второй мастер теперь видит заявку и историю
    ticket = client.get(f"/v1/tickets/{ticket_id}", headers=second_headers)
    assert ticket.status_code == 200
    assert ticket.json()["ticket"]["owner"]["id"].startswith("m_")


def test_add_member_twice_returns_409(client, headers, second_headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)
    masters = client.get("/v1/masters", headers=headers).json()["masters"]
    other_id = next(m["id"] for m in masters if m["full_name"] == "Пётр Кузнецов")

    client.post(f"/v1/tickets/{ticket_id}/members", json={"master_id": other_id}, headers=headers)
    response = client.post(
        f"/v1/tickets/{ticket_id}/members", json={"master_id": other_id}, headers=headers
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_member"


def test_add_unknown_member_returns_404(client, headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)
    response = client.post(
        f"/v1/tickets/{ticket_id}/members", json={"master_id": "m_missing"}, headers=headers
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "master_not_found"


def test_owner_cannot_be_removed(client, headers, second_headers):
    """Владельца нельзя отключить — заявка осталась бы без ответственного."""
    ticket_id = seed_ticket(client, headers)
    accepted = client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers).json()
    owner_id = accepted["ticket"]["owner"]["id"]

    response = client.request(
        "DELETE", f"/v1/tickets/{ticket_id}/members/{owner_id}", headers=second_headers
    )
    assert response.status_code == 403


def test_collaborator_can_be_removed(client, headers, second_headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)
    masters = client.get("/v1/masters", headers=headers).json()["masters"]
    other_id = next(m["id"] for m in masters if m["full_name"] == "Пётр Кузнецов")
    client.post(f"/v1/tickets/{ticket_id}/members", json={"master_id": other_id}, headers=headers)

    response = client.request(
        "DELETE", f"/v1/tickets/{ticket_id}/members/{other_id}", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


# ==================== Сообщения ====================


def test_send_message_returns_202_queued(client, headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    response = client.post(
        "/v1/messages",
        json={
            "ticket_id": ticket_id,
            "client_msg_id": str(uuid.uuid4()),
            "text": "Ваша заявка принята, ожидайте до 14:00",
        },
        headers=headers,
    )
    assert response.status_code == 202
    message = response.json()["message"]
    assert message["delivery"] == "queued"
    assert message["sender"] == "master"
    assert message["sender_name"] == "Фёдор Семёнов"
    assert message["seq"] == 1


def test_send_message_is_idempotent(client, headers):
    """Повтор с тем же client_msg_id не создаёт второе сообщение."""
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    client_msg_id = str(uuid.uuid4())
    payload = {"ticket_id": ticket_id, "client_msg_id": client_msg_id, "text": "Привет"}

    first = client.post("/v1/messages", json=payload, headers=headers)
    second = client.post("/v1/messages", json=payload, headers=headers)

    assert first.json()["message"]["id"] == second.json()["message"]["id"]

    history = client.get(f"/v1/tickets/{ticket_id}/messages", headers=headers).json()
    assert len(history["messages"]) == 1


def test_send_message_to_closed_ticket_returns_403(client, headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)
    client.post(f"/v1/tickets/{ticket_id}/close", headers=headers)

    response = client.post(
        "/v1/messages",
        json={"ticket_id": ticket_id, "client_msg_id": str(uuid.uuid4()), "text": "Привет"},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ticket_closed"


def test_send_message_by_outsider_returns_403(client, headers):
    """Мастер, не участвующий в заявке, писать в неё не может."""
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    other_instance = str(uuid.uuid4())
    client.post("/v1/session", json={"instance_id": other_instance, "full_name": "Пётр"})
    other = {"X-Instance-Id": other_instance}

    response = client.post(
        "/v1/messages",
        json={"ticket_id": ticket_id, "client_msg_id": str(uuid.uuid4()), "text": "Привет"},
        headers=other,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_ticket_member"


def test_empty_message_returns_422(client, headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    response = client.post(
        "/v1/messages",
        json={"ticket_id": ticket_id, "client_msg_id": str(uuid.uuid4()), "text": "   "},
        headers=headers,
    )
    assert response.status_code == 422


def test_messages_appear_in_history_for_both_masters(client, headers, second_headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)
    masters = client.get("/v1/masters", headers=headers).json()["masters"]
    other_id = next(m["id"] for m in masters if m["full_name"] == "Пётр Кузнецов")
    client.post(f"/v1/tickets/{ticket_id}/members", json={"master_id": other_id}, headers=headers)

    client.post(
        "/v1/messages",
        json={"ticket_id": ticket_id, "client_msg_id": str(uuid.uuid4()), "text": "Проверка"},
        headers=headers,
    )

    mine = client.get(f"/v1/tickets/{ticket_id}/messages", headers=headers).json()
    theirs = client.get(
        f"/v1/tickets/{ticket_id}/messages", headers=second_headers
    ).json()

    assert len(mine["messages"]) == len(theirs["messages"]) == 1
    assert mine["messages"][0]["text"] == "Проверка"


def test_message_seq_increments(client, headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    seqs = []
    for i in range(3):
        response = client.post(
            "/v1/messages",
            json={
                "ticket_id": ticket_id,
                "client_msg_id": str(uuid.uuid4()),
                "text": f"Сообщение {i}",
            },
            headers=headers,
        )
        seqs.append(response.json()["message"]["seq"])

    assert seqs == [1, 2, 3]


def test_read_marks_messages(client, headers):
    """Прочитано отмечается, unread_count растёт от сообщений клиента."""
    from datetime import datetime

    from api.db import transaction

    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    with transaction() as connection:
        connection.execute(
            "INSERT INTO messages (id, ticket_id, seq, sender, master_uid, sender_name, "
            "text, channel, delivery, created_at) "
            "VALUES ('msg_c1', ?, 1, 'client', NULL, 'Иван П.', 'Вопрос клиента', "
            "'telegram', 'delivered', ?)",
            (ticket_id, datetime.now().isoformat()),
        )

    ticket = client.get(f"/v1/tickets/{ticket_id}", headers=headers).json()["ticket"]
    assert ticket["unread_count"] == 1

    assert client.post(f"/v1/tickets/{ticket_id}/read", json={}, headers=headers).status_code == 200
    # Идемпотентность
    assert client.post(f"/v1/tickets/{ticket_id}/read", json={}, headers=headers).status_code == 200

    ticket = client.get(f"/v1/tickets/{ticket_id}", headers=headers).json()["ticket"]
    assert ticket["unread_count"] == 0


# ==================== События ====================


def test_sync_empty_for_new_master(client, headers):
    response = client.get("/v1/sync?wait=0", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["events"] == []
    assert body["has_more"] is False


def test_sync_returns_accept_event(client, headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    body = client.get("/v1/sync", headers=headers).json()
    types = [event["type"] for event in body["events"]]
    assert "ticket.accepted" in types
    assert body["cursor"] == body["events"][-1]["seq"]


def test_sync_cursor_advances(client, headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    first = client.get("/v1/sync", headers=headers).json()
    cursor = first["cursor"]
    assert cursor > 0

    # Повторный запрос с тем же курсором не возвращает старые события
    second = client.get(f"/v1/sync?cursor={cursor}&wait=0", headers=headers).json()
    assert second["events"] == []

    client.post("/v1/messages", json={
        "ticket_id": ticket_id, "client_msg_id": str(uuid.uuid4()), "text": "Привет"
    }, headers=headers)

    third = client.get(f"/v1/sync?cursor={cursor}", headers=headers).json()
    assert [e["type"] for e in third["events"]] == ["message.created"]


def test_sync_has_more_flag(client, headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)
    for i in range(5):
        client.post("/v1/messages", json={
            "ticket_id": ticket_id, "client_msg_id": str(uuid.uuid4()), "text": f"Сообщение {i}"
        }, headers=headers)

    body = client.get("/v1/sync?limit=2", headers=headers).json()
    assert len(body["events"]) == 2
    assert body["has_more"] is True


def test_sync_only_returns_own_events(client, headers):
    """Ленты мастеров независимы: чужих событий в них нет."""
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    other_instance = str(uuid.uuid4())
    client.post("/v1/session", json={"instance_id": other_instance, "full_name": "Пётр"})
    other = {"X-Instance-Id": other_instance}

    assert client.get("/v1/sync?wait=0", headers=other).json()["events"] == []


def test_sync_resets_future_cursor(client, headers):
    """Курсор из будущего обнуляется, иначе клиент не догоняет ленту."""
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    body = client.get("/v1/sync?cursor=999999", headers=headers).json()
    assert len(body["events"]) > 0


def test_sync_ticket_filter(client, headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    body = client.get(f"/v1/sync?ticket_id={ticket_id}", headers=headers).json()
    assert all(event["ticket_id"] == ticket_id for event in body["events"])


# ==================== Long-poll ====================


def test_sync_declares_wait_with_documented_default(client):
    """Контракт из README: wait есть и по умолчанию 25 секунд."""
    spec = client.get("/openapi.json").json()
    params = spec["paths"]["/v1/sync"]["get"]["parameters"]
    wait = next(p for p in params if p["name"] == "wait")
    assert wait["schema"]["default"] == 25
    assert "204" in spec["paths"]["/v1/sync"]["get"]["responses"]


def test_sync_with_events_answers_immediately(client, headers):
    """События уже есть — ждать нечего, ответ мгновенный даже при wait=25."""
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    started = time.monotonic()
    response = client.get("/v1/sync?wait=25", headers=headers)
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 2, f"ответ с событиями ждал {elapsed:.1f}с вместо мгновенного"


def test_sync_returns_204_when_nothing_happened(client, headers):
    """Тишина: держим соединение до конца wait и отдаём пустое тело."""
    started = time.monotonic()
    response = client.get("/v1/sync?wait=1", headers=headers)
    elapsed = time.monotonic() - started

    assert response.status_code == 204
    assert response.content == b""
    assert 1 <= elapsed < 5, f"соединение продержалось {elapsed:.1f}с при wait=1"


def test_sync_delivers_event_as_soon_as_it_appears(client, headers):
    """Главный смысл long-poll: событие приходит сразу, не по таймауту."""
    import threading

    from api import events
    from api.db import transaction

    def emit_soon():
        time.sleep(0.3)
        with transaction() as connection:
            events.emit_all(connection, "ticket.released", {"reason": "проверка"})

    emitter = threading.Thread(target=emit_soon)
    emitter.start()
    try:
        started = time.monotonic()
        response = client.get("/v1/sync?wait=15", headers=headers)
        elapsed = time.monotonic() - started
    finally:
        emitter.join()

    assert response.status_code == 200
    assert elapsed < 10, f"ответ ждал таймаут ({elapsed:.1f}с) вместо события"
    assert "ticket.released" in [e["type"] for e in response.json()["events"]]


def test_sync_wait_zero_does_not_hold_connection(client, headers):
    """wait=0 — обычный опрос: пустой список сразу, а не 204."""
    started = time.monotonic()
    response = client.get("/v1/sync?wait=0", headers=headers)
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json()["events"] == []
    assert elapsed < 1


def test_sync_rejects_wait_above_limit(client, headers):
    from api import events

    response = client.get(f"/v1/sync?wait={events.MAX_WAIT_SECONDS + 1}", headers=headers)
    assert response.status_code == 422


def test_sync_expired_cursor_returns_410(client, headers):
    """События вытеснены чисткой — клиенту нужен ресинк, а не тишина."""
    from api.db import transaction

    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)
    client.post("/v1/messages", json={
        "ticket_id": ticket_id, "client_msg_id": str(uuid.uuid4()), "text": "Сообщение"
    }, headers=headers)

    seen = client.get("/v1/sync?wait=0", headers=headers).json()["events"]
    assert len(seen) >= 2
    first_seq = seen[0]["seq"]
    kept_seq = seen[1]["seq"]

    # Так выглядит чистка старых событий: первые строки исчезли из ленты.
    with transaction() as connection:
        connection.execute("DELETE FROM events WHERE seq <= ?", (first_seq,))

    response = client.get("/v1/sync?cursor=0", headers=headers)
    assert response.status_code == 410
    error = response.json()["error"]
    assert error["code"] == "cursor_expired"
    assert error["details"]["min_available_cursor"] == kept_seq - 1

    # Путь восстановления из контракта: клиент берёт min_available_cursor.
    retry = client.get(f"/v1/sync?cursor={kept_seq - 1}&wait=0", headers=headers)
    assert retry.status_code == 200
    assert [e["seq"] for e in retry.json()["events"]] == [kept_seq]


# ==================== Профиль и справочники ====================


def test_patch_profile_broadcasts_event(client, headers):
    response = client.patch(
        "/v1/session/profile", json={"full_name": "Фёдор С."}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["profile"]["full_name"] == "Фёдор С."

    events = client.get("/v1/sync", headers=headers).json()["events"]
    assert any(e["type"] == "masters.directory_changed" for e in events)


def test_masters_directory_hides_instance_id(client, headers):
    body = client.get("/v1/masters", headers=headers).json()
    assert body["masters"], "справочник не должен быть пустым"
    assert "instance_id" not in body["masters"][0]


def test_workshops_list(client, headers):
    response = client.get("/v1/workshops", headers=headers)
    assert response.status_code == 200
    assert isinstance(response.json()["workshops"], list)


def test_master_profile_excludes_instance_id(client, headers):
    body = client.get("/v1/session", headers=headers).json()
    assert "instance_id" not in json_dumps(body)


# ==================== Вложения ====================


def test_upload_attachment_returns_contract_shape(client, headers):
    """Поле id в ответе называется attachment_id — как в README (6.5)."""
    response = client.post(
        "/v1/attachments",
        files={"file": ("photo.jpg", b"\xff\xd8\xff\xe0fake-jpeg", "image/jpeg")},
        headers=headers,
    )
    assert response.status_code == 201
    attachment = response.json()["attachment"]
    assert attachment["attachment_id"].startswith("a_")
    assert attachment["kind"] == "photo"
    assert attachment["filename"] == "photo.jpg"
    assert attachment["mime_type"] == "image/jpeg"
    assert attachment["size"] > 0


def test_upload_rejects_mime_outside_allowlist(client, headers):
    """README 6.5: белый список mime-типов. Исполняемый файл не принимаем."""
    response = client.post(
        "/v1/attachments",
        files={"file": ("evil.exe", b"MZ\x90\x00", "application/x-msdownload")},
        headers=headers,
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_mime_type"


def test_upload_rejected_mime_leaves_no_file_on_disk(client, headers, db_path):
    """Проверка типа должна быть до записи файла, а не после."""
    import os

    folder = os.path.join(os.path.dirname(db_path), "attachments")
    client.post(
        "/v1/attachments",
        files={"file": ("evil.exe", b"MZ\x90\x00", "application/x-msdownload")},
        headers=headers,
    )
    assert not os.path.isdir(folder) or os.listdir(folder) == []


def test_upload_to_missing_ticket_is_404(client, headers):
    response = client.post(
        "/v1/attachments",
        files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
        data={"ticket_id": "t_nope"},
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ticket_not_found"


def test_orphan_attachment_is_invisible_to_other_master(client, headers, second_headers):
    """Файл без заявки виден только загрузившему: правило «участники заявки»
    к нему неприменимо, заявки ещё нет."""
    attachment_id = client.post(
        "/v1/attachments",
        files={"file": ("note.txt", "черновик".encode(), "text/plain")},
        headers=headers,
    ).json()["attachment"]["attachment_id"]

    mine = client.get(f"/v1/attachments/{attachment_id}", headers=headers)
    assert mine.status_code == 200
    assert mine.content == "черновик".encode()

    theirs = client.get(f"/v1/attachments/{attachment_id}", headers=second_headers)
    assert theirs.status_code == 404
    assert theirs.json()["error"]["code"] == "attachment_not_found"


def test_feed_attachment_visible_to_other_master(client, headers, second_headers):
    """Фото заявки в общей ленте доступно всем мастерам — иначе не взять заявку."""
    ticket_id = seed_ticket(client, headers)
    attachment_id = client.post(
        "/v1/attachments",
        files={"file": ("photo.jpg", b"\xff\xd8\xff\xe0fake-jpeg", "image/jpeg")},
        data={"ticket_id": ticket_id},
        headers=headers,
    ).json()["attachment"]["attachment_id"]

    theirs = client.get(f"/v1/attachments/{attachment_id}", headers=second_headers)
    assert theirs.status_code == 200
    assert theirs.content == b"\xff\xd8\xff\xe0fake-jpeg"


def test_attachment_meta_endpoint(client, headers):
    attachment_id = client.post(
        "/v1/attachments",
        files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
        headers=headers,
    ).json()["attachment"]["attachment_id"]

    body = client.get(f"/v1/attachments/{attachment_id}/meta", headers=headers).json()
    assert body["attachment_id"] == attachment_id
    assert body["mime_type"] == "application/pdf"
    assert body["size"] == len(b"%PDF-1.4")


def test_attachment_rides_with_message(client, headers):
    """Загрузили файл, отправили сообщение со ссылкой — файл виден в истории."""
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    attachment_id = client.post(
        "/v1/attachments",
        files={"file": ("photo.jpg", b"\xff\xd8\xff\xe0fake-jpeg", "image/jpeg")},
        data={"ticket_id": ticket_id},
        headers=headers,
    ).json()["attachment"]["attachment_id"]

    response = client.post(
        "/v1/messages",
        json={
            "ticket_id": ticket_id,
            "client_msg_id": str(uuid.uuid4()),
            "text": "Смотрите фото",
            "attachments": [{"attachment_id": attachment_id}],
        },
        headers=headers,
    )
    assert response.status_code == 202
    sent = response.json()["message"]
    assert [a["attachment_id"] for a in sent["attachments"]] == [attachment_id]

    history = client.get(f"/v1/tickets/{ticket_id}/messages", headers=headers).json()
    assert history["messages"][0]["attachments"][0]["attachment_id"] == attachment_id


def test_message_with_unknown_attachment_is_rejected(client, headers):
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    response = client.post(
        "/v1/messages",
        json={
            "ticket_id": ticket_id,
            "client_msg_id": str(uuid.uuid4()),
            "text": "Привет",
            "attachments": [{"attachment_id": "a_missing"}],
        },
        headers=headers,
    )
    assert response.status_code == 422
    # Тот же код, что и в README для 422: клиенту не нужно различать
    # «нет такого файла» и «файл чужой» — обе ситуации требуют одного действия.
    assert response.json()["error"]["code"] == "validation_error"


def test_cannot_send_someone_elses_attachment(client, headers, second_headers):
    """Чужое непривязанное вложение в сообщение подставить нельзя."""
    ticket_id = seed_ticket(client, headers)
    client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    attachment_id = client.post(
        "/v1/attachments",
        files={"file": ("secret.txt", "секрет".encode(), "text/plain")},
        headers=second_headers,
    ).json()["attachment"]["attachment_id"]

    response = client.post(
        "/v1/messages",
        json={
            "ticket_id": ticket_id,
            "client_msg_id": str(uuid.uuid4()),
            "text": "Привет",
            "attachments": [{"attachment_id": attachment_id}],
        },
        headers=headers,
    )
    assert response.status_code in (403, 422)


# ==================== Атомарность состояния и событий ====================


def test_failed_event_rolls_back_ticket_state(client, headers, monkeypatch):
    """Событие и изменение статуса — один коммит.

    Если бы store коммитил смену статуса, а событие писалось вторым вызовом
    transaction(), падение между ними оставило бы заявку взятой, а остальные
    мастера ничего бы об этом не узнали до полного ресинка.
    """
    from api import events

    ticket_id = seed_ticket(client, headers)

    def boom(*args, **kwargs):
        raise RuntimeError("событие не записалось")

    monkeypatch.setattr(events, "emit_all", boom)
    with pytest.raises(RuntimeError):
        client.post(f"/v1/tickets/{ticket_id}/accept", headers=headers)

    ticket = client.get(f"/v1/tickets/{ticket_id}", headers=headers).json()["ticket"]
    assert ticket["status"] == "new", "изменение состояния не должно было уцелеть"


def json_dumps(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
