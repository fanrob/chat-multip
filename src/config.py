import sqlite3
import logging
import os

# ==================== НАСТРОЙКА ====================
EMAIL = ""
EMAIL_PASSWORD = ""
IMAP_SERVER = "imap.yandex.ru"
IMAP_PORT = 993
SMTP_SERVER = "smtp.yandex.ru"
SMTP_PORT = 465

BOT_TOKEN = ""
DEFAULT_OPERATOR_IDS = [5245766418, 5788922645]
ADMIN_ID = 0

CHECK_INTERVAL = 180
INACTIVITY_CHECK_INTERVAL = 3600
CLOSE_PROMPT_AFTER_DAYS = 14
AUTO_CLOSE_AFTER_DAYS = 7
FULL_DB = "full.db"
LOG_FILE = "bot.log"
# ==================================================

logger = logging.getLogger(__name__)


def _load_secrets_file(path: str = None) -> dict:
    """Читает файл секретов (KEY=VALUE построчно) и возвращает словарь."""
    if path is None:
        path = os.environ.get("SECRETS_FILE", "").strip()
    if not path:
        base = os.path.dirname(os.path.abspath(__file__))
        root = os.path.abspath(os.path.join(base, ".."))
        for folder in (os.getcwd(), root, base):
            for name in ("secrets.txt", "secret.cfg", "secrets.cfg"):
                candidate = os.path.join(folder, name)
                if os.path.isfile(candidate):
                    path = candidate
                    break
            if path:
                break
    if not path or not os.path.isfile(path):
        logger.warning("Файл секретов не найден (secrets.txt/secret.cfg/secrets.cfg).")
        return {}
    secrets = {}
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                logger.warning(f"{os.path.basename(path)} строка {lineno}: нет знака '=' — пропуск")
                continue
            key, _, value = line.partition("=")
            secrets[key.strip()] = value.strip()
    logger.info(f"Загружены секреты из {path}: {list(secrets.keys())}")
    return secrets


class SettingsStorage:
    """Хранение токенов и параметров в БД (таблица settings, ключ-значение)."""

    DEFAULTS = {
        "bot_token": "",
        "admin_id": "",
        "разрешить ответы на чужие заявки": "true",
        "email": "",
        "email_password": "",
        "imap_server": IMAP_SERVER,
        "imap_port": str(IMAP_PORT),
        "smtp_server": SMTP_SERVER,
        "smtp_port": str(SMTP_PORT),
        "check_interval": str(CHECK_INTERVAL),
        "close_prompt_after_days": str(CLOSE_PROMPT_AFTER_DAYS),
        "auto_close_after_days": str(AUTO_CLOSE_AFTER_DAYS),
    }

    def __init__(self, db_path: str = FULL_DB):
        self.db_path = db_path
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            for key, value in self.DEFAULTS.items():
                connection.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )

    def get(self, key: str, default: str = "") -> str:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default

    def set(self, key: str, value):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )


# Сначала читаем secrets.txt/secret.cfg и подставляем значения в DEFAULTS
_secrets = _load_secrets_file()
SettingsStorage.DEFAULTS.update(_secrets)

settings_storage = SettingsStorage()

# Значения из файла секретов должны попадать в БД даже если строка уже есть,
# но пуста (например, из-за прошлых запусков без файла секретов).
# Непустые значения (заданные админом через панель) не перезаписываем.
for _key, _value in _secrets.items():
    if not settings_storage.get(_key):
        settings_storage.set(_key, _value)

BOT_TOKEN = settings_storage.get("bot_token")
ADMIN_ID = int(settings_storage.get("admin_id", "0") or 0)
EMAIL = settings_storage.get("email")
EMAIL_PASSWORD = settings_storage.get("email_password")
IMAP_SERVER = settings_storage.get("imap_server", IMAP_SERVER)
IMAP_PORT = int(settings_storage.get("imap_port", str(IMAP_PORT)))
SMTP_SERVER = settings_storage.get("smtp_server", SMTP_SERVER)
SMTP_PORT = int(settings_storage.get("smtp_port", str(SMTP_PORT)))
CHECK_INTERVAL = int(settings_storage.get("check_interval", str(CHECK_INTERVAL)))

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN не задан: заполните таблицу settings в БД (ключ bot_token)")


#  Динамические геттеры: читают актуальные значения из БД при каждом вызове,
#  чтобы изменённые админом настройки применялись без перезапуска бота.

def get_email() -> str:
    return settings_storage.get("email")


def get_email_password() -> str:
    return settings_storage.get("email_password")


def get_imap_server() -> str:
    return settings_storage.get("imap_server", IMAP_SERVER)


def get_imap_port() -> int:
    return int(settings_storage.get("imap_port", str(IMAP_PORT)))


def get_smtp_server() -> str:
    return settings_storage.get("smtp_server", SMTP_SERVER)


def get_smtp_port() -> int:
    return int(settings_storage.get("smtp_port", str(SMTP_PORT)))


def get_check_interval() -> int:
    return int(settings_storage.get("check_interval", str(CHECK_INTERVAL)))
