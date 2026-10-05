"""Telegram-бот мессенджера: приём заявок из Telegram и доставка ответов.

Отдельный процесс от HTTP API (папка api) и от почтового бота (папка mail_bot),
который занимается почтой. Основная точка входа — main.py.

Общий с API и почтой код лежит в корне src: config.py (таблица settings, путь к БД),
storage.py, models.py, bridge.py, outbox_queue.py.
"""
