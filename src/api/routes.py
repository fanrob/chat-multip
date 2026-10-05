"""HTTP-роуты API.

Почему обработчики `def`, а не `async def`: sqlite3 синхронный. Если объявить
обработчик async, FastAPI выполнит его прямо в event loop, и любой запрос,
застрявший на блокировке БД, остановит весь сервис. С `def` FastAPI отдаёт
функцию в пул потоков, где блокировка затрагивает только один поток.

Все маршруты под /v1, как в контракте README клиента.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, Response

from api import attachments as attachments_service
from api import errors, events, store
from api.auth import Master, current_master
from api.db import reading
from api.schemas import (
    AttachmentResponse,
    CloseRequest,
    DeclineRequest,
    MasterResponse,
    MastersResponse,
    MemberAddRequest,
    MemberResponse,
    MessageCreate,
    MessageResponse,
    MessagesResponse,
    OkResponse,
    ProfilePatch,
    ReadRequest,
    ReleaseRequest,
    SessionCreate,
    SessionResponse,
    SyncResponse,
    TicketResponse,
    TicketsResponse,
    WorkshopsResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _server_time() -> str:
    """Время в UTC с суффиксом Z — как требует раздел 8 README.

    timezone-aware, а не utcnow(): наивный utcnow() помечен как устаревший
    и в будущей версии Python будет удалён.
    """
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def _profile(master: Master) -> Dict[str, Any]:
    from api.auth import workshop_name

    return {
        "id": master.master_uid,
        "full_name": master.full_name,
        "workshop_id": store.workshop_id_to_api(master.workshop_id),
        "workshop_name": workshop_name(master.workshop_id),
        "is_active": master.is_active,
        "online": True,
    }


def _bot_username() -> Dict[str, Any]:
    """Имя бота для клиента. Не критично, если не настроено."""
    try:
        from config import BOT_TOKEN

        if not BOT_TOKEN:
            return {}
        # Telegram отдаёт username только запросом к сети; вместо этого
        # отдаём статичное поле из settings, если администратор его заполнил.
        with reading() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = 'bot_username'"
            ).fetchone()
        return {"bot_username": row["value"]} if row and row["value"] else {}
    except Exception:  # noqa: BLE001
        return {}


# ==================== Здоровье ====================


@router.get("/health", tags=["service"])
def health() -> Dict[str, Any]:
    """Проверка живости. Без авторизации: docker и балансировщик должны
    видеть ответ без заголовков."""
    data = events.stats()
    data["status"] = "ok"
    data["server_time"] = _server_time()
    return data


# ==================== Сессия ====================


@router.post("/session", response_model=SessionResponse, tags=["session"])
def create_session(payload: SessionCreate) -> SessionResponse:
    """Регистрация/вход экземпляра клиента.

    Первый запрос, поэтому instance_id приходит в теле, а не в заголовке.
    Идемпотентно: повторный вызов с тем же instance_id не создаёт второго
    мастера, а обновляет профиль.
    """
    from api.auth import get_or_create, is_allowed, normalize_instance_id

    instance_id = normalize_instance_id(payload.instance_id)

    if not is_allowed(instance_id):
        logger.warning("POST /session отклонён: %s не в белом списке", instance_id)
        raise errors.instance_not_whitelisted()

    workshop_db = store.workshop_id_to_db(payload.workshop_id)
    if workshop_db is not None:
        with reading() as connection:
            exists = connection.execute(
                "SELECT 1 FROM workshops WHERE id = ?", (workshop_db,)
            ).fetchone()
        if not exists:
            raise errors.validation_error(
                "Цех не найден", workshop_id=payload.workshop_id
            )

    master = get_or_create(instance_id, payload.full_name, workshop_db)

    return SessionResponse(
        master_id=master.master_uid,
        profile=_profile(master),
        token=None,
        cursor=events.current_cursor(master.master_uid),
        server_time=_server_time(),
        telegram=_bot_username(),
    )


@router.get("/session", response_model=SessionResponse, tags=["session"])
def read_session(master: Master = Depends(current_master)) -> SessionResponse:
    """Текущий профиль и актуальный курсор."""
    return SessionResponse(
        master_id=master.master_uid,
        profile=_profile(master),
        token=None,
        cursor=events.current_cursor(master.master_uid),
        server_time=_server_time(),
        telegram=_bot_username(),
    )


@router.patch("/session/profile", response_model=SessionResponse, tags=["session"])
def patch_profile(
    payload: ProfilePatch,
    master: Master = Depends(current_master),
) -> SessionResponse:
    """Смена ФИО или цеха.

    Событие masters.directory_changed рассылает сам store в той же транзакции:
    у клиентов в кэше лежит справочник мастеров, и без события они показывали
    бы старое имя.
    """
    data = store.update_profile(master.master_uid, payload.full_name, payload.workshop_id)

    refreshed = Master(
        master_uid=master.master_uid,
        instance_id=master.instance_id,
        full_name=data["full_name"],
        workshop_id=store.workshop_id_to_db(data["workshop_id"]),
        is_active=master.is_active,
        created_at=master.created_at,
    )

    return SessionResponse(
        master_id=master.master_uid,
        profile=_profile(refreshed),
        token=None,
        cursor=events.current_cursor(master.master_uid),
        server_time=_server_time(),
        telegram=_bot_username(),
    )


# ==================== Справочники ====================


@router.get("/workshops", response_model=WorkshopsResponse, tags=["directory"])
def list_workshops(master: Master = Depends(current_master)) -> WorkshopsResponse:
    """Список цехов — для выбора при первом запуске."""
    return WorkshopsResponse(workshops=store.list_workshops())


@router.get("/masters", response_model=MastersResponse, tags=["directory"])
def list_masters(
    query: Optional[str] = Query(default=None, max_length=100),
    workshop_id: Optional[str] = Query(default=None),
    include_self: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    master: Master = Depends(current_master),
) -> MastersResponse:
    """Справочник мастеров для подключения второго участника."""
    rows = store.list_masters(
        query=query,
        workshop_id=workshop_id,
        include_self=include_self,
        limit=limit,
    )
    if not include_self:
        rows = [row for row in rows if row["id"] != master.master_uid]
    return MastersResponse(masters=rows)


@router.get("/masters/{master_id}", response_model=MasterResponse, tags=["directory"])
def read_master(
    master_id: str,
    master: Master = Depends(current_master),
) -> MasterResponse:
    """Карточка мастера."""
    brief = store.get_master_brief(master_id)
    if not brief:
        raise errors.master_not_found(master_id)
    return MasterResponse(master=brief)


# ==================== Синхронизация ====================


@router.get(
    "/sync",
    response_model=SyncResponse,
    tags=["sync"],
    responses={
        204: {"description": "Событий не появилось за время ожидания"},
        410: {"description": "Курсор старше доступных событий — нужен ресинк"},
    },
)
def sync(
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=events.MAX_LIMIT),
    wait: int = Query(
        default=events.DEFAULT_WAIT_SECONDS,
        ge=0,
        le=events.MAX_WAIT_SECONDS,
    ),
    ticket_id: Optional[str] = Query(default=None),
    master: Master = Depends(current_master),
) -> Any:
    """События после курсора, с ожиданием.

    Long-poll: если событий нет, соединение держится до `wait` секунд и
    отдаёт событие сразу, как только оно появилось. Так клиент не тратит
    запросы на пустые опросы и видит события за секунды, а не за цикл опроса.

    `wait=0` — обычный опрос без ожидания, ответ сразу (пустой список, не 204).

    Обработчик остаётся `def`: ожидание занимает поток пула, а не event loop
    (см. докстринг модуля). Один висящий клиент держит один поток; при 5–7
    клиентах пул из 40 потоков даже не замечается.

    Если курсор «из прошлого» (события вытеснены чисткой или база восстановлена
    из бэкапа), отвечаем 410 cursor_expired: клиент обязан сбросить курсор и
    перекачать ленту, иначе он будет вечно догонять недоступные события.
    """
    safe_cursor = events.validate_cursor(master.master_uid, cursor)
    oldest = events.min_available_cursor()
    if safe_cursor < oldest:
        raise errors.cursor_expired(oldest)

    if wait == 0:
        return SyncResponse(
            **events.fetch(master.master_uid, safe_cursor, limit, ticket_id)
        )

    result = events.fetch_with_wait(
        master.master_uid, safe_cursor, limit, ticket_id, wait
    )
    if not result["events"]:
        # Ждали, событий не было: пустое тело вместо JSON с пустым списком.
        return Response(status_code=204)
    return SyncResponse(**result)


# ==================== Заявки ====================


@router.get("/tickets", response_model=TicketsResponse, tags=["tickets"])
def read_tickets(
    scope: str = Query(default="feed", pattern="^(feed|mine|all)$"),
    status: Optional[str] = Query(default=None, pattern="^(new|in_progress|closed)$"),
    workshop_id: Optional[str] = Query(default=None),
    query: Optional[str] = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    before: Optional[str] = Query(default=None),
    master: Master = Depends(current_master),
) -> TicketsResponse:
    """Список заявок: лента / мои / все."""
    return TicketsResponse(
        **store.list_tickets(
            master_uid=master.master_uid,
            scope=scope,
            status_filter=status,
            workshop_id=workshop_id,
            query=query,
            limit=limit,
            before=before,
        )
    )


@router.get("/tickets/{ticket_id}", response_model=TicketResponse, tags=["tickets"])
def read_ticket(
    ticket_id: str,
    master: Master = Depends(current_master),
) -> TicketResponse:
    """Карточка заявки с участниками и последним сообщением."""
    return TicketResponse(ticket=store.get_ticket(master.master_uid, ticket_id))


@router.post("/tickets/{ticket_id}/accept", response_model=TicketResponse, tags=["tickets"])
def accept_ticket(
    ticket_id: str,
    master: Master = Depends(current_master),
) -> TicketResponse:
    """Принять заявку. Атомарно: выигрывает тот, кто первый."""
    return TicketResponse(ticket=store.accept_ticket(master, ticket_id))


@router.post("/tickets/{ticket_id}/release", response_model=TicketResponse, tags=["tickets"])
def release_ticket(
    ticket_id: str,
    payload: Optional[ReleaseRequest] = None,
    master: Master = Depends(current_master),
) -> TicketResponse:
    """Вернуть заявку в общую ленту."""
    reason = payload.reason if payload else None
    return TicketResponse(ticket=store.release_ticket(master, ticket_id, reason))


@router.post("/tickets/{ticket_id}/close", response_model=TicketResponse, tags=["tickets"])
def close_ticket(
    ticket_id: str,
    payload: Optional[CloseRequest] = None,
    master: Master = Depends(current_master),
) -> TicketResponse:
    """Закрыть заявку."""
    reason = payload.reason if payload else None
    return TicketResponse(ticket=store.close_ticket(master, ticket_id, reason))


@router.post("/tickets/{ticket_id}/reopen", response_model=TicketResponse, tags=["tickets"])
def reopen_ticket(
    ticket_id: str,
    master: Master = Depends(current_master),
) -> TicketResponse:
    """Переоткрыть закрытую заявку."""
    return TicketResponse(ticket=store.reopen_ticket(master, ticket_id))


@router.post("/tickets/{ticket_id}/decline", response_model=OkResponse, tags=["tickets"])
def decline_ticket(
    ticket_id: str,
    payload: Optional[DeclineRequest] = None,
    master: Master = Depends(current_master),
) -> OkResponse:
    """Скрыть заявку из своей ленты. Идемпотентно."""
    store.decline_ticket(master, ticket_id, payload.reason if payload else None)
    # Событие не рассылаем: остальным мастерам ничего не изменилось.
    return OkResponse(ok=True)


@router.post("/tickets/{ticket_id}/read", response_model=OkResponse, tags=["tickets"])
def read_ticket_messages(
    ticket_id: str,
    payload: Optional[ReadRequest] = None,
    master: Master = Depends(current_master),
) -> OkResponse:
    """Отметить прочитанным. Идемпотентно."""
    store.mark_read(master.master_uid, ticket_id, payload.seq if payload else None)
    return OkResponse(ok=True)


# ==================== Участники ====================


@router.post("/tickets/{ticket_id}/members", response_model=MemberResponse, tags=["members"])
def add_member(
    ticket_id: str,
    payload: MemberAddRequest,
    master: Master = Depends(current_master),
) -> MemberResponse:
    """Подключить второго мастера к заявке."""
    return MemberResponse(member=store.add_member(master, ticket_id, payload.master_id))


@router.delete(
    "/tickets/{ticket_id}/members/{master_id}", response_model=OkResponse, tags=["members"]
)
def remove_member(
    ticket_id: str,
    master_id: str,
    master: Master = Depends(current_master),
) -> OkResponse:
    """Отключить мастера от заявки."""
    store.remove_member(master, ticket_id, master_id)
    return OkResponse(ok=True)


# ==================== Сообщения ====================


@router.get(
    "/tickets/{ticket_id}/messages", response_model=MessagesResponse, tags=["messages"]
)
def read_messages(
    ticket_id: str,
    after_seq: Optional[int] = Query(default=None, ge=0),
    before_seq: Optional[int] = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    master: Master = Depends(current_master),
) -> MessagesResponse:
    """История сообщений по заявке."""
    return MessagesResponse(
        **store.list_messages(master.master_uid, ticket_id, after_seq, before_seq, limit)
    )


@router.post(
    "/messages", response_model=MessageResponse, status_code=202, tags=["messages"]
)
def send_message(
    payload: MessageCreate,
    master: Master = Depends(current_master),
) -> MessageResponse:
    """Отправить сообщение клиенту.

    202 Accepted, а не 200: сообщение принято в очередь, но ещё не ушло
    в Telegram. Клиент покажет статус «в очереди» и получит обновление
    через событие message.updated, когда бот отправит.
    """
    try:
        payload.validate_payload()
    except ValueError as exc:
        raise errors.validation_error(str(exc), ticket_id=payload.ticket_id) from exc

    message = store.create_message(
        master=master,
        ticket_id=payload.ticket_id,
        client_msg_id=payload.client_msg_id,
        text=payload.text,
        attachment_ids=[ref.attachment_id for ref in payload.attachments],
    )
    return MessageResponse(message=message)


# ==================== Вложения ====================


@router.post(
    "/attachments",
    response_model=AttachmentResponse,
    status_code=201,
    tags=["attachments"],
)
def upload_attachment(
    file: UploadFile = File(description="Файл для отправки клиенту"),
    ticket_id: Optional[str] = Form(default=None),
    master: Master = Depends(current_master),
) -> AttachmentResponse:
    """Загрузить файл от мастера.

    Отдельным шагом перед сообщением: если текст не отправится,
    вложение останется и мастер сможет использовать его позже.

    Событие здесь не шлём: в контракте (README 6.2) типа события для
    вложения нет, а id возвращается в теле ответа — клиент сам передаст
    его в `POST /messages`. Событие `message.created` с вложением вместо
    сообщения клиент бы не понял.
    """
    return AttachmentResponse(attachment=attachments_service.save(master, file, ticket_id))


@router.get("/attachments/{attachment_id}/meta", tags=["attachments"])
def read_attachment_meta(
    attachment_id: str,
    master: Master = Depends(current_master),
) -> Dict[str, Any]:
    """Метаданные вложения без скачивания."""
    resolved = attachments_service.resolve_for_download(master.master_uid, attachment_id)
    meta = attachments_service.get_meta(attachment_id) or {}
    return {
        "attachment_id": attachment_id,
        "filename": resolved["filename"],
        "mime_type": resolved["mime_type"],
        "size": resolved["size"],
        "source": meta.get("source", "master"),
        "created_at": meta.get("created_at"),
    }


@router.get("/attachments/{attachment_id}", tags=["attachments"])
def download_attachment(
    attachment_id: str,
    master: Master = Depends(current_master),
) -> FileResponse:
    """Скачать вложение (в том числе фото, присланные клиентом)."""
    resolved = attachments_service.resolve_for_download(master.master_uid, attachment_id)

    # Content-Disposition с ASCII-заглушкой: русские имена в HTTP-заголовке
    # недопустимы, поэтому кодируем UTF-8 по RFC 5987, а fallback оставляем
    # латиницей — иначе некоторые клиенты покажут кракозябры.
    name = resolved["filename"]
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "_")
    from urllib.parse import quote

    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(name)}"
    )

    return FileResponse(
        path=resolved["path"],
        media_type=resolved["mime_type"],
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(resolved["size"]),
        },
    )
