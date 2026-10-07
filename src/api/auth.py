"""Идентификация мастера по заголовку X-Instance-Id.

Модель v1: приложение при первом запуске генерирует UUID (instance_id) и
хранит его у себя. Сервер выдаёт мастеру постоянный master_uid и больше
не спрашивает Telegram ID.

Почему так: мастер может работать без Telegram вообще, а позже появиться
в Viber или в Telegram — uid останется тем же, и переписка не распадётся на
несколько историй.

Белый список: без интернета и TLS защита условная, поэтому дополнительно
проверяем instance_id по списку в settings. Если списка нет — принимаем
любой валидный UUID (режим первого запуска), но пишем в лог.
"""

import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from fastapi import Depends, Header

from api import errors
from api.db import reading, transaction

logger = logging.getLogger(__name__)

#: Разрешённый формат instance_id: обычный UUID в нижнем регистре.
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

#: Ключ в settings со списком разрешённых instance_id (через запятую).
ALLOWLIST_KEY = "api_instance_allowlist"

HEADER_NAME = "X-Instance-Id"


@dataclass(frozen=True)
class Master:
    """Мастер, как его видит API."""

    master_uid: str
    instance_id: str
    full_name: str
    workshop_id: Optional[int]
    is_active: bool
    created_at: str
    last_seen_at: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Master":
        return cls(
            master_uid=row["master_uid"],
            instance_id=row["instance_id"],
            full_name=row["full_name"] or "",
            workshop_id=row["workshop_id"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
        )


def new_instance_id() -> str:
    """Генерирует instance_id для новой установки приложения."""
    return str(uuid.uuid4())


def new_master_uid() -> str:
    """Внутренний идентификатор мастера, видимый в API.

    Короткий префикс + 12 hex-символов от UUID: читаем в логах и сообщениях,
    при этом не угадывается перебором.
    """
    return "m_" + uuid.uuid4().hex[:12]


def normalize_instance_id(raw: Optional[str]) -> str:
    """Приводит instance_id к каноническому виду или поднимает 401/422."""
    if not raw:
        raise errors.unauthorized(f"Заголовок {HEADER_NAME} обязателен")
    value = raw.strip().lower()
    if not UUID_RE.match(value):
        raise errors.validation_error(
            f"{HEADER_NAME} должен быть UUID в формате 8-4-4-4-12",
            header=HEADER_NAME,
        )
    return value


def allowlist() -> List[str]:
    """Читает белый список instance_id из settings.

    Пустой список означает «разрешено всем» — это штатный режим, пока
    администратор не заполнит api_instance_allowlist.
    """
    with reading() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (ALLOWLIST_KEY,)
        ).fetchone()
    if not row or not row["value"]:
        return []
    return [
        part.strip().lower()
        for part in row["value"].split(",")
        if part.strip()
    ]


def is_allowed(instance_id: str) -> bool:
    """Проверяет instance_id против белого списка."""
    allowed = allowlist()
    if not allowed:
        logger.debug("Белый список пуст, принимаем любой instance_id")
        return True
    return instance_id in allowed


def find_by_instance(instance_id: str) -> Optional[Master]:
    """Ищет мастера по instance_id."""
    with reading() as connection:
        row = connection.execute(
            "SELECT * FROM api_masters WHERE instance_id = ?", (instance_id,)
        ).fetchone()
    return Master.from_row(row) if row else None


def get_or_create(instance_id: str, full_name: str, workshop_id: Optional[int]) -> Master:
    """Регистрирует мастера или обновляет его профиль.

    Вызывается из POST /session. Если ФИО или цех не переданы, старые значения
    сохраняются — иначе клиент, который шлёт только instance_id, стёр бы данные.
    """
    now = _now()
    with transaction() as connection:
        row = connection.execute(
            "SELECT * FROM api_masters WHERE instance_id = ?", (instance_id,)
        ).fetchone()

        if row is None:
            master_uid = new_master_uid()
            connection.execute(
                """
                INSERT INTO api_masters
                    (master_uid, instance_id, full_name, workshop_id,
                     is_active, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (master_uid, instance_id, full_name, workshop_id, now, now),
            )
            logger.info("Новый мастер %s (%s)", master_uid, full_name or "без имени")
        else:
            master_uid = row["master_uid"]
            name = full_name.strip() or row["full_name"]
            workshop = workshop_id if workshop_id is not None else row["workshop_id"]
            connection.execute(
                "UPDATE api_masters SET full_name = ?, workshop_id = ?, last_seen_at = ? "
                "WHERE master_uid = ?",
                (name, workshop, now, master_uid),
            )

        fresh = connection.execute(
            "SELECT * FROM api_masters WHERE master_uid = ?", (master_uid,)
        ).fetchone()

    return Master.from_row(fresh)


def touch(master_uid: str) -> None:
    """Обновляет last_seen_at. Вызывается на каждый авторизованный запрос."""
    with transaction() as connection:
        connection.execute(
            "UPDATE api_masters SET last_seen_at = ? WHERE master_uid = ?",
            (_now(), master_uid),
        )


def workshop_name(workshop_id: Optional[int]) -> Optional[str]:
    if not workshop_id:
        return None
    with reading() as connection:
        row = connection.execute(
            "SELECT name FROM workshops WHERE id = ?", (workshop_id,)
        ).fetchone()
    return row["name"] if row else None


def _now() -> str:
    return datetime.now().isoformat()


async def current_master(
    x_instance_id: str = Header(
        default=None,
        alias=HEADER_NAME,
        description="UUID, сгенерированный приложением при первом запуске",
    ),
) -> Master:
    """FastAPI-зависимость: возвращает мастера по X-Instance-Id.

    401 — заголовка нет или он не UUID.
    403 — instance_id не в белом списке либо мастер отключён.
    404 — мастер ещё не проходил POST /session.
    """
    instance_id = normalize_instance_id(x_instance_id)

    if not is_allowed(instance_id):
        logger.warning("Отклонён instance_id %s (нет в белом списке)", instance_id)
        raise errors.instance_not_whitelisted()

    master = find_by_instance(instance_id)
    if master is None:
        raise errors.ApiError(
            404,
            "master_not_found",
            "Мастер не найден. Сначала выполните POST /v1/session.",
        )
    if not master.is_active:
        raise errors.forbidden("Доступ мастера отключён")

    touch(master.master_uid)
    return master


#: Зависимость для эндпоинтов, требующих мастера.
RequireMaster = Depends(current_master)
