"""Pydantic-модели запросов и ответов API.

Имена полей и формат — как в разделе 6 README клиента (`E:\\proj\\chat_multi_cli\\README.md`),
чтобы клиент и сервер не разошлись в деталях.

Все id — строки с префиксами (`m_`, `t_`, `msg_`, `a_`) и никогда не меняются:
они попадают в URL и в локальный кэш клиента.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

#: Префиксы идентификаторов — единые для всего API.
MASTER_PREFIX = "m_"
TICKET_PREFIX = "t_"
MESSAGE_PREFIX = "msg_"
ATTACHMENT_PREFIX = "a_"
WORKSHOP_PREFIX = "w_"

#: Ограничение на размер одного вложения, байт.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

#: Разрешённые mime-типы вложений.
ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "application/pdf",
    "text/plain",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/zip",
    "application/x-zip-compressed",
}


# ==================== Общие ====================


class ApiModel(BaseModel):
    """Базовая модель: запрещает неизвестные поля.

    Это защита от опечаток в клиенте: вместо тихого игнорирования поля
    приходит 422 с понятным сообщением.
    """

    model_config = {"extra": "forbid"}


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(ApiModel):
    """Единый конверт ошибки (README, раздел 7)."""

    error: ErrorBody


class Workshop(ApiModel):
    id: str
    name: str
    masters_count: int = 0


class Profile(ApiModel):
    """Профиль мастера. instance_id наружу не отдаётся."""

    id: str
    full_name: str
    workshop_id: Optional[str] = None
    workshop_name: Optional[str] = None
    is_active: bool = True
    online: bool = False


class MasterBrief(ApiModel):
    id: str
    full_name: str
    workshop_id: Optional[str] = None
    workshop_name: Optional[str] = None
    online: bool = False
    active_tickets: int = 0
    role: Optional[str] = None


class ClientBrief(ApiModel):
    id: Optional[str] = None
    display_name: str
    phone: Optional[str] = None


class AttachmentBrief(ApiModel):
    attachment_id: str
    kind: str = "file"
    filename: str = ""
    mime_type: str = ""
    size: int = 0
    source: str = "master"
    created_at: Optional[str] = None


class Attachment(AttachmentBrief):
    """Полная карточка вложения (ответ POST /attachments)."""

    width: Optional[int] = None
    height: Optional[int] = None


class Message(ApiModel):
    id: str
    seq: int
    ticket_id: str
    sender: str = "client"
    sender_master_id: Optional[str] = None
    sender_name: str = ""
    text: str = ""
    attachments: List[AttachmentBrief] = Field(default_factory=list)
    created_at: str
    delivery: str = "queued"
    read_at: Optional[str] = None


class Member(ApiModel):
    master_id: str
    full_name: str = ""
    role: str = "collaborator"
    joined_at: str


class TicketBrief(ApiModel):
    """Компактное представление заявки — для ленты и событий."""

    id: str
    status: str
    workshop_id: Optional[str] = None
    workshop_name: Optional[str] = None
    subject: str = ""
    created_at: str
    updated_at: Optional[str] = None
    client: ClientBrief
    owner: Optional[MasterBrief] = None
    members: List[Member] = Field(default_factory=list)
    last_message: Optional["LastMessage"] = None
    unread_count: int = 0


class LastMessage(ApiModel):
    seq: int
    sender: str
    preview: str = ""


class Ticket(TicketBrief):
    """Полная карточка заявки."""

    text: str = ""
    source: Optional[str] = None
    closed_at: Optional[str] = None
    close_reason: Optional[str] = None


TicketBrief.model_rebuild()


class Event(ApiModel):
    seq: int
    type: str
    ts: str
    ticket_id: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


# ==================== Запросы ====================


class SessionCreate(ApiModel):
    """POST /v1/session — первый запрос, без заголовка X-Instance-Id."""

    instance_id: str = Field(description="UUID, сгенерированный приложением")
    full_name: str = Field(min_length=1, max_length=200)
    workshop_id: Optional[str] = None
    client_version: Optional[str] = None
    platform: Optional[str] = None

    @field_validator("full_name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("ФИО не может быть пустым")
        return cleaned

    @field_validator("workshop_id")
    @classmethod
    def workshop_known_prefix(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.startswith(WORKSHOP_PREFIX):
            raise ValueError(f"workshop_id должен начинаться с {WORKSHOP_PREFIX!r}")
        return value


class ProfilePatch(ApiModel):
    """PATCH /v1/session/profile."""

    full_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    workshop_id: Optional[str] = None


class AttachmentRef(ApiModel):
    attachment_id: str


class MessageCreate(ApiModel):
    """POST /v1/messages."""

    ticket_id: str
    client_msg_id: str = Field(description="UUID клиента — ключ идемпотентности")
    text: str = Field(default="", max_length=4000)
    attachments: List[AttachmentRef] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    def validate_payload(self) -> None:
        """Проверяет, что есть что отправлять. Вызывается вручную,
        чтобы вернуть 422 с кодом validation_error."""
        if not self.text and not self.attachments:
            raise ValueError("Нужен текст или хотя бы одно вложение")


class CloseRequest(ApiModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class DeclineRequest(ApiModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class ReleaseRequest(ApiModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class MemberAddRequest(ApiModel):
    master_id: str


class ReadRequest(ApiModel):
    """POST /tickets/{id}/read — отметить прочитанным до seq."""

    seq: Optional[int] = Field(default=None, ge=0)


# ==================== Ответы ====================


class SessionResponse(ApiModel):
    master_id: str
    profile: Profile
    token: Optional[str] = None
    cursor: int = 0
    server_time: str
    telegram: Dict[str, Any] = Field(default_factory=dict)


class WorkshopsResponse(ApiModel):
    workshops: List[Workshop] = Field(default_factory=list)


class MastersResponse(ApiModel):
    masters: List[MasterBrief] = Field(default_factory=list)


class MasterResponse(ApiModel):
    master: MasterBrief


class TicketsResponse(ApiModel):
    tickets: List[TicketBrief] = Field(default_factory=list)
    next_before: Optional[str] = None


class TicketResponse(ApiModel):
    ticket: Ticket


class MessagesResponse(ApiModel):
    messages: List[Message] = Field(default_factory=list)
    has_more_before: bool = False
    has_more_after: bool = False


class MessageResponse(ApiModel):
    message: Message


class SyncResponse(ApiModel):
    events: List[Event] = Field(default_factory=list)
    cursor: int = 0
    has_more: bool = False


class MemberResponse(ApiModel):
    member: Member


class AttachmentResponse(ApiModel):
    attachment: Attachment


class OkResponse(ApiModel):
    ok: bool = True
    ticket: Optional[Ticket] = None
