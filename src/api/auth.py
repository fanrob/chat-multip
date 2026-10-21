"""Идентификация мастера по заголовку X-Instance-Id.

Модель v1: приложение при первом запуске генерирует UUID (instance_id) и
хранит его у себя. Сервер выдаёт мастеру постоянный master_uid и больше
не спрашивает Telegram ID.

Почему так: мастер может работать без Telegram вообще, а позже появиться
в Viber или в Telegram — uid останется тем же, и переписка не распадётся на
несколько историй.

Белый список и отложенная активация: без интернета и TLS защита условная,
поэтому instance_id проверяется по списку в settings (ключ
api_instance_allowlist, через запятую). Пустой список — режим первого
запуска, входим сразу. Если список заполнен и id в него не попал, мастер
всё равно записывается в БД, но неактивным (is_active=0): администратор
получает уведомление в Telegram с кнопками «Принять/Отказать», а клиент
до подтверждения получает 403 — как и раньше. Администратор может
подтвердить кнопкой в уведомлении или через панель управления мастерами.
"""

import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

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

#: Канал api_outbox для уведомлений администратору. Соответствует
#: outbox_queue.CHANNEL_TELEGRAM_NOTIFY: уведомление не должно вклиниваться
#: в FIFO-очередь ответов мастера клиенту.
ADMIN_NOTIFY_CHANNEL = "telegram_notify"

HEADER_NAME = "X-Instance-Id"

#: Статусы register(): 'registered' — вход разрешён (мастер может быть
#: отключён администратором, это видно по profile.is_active), 'pending' —
#: ждёт подтверждения администратора, клиенту отдан 403.
STATUS_REGISTERED = "registered"
STATUS_PENDING = "pending"


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


def register(instance_id: str, full_name: str, workshop_id: Optional[int]) -> Tuple[Master, str]:
    """Регистрирует мастера или обновляет его профиль. Вызывается из POST /session.

    Возвращает (мастер, статус). Статус STATUS_PENDING — id не в белом
    списке: мастер записан неактивным, администратору ушло уведомление в
    очередь, клиенту routes отдаёт 403.

    Если ФИО или цех не переданы, старые значения сохраняются — иначе
    клиент, который шлёт только instance_id, стёр бы данные.
    """
    now = _now()
    with transaction() as connection:
        row = connection.execute(
            "SELECT * FROM api_masters WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        allowed_ids = allowlist()

        if row is None:
            whitelisted = not allowed_ids or instance_id in allowed_ids
            master_uid = new_master_uid()
            connection.execute(
                """
                INSERT INTO api_masters
                    (master_uid, instance_id, full_name, workshop_id,
                     is_active, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (master_uid, instance_id, full_name, workshop_id,
                 1 if whitelisted else 0, now, now),
            )
            if whitelisted:
                logger.info("Новый мастер %s (%s)", master_uid, full_name or "без имени")
                status = STATUS_REGISTERED
            else:
                logger.info(
                    "Новый мастер %s (%s) ждёт подтверждения: %s не в белом списке",
                    master_uid, full_name or "без имени", instance_id,
                )
                _enqueue_join_request(connection, master_uid, instance_id, full_name, workshop_id)
                status = STATUS_PENDING
        else:
            master_uid = row["master_uid"]
            name = full_name.strip() or row["full_name"]
            workshop = workshop_id if workshop_id is not None else row["workshop_id"]
            is_active = bool(row["is_active"])
            if not is_active and allowed_ids and instance_id in allowed_ids:
                # Явное попадание в белый список — админ разрешил вход вручную.
                is_active = True
            connection.execute(
                "UPDATE api_masters SET full_name = ?, workshop_id = ?, is_active = ?, last_seen_at = ? "
                "WHERE master_uid = ?",
                (name, workshop, int(is_active), now, master_uid),
            )
            if is_active:
                status = STATUS_REGISTERED
            elif allowed_ids:
                # Список заполнен, id в нём нет — ждём подтверждения.
                # Уведомление уже уходило при создании, повтор не шлём.
                status = STATUS_PENDING
            else:
                # Открытый режим: админ отключил мастера — оставляем
                # отключённым, но вход формально разрешён (как раньше:
                # 200 с is_active=false, остальные запросы 403).
                status = STATUS_REGISTERED

        fresh = connection.execute(
            "SELECT * FROM api_masters WHERE master_uid = ?", (master_uid,)
        ).fetchone()

    return Master.from_row(fresh), status


def _enqueue_join_request(
    connection: sqlite3.Connection,
    master_uid: str,
    instance_id: str,
    full_name: str,
    workshop_id: Optional[int],
) -> None:
    """Кладёт администратору уведомление о новом мастере, ждущем входа.

    Вызывается внутри той же транзакции, что создаёт мастера: фантомного
    «просителя» без уведомления не будет. Доставку выполняет процесс бота
    (outbox-консьюмер канала telegram_notify), кнопки Принять/Отказать
    обрабатывает telegram_bot.bot.admin_callback.
    """
    lines = ["🔔 Новый мастер просит доступ к серверу.", ""]
    lines.append(f"ФИО: {full_name.strip() or 'не указано'}")
    lines.append(f"Цех: {workshop_name(workshop_id) or 'не указан'}")
    lines.append(f"instance_id: {instance_id}")
    payload = {
        "system": True,
        "target": "admin",
        "text": "\n".join(lines),
        "master_uid": master_uid,
        "instance_id": instance_id,
        "buttons": [
            {"text": "✅ Принять", "callback_data": f"join_approve:{master_uid}"},
            {"text": "❌ Отказать", "callback_data": f"join_decline:{master_uid}"},
        ],
    }
    connection.execute(
        """
        INSERT INTO api_outbox (ticket_id, message_id, channel, payload, status, created_at)
        VALUES ('', '', ?, ?, 'pending', ?)
        """,
        (ADMIN_NOTIFY_CHANNEL, json.dumps(payload, ensure_ascii=False), _now()),
    )


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
    404 — мастер ещё не проходил POST /session.
    403 — мастер ждёт подтверждения (instance_not_whitelisted) либо
    отключён администратором (forbidden).

    Белый список здесь не проверяется: он отсекает новых клиентов на
    POST /session, а уже активный мастер работает, даже если его id
    админ не вписал в список вручную.
    """
    instance_id = normalize_instance_id(x_instance_id)

    master = find_by_instance(instance_id)
    if master is None:
        raise errors.ApiError(
            404,
            "master_not_found",
            "Мастер не найден. Сначала выполните POST /v1/session.",
        )
    if not master.is_active:
        if not is_allowed(instance_id):
            logger.debug("Запрос от неподтверждённого instance_id %s", instance_id)
            raise errors.instance_not_whitelisted()
        raise errors.forbidden("Доступ мастера отключён администратором")

    touch(master.master_uid)
    return master


#: Зависимость для эндпоинтов, требующих мастера.
RequireMaster = Depends(current_master)
