"""Единый формат ошибок и коды из README (раздел 7).

Клиент разбирает { "error": { code, message, details } } и по коду решает,
что показать пользователю: «заявку уже взял Фёдор» при already_accepted,
полный ресинк при cursor_expired и так далее. Поэтому коды — это контракт,
их нельзя переименовывать без изменения клиента.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

#: Константа 422 вместо HTTP_422_UNPROCESSABLE: в новых
#: версиях starlette это имя deprecated, а числовой код стабилен.
HTTP_422_UNPROCESSABLE = 422


class ApiError(StarletteHTTPException):
    """Ошибка с кодом из контракта.

    Наследуемся от HTTPException, чтобы работали и наши обработчики,
    и стандартные проверки FastAPI (Depends, роутинг).
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.message = message
        self.details = details or {}


# ==================== Конструкторы ошибок ====================


def validation_error(message: str = "Некорректные данные", **details: Any) -> ApiError:
    return ApiError(HTTP_422_UNPROCESSABLE, "validation_error", message, details)


def unauthorized(message: str = "Нет заголовка X-Instance-Id") -> ApiError:
    return ApiError(status.HTTP_401_UNAUTHORIZED, "unauthorized", message)


def instance_not_whitelisted() -> ApiError:
    return ApiError(
        status.HTTP_403_FORBIDDEN,
        "instance_not_whitelisted",
        "Экземпляр не добавлен в белый список. Обратитесь к администратору.",
    )


def forbidden(message: str = "Действие не разрешено", **details: Any) -> ApiError:
    return ApiError(status.HTTP_403_FORBIDDEN, "forbidden", message, details)


def ticket_not_found(ticket_id: str) -> ApiError:
    return ApiError(
        status.HTTP_404_NOT_FOUND,
        "ticket_not_found",
        "Заявка не найдена",
        {"ticket_id": ticket_id},
    )


def master_not_found(master_id: str) -> ApiError:
    return ApiError(
        status.HTTP_404_NOT_FOUND,
        "master_not_found",
        "Мастер не найден",
        {"master_id": master_id},
    )


def attachment_not_found(attachment_id: str) -> ApiError:
    return ApiError(
        status.HTTP_404_NOT_FOUND,
        "attachment_not_found",
        "Вложение не найдено",
        {"attachment_id": attachment_id},
    )


def not_ticket_member(ticket_id: str) -> ApiError:
    return ApiError(
        status.HTTP_403_FORBIDDEN,
        "not_ticket_member",
        "Вы не участник этой заявки",
        {"ticket_id": ticket_id},
    )


def ticket_closed(ticket_id: str) -> ApiError:
    return ApiError(
        status.HTTP_403_FORBIDDEN,
        "ticket_closed",
        "Заявка закрыта",
        {"ticket_id": ticket_id},
    )


def already_accepted(ticket_id: str, owner_master_id: str, owner_name: str) -> ApiError:
    """409 с данными текущего владельца: клиент покажет, кто взял заявку."""
    return ApiError(
        status.HTTP_409_CONFLICT,
        "already_accepted",
        f"Заявку уже взял {owner_name}",
        {
            "ticket_id": ticket_id,
            "owner_master_id": owner_master_id,
            "owner_name": owner_name,
        },
    )


def already_member(ticket_id: str, master_id: str) -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "already_member",
        "Мастер уже подключён к заявке",
        {"ticket_id": ticket_id, "master_id": master_id},
    )


def ticket_not_in_feed(ticket_id: str) -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "ticket_not_in_feed",
        "Заявка больше не доступна",
        {"ticket_id": ticket_id},
    )


def workshop_mismatch(ticket_id: str, expected: str, actual: str) -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "workshop_mismatch",
        "Цех подключаемого мастера не совпадает с цехом заявки",
        {"ticket_id": ticket_id, "ticket_workshop_id": expected, "master_workshop_id": actual},
    )


def cursor_expired(min_available_cursor: int) -> ApiError:
    """410: события вытеснены, клиент обязан сделать полный ресинк."""
    return ApiError(
        status.HTTP_410_GONE,
        "cursor_expired",
        "Курсор устарел, нужен полный ресинк",
        {"min_available_cursor": min_available_cursor},
    )


def payload_too_large(size: int, limit: int) -> ApiError:
    return ApiError(
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        "payload_too_large",
        f"Файл больше {limit // (1024 * 1024)} МБ",
        {"size": size, "limit": limit},
    )


def unsupported_mime_type(mime: str) -> ApiError:
    return ApiError(
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        "unsupported_mime_type",
        f"Тип файла не поддерживается: {mime}",
        {"mime_type": mime},
    )


def attachment_missing(message_id: str, attachment_id: str) -> ApiError:
    return ApiError(
        HTTP_422_UNPROCESSABLE,
        "validation_error",
        "Вложение не найдено, принадлежит другой заявке или загружено другим мастером",
        {"message_id": message_id, "attachment_id": attachment_id},
    )


def server_error(message: str = "Внутренняя ошибка сервера") -> ApiError:
    return ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, "server_error", message)


# ==================== Обработчики ====================


def _envelope(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


#: Коды, которые auth.py уже формирует своими HTTPException — маппим на контракт.
_CODE_BY_STATUS = {
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "instance_not_whitelisted",
    status.HTTP_404_NOT_FOUND: "ticket_not_found",
    status.HTTP_410_GONE: "cursor_expired",
}


def install(app: FastAPI) -> None:
    """Вешает обработчики, чтобы все не-2xx выглядели одинаково."""

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.details),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic-ошибки verbose; клиенту достаточно списка полей.
        fields = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())),
                "message": error.get("msg", ""),
            }
            for error in exc.errors()
        ]
        first = fields[0]["message"] if fields else "Некорректные данные"
        return JSONResponse(
            status_code=HTTP_422_UNPROCESSABLE,
            content=_envelope("validation_error", first, {"fields": fields}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _CODE_BY_STATUS.get(exc.status_code)
        if code is None:
            if exc.status_code == status.HTTP_405_METHOD_NOT_ALLOWED:
                code = "not_found"
            elif exc.status_code >= 500:
                code = "server_error"
            else:
                code = "http_error"
        detail = exc.detail if isinstance(exc.detail, str) else "Ошибка запроса"
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(code, detail),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Логируем с трейсбеком, клиенту отдаём нейтральный текст:
        # детали БД клиенту не нужны.
        logger.exception("Необработанная ошибка: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("server_error", "Внутренняя ошибка сервера"),
        )
