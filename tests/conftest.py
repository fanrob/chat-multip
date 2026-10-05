"""Фикстуры для тестов API.

Тесты работают на копии боевой БД из full.db: так проверяется, что схема
накладывается на реальные данные бота (5-значные id заявок, статусы
new/taken), а не только на пустую базу, которую создаст CREATE TABLE.
"""

import shutil
import sys
import uuid
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEED_DB = PROJECT_ROOT / "full.db"


@pytest.fixture()
def db_path(tmp_path, monkeypatch) -> str:
    """Временная БД + подмена пути через переменную окружения.

    Так все модули (db, store, events, auth) видят один и тот же файл:
    они читают путь из CHAT_MULTI_DB, а не из аргументов.
    """
    target = tmp_path / "test.db"
    if SEED_DB.is_file():
        shutil.copy(SEED_DB, target)
    else:
        target.touch()

    monkeypatch.setenv("CHAT_MULTI_DB", str(target))
    monkeypatch.setenv("CHAT_MULTI_ATTACHMENTS", str(tmp_path / "attachments"))

    from api import schema

    schema.apply(str(target))
    return str(target)


@pytest.fixture(autouse=True)
def isolate_legacy_storages(db_path, monkeypatch) -> None:
    """Переводит хранилища ботов на временную БД.

    src/storage.py берёт путь из config.FULL_DB, который при импорте уже
    зафиксирован как 'full.db' относительно рабочего каталога. Тесты работают
    с API-подключением (CHAT_MULTI_DB), поэтому без этого патча бот-хранилища
    писали бы в боевую базу. Автоприменение нужно всем тестам: outbox пишет
    привязку отправленных сообщений через operator_message_storage.
    """
    from config import settings_storage
    from storage import master_storage, operator_message_storage, ticket_storage

    for storage in (ticket_storage, operator_message_storage, master_storage, settings_storage):
        monkeypatch.setattr(storage, "db_path", db_path)


@pytest.fixture()
def client(db_path):
    """TestClient с применёнными миграциями."""
    from fastapi.testclient import TestClient

    from api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def make_instance() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def instance_id() -> str:
    return make_instance()


@pytest.fixture()
def headers() -> dict:
    """Заголовки зарегистрированного мастера (Фёдор, без цеха)."""
    instance = make_instance()
    from api import auth

    auth.get_or_create(instance, "Фёдор Семёнов", None)
    return {auth.HEADER_NAME: instance}


@pytest.fixture()
def second_headers() -> dict:
    """Второй мастер (Пётр) — для проверки совместной работы."""
    instance = make_instance()
    from api import auth

    auth.get_or_create(instance, "Пётр Кузнецов", None)
    return {auth.HEADER_NAME: instance}


def seed_ticket(client, headers, ticket_id: str = "t_test01") -> str:
    """Кладёт заявку в ленту напрямую в БД.

    Созданием заявок занимается Telegram-бот, а не API, поэтому в тестах
    мы имитируем его запись — ровно так же, как это делает бот.
    """
    from datetime import datetime

    from api.db import transaction

    now = datetime.now().isoformat()
    with transaction() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO tickets
                (id, source, client_id, client_name, text, status, taken_by, created_at,
                 message_id, subject)
            VALUES (?, 'telegram', '12345', 'Иван П.', 'Не могу пригнать машину',
                    'new', NULL, ?, 1, 'Левый баккер')
            """,
            (ticket_id, now),
        )
    return ticket_id
