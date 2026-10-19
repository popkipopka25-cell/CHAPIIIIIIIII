"""Конфигурация бота. Секреты читаются только из окружения или локального .env."""
import os
from dotenv import load_dotenv

load_dotenv()

# Новые имена из требований поддерживаются вместе со старыми для совместимости.
BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
BOT_NAME = os.getenv("BOT_NAME", "Чапи").strip() or "Чапи"


def _read_int(primary: str, legacy: str | None, default: int) -> int:
    raw = os.getenv(primary)
    if raw is None and legacy:
        raw = os.getenv(legacy)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


OWNER_TELEGRAM_ID = _read_int("OWNER_TELEGRAM_ID", "OWNER_ID", 0)
# Совместимость с существующим кодом, который использует OWNER_ID.
OWNER_ID = OWNER_TELEGRAM_ID
DB_PATH = os.getenv("DB_PATH", "bot_database.db").strip() or "bot_database.db"
DAILY_REWARD = _read_int("DAILY_REWARD", None, 100)
START_BALANCE = _read_int("START_BALANCE", None, 1000)
XP_PER_MESSAGE = _read_int("XP_PER_MESSAGE", None, 5)


def check_config() -> list[str]:
    """Возвращает проблемы конфигурации, не раскрывая значения секретов."""
    problems: list[str] = []
    if not BOT_TOKEN:
        problems.append(
            "Не задан TELEGRAM_BOT_TOKEN (допустимо старое имя BOT_TOKEN). "
            "Добавьте токен в локальный .env или Secrets хостинга."
        )
    if OWNER_TELEGRAM_ID <= 0:
        problems.append(
            "Не задан корректный OWNER_TELEGRAM_ID (допустимо старое имя OWNER_ID). "
            "Укажите свой числовой Telegram ID в окружении."
        )
    if DAILY_REWARD < 0 or START_BALANCE < 0 or XP_PER_MESSAGE < 0:
        problems.append("Награды, стартовый баланс и XP_PER_MESSAGE не должны быть отрицательными.")
    if DAILY_REWARD > 10**12 or START_BALANCE > 10**12 or XP_PER_MESSAGE > 1000:
        problems.append("Слишком большие DAILY_REWARD/START_BALANCE (максимум 10^12) или XP_PER_MESSAGE (максимум 1000).")
    return problems
