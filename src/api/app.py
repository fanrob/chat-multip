"""Точка входа API. Отдельный процесс, Telegram не требуется.

Запуск:
    python src/api/app.py
    (или из каталога src: python -m api.app)

Порт по умолчанию 21000, переопределяется переменной CHAT_MULTI_API_PORT.

Почему отдельный процесс, а не модуль внутри бота:
    1. telegram-бот работает на asyncio и блокирующем run_polling(), его нельзя
       аккуратно разделить с uvicorn в одном процессе;
    2. API должен переживать падение Telegram: бот завис на сети — мастера
       всё равно видят заявки;
    3. позже появится отдельный Viber-бот — три процесса, одна БД, общая
       схема событий.
"""

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Позволяет запускать файл напрямую (`python src/api/app.py`), когда
# каталог src не в PYTHONPATH.
_SRC_ROOT = Path(__file__).resolve().parent.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from api import errors, schema  # noqa: E402
from api.db import db_path, ensure_wal  # noqa: E402
from api.routes import router  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_PORT = 21000
DEFAULT_HOST = "0.0.0.0"

API_TITLE = "Chat Multi API"
API_VERSION = "1.0.0"

DESCRIPTION = """
HTTP-доступ к заявкам для приложения мастера.

Идентификация — заголовок `X-Instance-Id` с UUID, который приложение
генерирует при первом запуске. Обмен курсором: `GET /v1/sync?cursor=N`
возвращает события с seq больше N.
"""


def _configure_logging() -> None:
    level = os.environ.get("CHAT_MULTI_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _port() -> int:
    raw = os.environ.get("CHAT_MULTI_API_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        logger.warning("CHAT_MULTI_API_PORT=%r не число, беру %d", raw, DEFAULT_PORT)
        return DEFAULT_PORT


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Подготовка БД до первого запроса и проверка при старте.

    Миграции применяются здесь, а не в импортах: если БД недоступна,
    процесс должен упасть сразу и явно, а не падать на первом же
    обращении клиента.
    """
    target = db_path()
    logger.info("БД: %s", target)

    ensure_wal(target)
    schema.apply(target)

    logger.info(
        "API v%s готов: %s:%d", API_VERSION, os.environ.get("CHAT_MULTI_API_HOST", DEFAULT_HOST),
        _port(),
    )
    yield
    logger.info("API остановлен")


def create_app() -> FastAPI:
    """Собирает приложение. Отдельная функция — чтобы тесты поднимали
    приложение с временной БД, не запуская процесс."""
    _configure_logging()

    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
    )

    # Клиент — нативное десктопное приложение (httpx), а не браузер,
    # поэтому CORS формально не нужен. Оставлен на случай отладочной
    # веб-страницы и потому, что без него любой браузерный тест падает.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    errors.install(app)
    app.include_router(router, prefix="/v1")

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {
            "service": API_TITLE,
            "version": API_VERSION,
            "docs": "/docs",
            "health": "/v1/health",
        }

    return app


app = create_app()


def main() -> None:
    """Запуск uvicorn.

    Один worker намеренно: SQLite плохо переносит несколько писателей, а
    при нашей нагрузке (десятки запросов в минуту) один процесс с запасом.
    Горизонтальное масштабирование — отдельная задача, требующая Postgres.
    """
    import uvicorn

    host = os.environ.get("CHAT_MULTI_API_HOST", DEFAULT_HOST)
    uvicorn.run(
        "api.app:app",
        host=host,
        port=_port(),
        workers=1,
        log_level=os.environ.get("CHAT_MULTI_LOG_LEVEL", "info").lower(),
        # В контейнере за uvicorn стоит nginx/traefik или проброс порта,
        # поэтому доверяем заголовкам о схеме для корректных ссылок в openapi.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
