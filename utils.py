
"""Вспомогательные функции Чапи."""
import re
from contextvars import ContextVar
from html import escape

# Ранг, установленный командой .дк для текущей команды.
COMMAND_MIN_RANK: ContextVar[int | None] = ContextVar(
    "command_min_rank", default=None
)

# Новая система рангов: 0–5.
RANK_NAMES = {
    0: "Участник",
    1: "Младший модератор",
    2: "Старший модератор",
    3: "Младший админ",
    4: "Старший админ",
    5: "Создатель",
}

ROLE_RANKS = {
    "user": 0,
    "agent": 1,
    "junior_moderator": 1,
    "moderator": 2,
    "senior_moderator": 2,
    "junior_admin": 3,
    "admin": 4,
    "senior_admin": 4,
    "owner": 5,
    "creator": 5,
}

# Сохраняем совместимость с существующими проверками проекта.
ROLE_LEVELS = {
    role: rank * 20
    for role, rank in ROLE_RANKS.items()
}

ROLE_NAMES = {
    "owner": "Создатель",
    "creator": "Создатель",
    "admin": "Старший админ",
    "senior_admin": "Старший админ",
    "junior_admin": "Младший админ",
    "moderator": "Старший модератор",
    "senior_moderator": "Старший модератор",
    "junior_moderator": "Младший модератор",
    "agent": "Младший модератор",
    "user": "Участник",
}

TIME_UNITS = {
    "с": 1, "сек": 1, "секунда": 1, "секунду": 1,
    "секунды": 1, "секунд": 1,
    "м": 60, "мин": 60, "минута": 60, "минуту": 60,
    "минуты": 60, "минут": 60,
    "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600,
    "д": 86400, "дн": 86400, "день": 86400,
    "дня": 86400, "дней": 86400,
}


def parse_duration(text: str) -> int | None:
    if not isinstance(text, str) or len(text) > 32:
        return None

    match = re.fullmatch(
        r"(\d+)\s*(секунд[ауы]?|сек|с|минут[ауы]?|мин|м|"
        r"час(?:а|ов)?|ч|дн|день|дня|дней|д)",
        text.strip().lower(),
    )
    if not match:
        return None

    seconds = int(match.group(1)) * TIME_UNITS[match.group(2)]
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
    """Проверяет роль и дополнительный ранг из .дк."""
    custom_rank = COMMAND_MIN_RANK.get()
    actual_rank = ROLE_RANKS.get(role, 0)

    if custom_rank is not None:
        return actual_rank >= custom_rank

    return ROLE_LEVELS.get(role, 0) >= ROLE_LEVELS.get(required, 0)


def can_act_on(actor_role: str, target_role: str) -> bool:
    """Нельзя применять действия к равной или более высокой роли."""
    return ROLE_LEVELS.get(actor_role, 0) > ROLE_LEVELS.get(target_role, 0)


async def resolve_target(message, args: list[str], db_module):
    """Определяет цель по ответу, @username или Telegram ID."""
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
            user_id = await db_module.find_user_by_username(token)
            if user_id is None:
                return None, (
                    f"Не знаю пользователя {token}. Он должен сначала "
                    "написать в группе либо используйте ответ на его сообщение."
                )
            return user_id, None

    return None, "Ответьте на сообщение пользователя, укажите @username или ID."


def mention(user_id: int, name: str | None) -> str:
    safe_name = escape(str(name or user_id), quote=True)
    return f'<a href="tg://user?id={int(user_id)}">{safe_name}</a>'


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def human_duration(seconds: int) -> str:
    seconds = int(seconds)
    parts = []

    for size, forms in (
        (86400, ("день", "дня", "дней")),
        (3600, ("час", "часа", "часов")),
        (60, ("минуту", "минуты", "минут")),
        (1, ("секунду", "секунды", "секунд")),
    ):
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count} {_plural(count, *forms)}")
        if len(parts) == 2:
            break

    return " ".join(parts) or "0 секунд"
