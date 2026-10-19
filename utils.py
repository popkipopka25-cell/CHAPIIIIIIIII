"""Вспомогательные функции: парсинг времени, целей команд, роли."""
import re
from html import escape

ROLE_LEVELS = {"user": 0, "agent": 20, "moderator": 30, "admin": 50, "owner": 100}
ROLE_NAMES = {
    "owner": "Владелец",
    "admin": "Администратор",
    "moderator": "Модератор",
    "agent": "Агент антиспама",
    "user": "Участник",
}

TIME_UNITS = {
    "с": 1, "сек": 1, "секунда": 1, "секунду": 1, "секунды": 1, "секунд": 1,
    "м": 60, "мин": 60, "минута": 60, "минуту": 60, "минуты": 60, "минут": 60,
    "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600,
    "д": 86400, "дн": 86400, "день": 86400, "дня": 86400, "дней": 86400,
}


def parse_duration(text: str) -> int | None:
    """'10м' -> 600, '1ч' -> 3600, '2д' -> 172800. None если не распознано."""
    if not isinstance(text, str) or len(text) > 32:
        return None
    m = re.fullmatch(r"(\d+)\s*(секунд[ауы]?|сек|с|минут[ауы]?|мин|м|час(?:а|ов)?|ч|дн|день|дня|дней|д)", text.strip().lower())
    if not m:
        return None
    seconds = int(m.group(1)) * TIME_UNITS[m.group(2)]
    # Нулевые и чрезмерно большие сроки не должны случайно превращаться
    # в бессрочные наказания или ломать расчёт даты.
    if seconds <= 0 or seconds > 365 * 86400:
        return None
    return seconds


def format_duration(seconds: int) -> str:
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{seconds // 86400} дн."
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600} ч."
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60} мин."
    return f"{seconds} сек."


def has_at_least(role: str, required: str) -> bool:
    return ROLE_LEVELS.get(role, 0) >= ROLE_LEVELS.get(required, 0)


def can_act_on(actor_role: str, target_role: str) -> bool:
    """Нельзя применять действия к пользователю с равной или более высокой ролью."""
    return ROLE_LEVELS.get(actor_role, 0) > ROLE_LEVELS.get(target_role, 0)


async def resolve_target(message, args: list[str], db_module) -> tuple[int | None, str | None]:
    """Определяет цель команды: ответ на сообщение, @username или числовой ID.
    Возвращает (user_id, ошибка)."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id, None
    if args:
        token = args[0]
        if token.isascii() and token.isdigit():
            if len(token) > 19:
                return None, "Числовой ID слишком длинный."
            value = int(token)
            if 0 < value <= 9_223_372_036_854_775_807:
                return value, None
            return None, "Укажите корректный положительный Telegram ID."
        if token.startswith("@"):
            uid = await db_module.find_user_by_username(token)
            if uid is None:
                return None, (
                    f"Не знаю пользователя {token}. Он должен сначала написать хотя бы одно "
                    "сообщение в группе, либо используйте ответ на его сообщение."
                )
            return uid, None
    return None, "Укажите пользователя: ответьте на его сообщение, напишите @username или ID."


def mention(user_id: int, name: str | None) -> str:
    """HTML-безопасная ссылка на пользователя для сообщений бота."""
    safe_name = escape(str(name or user_id), quote=True)
    return f'<a href="tg://user?id={int(user_id)}">{safe_name}</a>'


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def human_duration(seconds: int) -> str:
    """600 -> '10 минут', 3600 -> '1 час', 93600 -> '1 день 2 часа'."""
    seconds = int(seconds)
    parts = []
    for size, forms in (
        (86400, ("день", "дня", "дней")),
        (3600, ("час", "часа", "часов")),
        (60, ("минуту", "минуты", "минут")),
        (1, ("секунду", "секунды", "секунд")),
    ):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n} {_plural(n, *forms)}")
        if len(parts) == 2:
            break
    return " ".join(parts) or "0 секунд"
