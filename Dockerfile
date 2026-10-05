FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Europe/Moscow

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

RUN mkdir -p /data \
    && useradd -m -u 1001 appuser \
    && chown -R appuser:appuser /app /data

WORKDIR /data
USER appuser

# Точка входа чат-бота. Второй процесс из того же образа — почтовый бот:
#   docker run ... /app/src/mail_bot/main.py
CMD ["python", "/app/src/telegram_bot/main.py"]
