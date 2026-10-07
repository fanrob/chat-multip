"""Загрузка, хранение и выдача вложений.

Файлы лежат на диске в каталоге (по умолчанию `attachments/` рядом с БД), в БД —
только метаданные. В саму SQLite картинки класть нельзя: она всё равно
считает их страницами по 4 КБ, и чтение через API каждый раз дёргало бы диск.

Имя файла на диске генерируем сами (id + расширение), а не берём из запроса:
иначе в путь попадёт `../` или имя в кириллице, которое сломает часть систем.
Оригинальное имя сохраняем в БД для показа пользователю.
"""

import logging
import mimetypes
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from fastapi import UploadFile

from api import errors
from api.auth import Master
from api.db import reading, transaction
from api.errors import ApiError
from api.schemas import ALLOWED_MIME_TYPES, MAX_ATTACHMENT_BYTES

logger = logging.getLogger(__name__)

#: Куда складывать файлы. В docker это внутри тома, поэтому путь настраивается.
STORAGE_ENV = "CHAT_MULTI_ATTACHMENTS"
DEFAULT_DIR = "attachments"

#: Расширения для случаев, когда браузер/клиент не прислал понятный mime-тип.
_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
}


def storage_dir() -> str:
    """Каталог хранения файлов; создаётся при первом обращении."""
    path = os.environ.get(STORAGE_ENV, DEFAULT_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def new_attachment_id() -> str:
    return "a_" + uuid.uuid4().hex[:12]


def _resolve_mime(filename: str, declared: Optional[str]) -> str:
    """Определяет mime-тип: сначала пришедший от клиента, потом по имени."""
    if declared and declared != "application/octet-stream":
        return declared.split(";")[0].strip().lower()
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _extension(mime: str, filename: str) -> str:
    if mime in _EXT_BY_MIME:
        return _EXT_BY_MIME[mime]
    _, ext = os.path.splitext(filename)
    # Оставляем только буквы/цифры длиной до 8 символов.
    cleaned = "".join(ch for ch in ext if ch.isalnum())[:8]
    return f".{cleaned}" if cleaned else ""


def _kind(mime: str) -> str:
    return "photo" if mime.startswith("image/") else "file"


def _check_mime(mime: str) -> str:
    """Пропускает только типы из белого списка README (раздел 6.5).

    Проверяем до записи на диск: иначе запрещённый файл уже окажется
    в хранилище, и придётся его отдельно подчищать.
    """
    if mime not in ALLOWED_MIME_TYPES:
        raise errors.unsupported_mime_type(mime)
    return mime


def _store_upload(upload: UploadFile, mime: str) -> Tuple[str, str, int]:
    """Дочитывает файл на диск, возвращает (attachment_id, path, size).

    Файл читается блоками, а не целиком: 20 МБ в памяти на каждый запрос
    при нескольких мастерах — лишний расход.
    """
    attachment_id = new_attachment_id()
    path = os.path.join(
        storage_dir(), f"{attachment_id}{_extension(mime, upload.filename or '')}"
    )

    total = 0
    with open(path, "wb") as target:
        while True:
            chunk = upload.file.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ATTACHMENT_BYTES:
                # Превышение — сразу удаляем огрызок, чтобы не копить мусор.
                target.close()
                os.remove(path)
                raise errors.payload_too_large(total, MAX_ATTACHMENT_BYTES)
            target.write(chunk)

    return attachment_id, path, total


def save(
    master: Master,
    upload: UploadFile,
    ticket_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Сохраняет файл и возвращает метаданные вложения.

    Файл не привязывается к сообщению: сначала мастер грузит вложение,
    потом отправляет сообщение с его id. Так при ошибочном сообщении
    вложение не пропадёт.
    """
    if not upload.filename:
        raise errors.validation_error("Файл не выбран", field="file")

    mime = _check_mime(_resolve_mime(upload.filename, upload.content_type))
    attachment_id, path, size = _store_upload(upload, mime)

    now = datetime.now().isoformat()
    try:
        with transaction() as connection:
            if ticket_id:
                exists = connection.execute(
                    "SELECT 1 FROM tickets WHERE id = ?", (ticket_id,)
                ).fetchone()
                if not exists:
                    raise errors.ticket_not_found(ticket_id)

            connection.execute(
                """
                INSERT INTO api_attachments
                    (id, ticket_id, message_id, source, channel, filename, mime_type,
                     size_bytes, storage_key, created_at, master_uid)
                VALUES (?, ?, NULL, 'master', 'telegram', ?, ?, ?, ?, ?, ?)
                """,
                (
                    attachment_id,
                    ticket_id,
                    os.path.basename(upload.filename)[:255],
                    mime,
                    size,
                    path,
                    now,
                    master.master_uid,
                ),
            )
    except ApiError:
        # Заявки нет или транзакция откатилась — файл на диске лишний.
        _discard_file(path)
        raise

    logger.info(
        "Мастер %s загрузил %s (%s, %d байт)", master.master_uid, attachment_id, mime, size
    )

    return _to_meta(
        {
            "id": attachment_id,
            "filename": os.path.basename(upload.filename)[:255],
            "mime_type": mime,
            "size_bytes": size,
            "source": "master",
            "created_at": now,
        }
    )


def _discard_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        logger.warning("Не удалось удалить временный файл %s", path)


def get_meta(attachment_id: str) -> Optional[Dict[str, Any]]:
    with reading() as connection:
        row = connection.execute(
            "SELECT * FROM api_attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
    return _to_meta(row) if row else None


def _to_meta(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "attachment_id": row["id"],
        "kind": _kind(row["mime_type"] or ""),
        "filename": row["filename"] or "",
        "mime_type": row["mime_type"] or "",
        "size": row["size_bytes"] or 0,
        "source": row["source"],
        "created_at": row["created_at"],
    }


def can_access(master_uid: str, row: sqlite3.Row) -> bool:
    """Может ли мастер получить файл.

    Правило: файл виден участникам заявки. Для заявок в общей ленте
    (status=new) — всем активным мастерам, иначе второй мастер не смог бы
    посмотреть фото до принятия заявки.

    Файл, загруженный, но ещё не привязанный к заявке, виден только
    загрузившему его мастеру.
    """
    from api.store import is_member, is_visible

    with reading() as connection:
        if not row["ticket_id"]:
            # Заявки ещё нет: файл принадлежит загрузившему.
            return bool(row["master_uid"]) and row["master_uid"] == master_uid
        if is_member(connection, row["ticket_id"], master_uid):
            return True
        return is_visible(connection, row["ticket_id"], master_uid)


def resolve_for_download(master_uid: str, attachment_id: str) -> Dict[str, Any]:
    """Отдаёт путь к файлу и имя для Content-Disposition.

    Если файл записан в БД, но на диске его нет (перенесли БД без папки),
    сообщаем 404, а не 500: с точки зрения клиента вложения просто нет.
    """
    with reading() as connection:
        row = connection.execute(
            "SELECT * FROM api_attachments WHERE id = ?", (attachment_id,)
        ).fetchone()

    if not row:
        raise errors.attachment_not_found(attachment_id)

    if not can_access(master_uid, row):
        # Не отличаем «нет доступа» от «нет файла», чтобы по коду ответа
        # нельзя было перебором узнать о чужих вложениях.
        raise errors.attachment_not_found(attachment_id)

    path = row["storage_key"]
    if not path or not os.path.isfile(path):
        logger.error("Файл вложения %s отсутствует на диске: %s", attachment_id, path)
        raise errors.attachment_not_found(attachment_id)

    return {
        "path": path,
        "filename": row["filename"] or os.path.basename(path),
        "mime_type": row["mime_type"] or "application/octet-stream",
        "size": row["size_bytes"] or 0,
    }


def resolve_for_delivery(attachment_id: str) -> Dict[str, Any]:
    """Путь к файлу для отправки в мессенджер, без проверки прав мастера.

    Отдельная функция вместо флага в resolve_for_download: право скачать файл
    клиенту и право отправить его в Telegram — разные вещи. Этот путь доступен
    только процессу бота, который и так работает с чужими заявками.

    Вложение достаётся по id из строки api_attachments, привязанной к сообщению
    в очереди: сообщение уже отправлено клиенту, значит файл существует.
    """
    with reading() as connection:
        row = connection.execute(
            "SELECT * FROM api_attachments WHERE id = ?", (attachment_id,)
        ).fetchone()

    if not row:
        raise errors.attachment_not_found(attachment_id)

    path = row["storage_key"]
    if not path or not os.path.isfile(path):
        logger.error("Файл вложения %s отсутствует на диске: %s", attachment_id, path)
        raise errors.attachment_not_found(attachment_id)

    return {
        "path": path,
        "filename": row["filename"] or os.path.basename(path),
        "mime_type": row["mime_type"] or "application/octet-stream",
        "size": row["size_bytes"] or 0,
    }


def delete(attachment_id: str) -> bool:
    """Удаляет вложение: строку в БД и файл с диска."""
    with transaction() as connection:
        row = connection.execute(
            "SELECT storage_key FROM api_attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
        if not row:
            return False
        connection.execute("DELETE FROM api_attachments WHERE id = ?", (attachment_id,))

    path = row["storage_key"]
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            logger.warning("Не удалось удалить файл %s", path)
    return True
