
"""Настройка минимальных рангов команд Чапи."""
from __future__ import annotations

import re
import time

from aiogram import BaseMiddleware, F, Router
from aiogram.types import Message

import config
import database as db

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

RANK_NAMES = {
    0: "Участник",
    1: "Младший модератор",
    2: "Старший модератор",
    3: "Младший админ",
    4: "Старший админ",
    5: "Создатель",
}

COMMANDS = {
    10: ("помощь",),
    11: ("мой id",),
    12: ("кто админ",),
    13: ("инфо группы",),
    14: ("профиль",),
    20: ("мут",),
    21: ("размут", "снять мут", "анмут"),
    22: ("варн",),
    23: ("варны",),
    24: ("мои варны", "варнлист"),
    25: ("снять варн",),
    26: ("снять все варны",),
    27: ("проверить мут",),
    28: ("муты",),
    29: ("банлист",),
    30: ("причина",),
    31: ("бан",),
    32: ("разбан",),
    33: ("кик",),
    34: ("роль",),
    35: ("журнал",),
    36: ("жалоба",),
    37: ("обжалование",),
    38: ("дела",),
    39: ("дело принять",),
    40: ("дело отклонить",),
    41: ("дело",),
    50: ("баланс",),
    51: ("бонус",),
    52: ("перевести",),
    53: ("топ",),
    54: ("магазин",),
    55: ("купить",),
    56: ("инвентарь",),
    57: ("подарить",),
    58: ("история",),
    59: ("задания",),
    60: ("достижения",),
    61: ("премиум статус",),
    62: ("премиум выдать",),
    63: ("премиум снять",),
    64: ("статус установить",),
    65: ("статус",),
    66: ("выдать",),
    67: ("забрать",),
    68: ("товар добавить",),
    69: ("товар удалить",),
    70: ("брак",),
    71: ("принять брак",),
    72: ("отклонить брак",),
    73: ("мой брак",),
    74: ("развод",),
    75: ("восстановить брак",),
    76: ("принять восстановление брака",),
    77: ("отклонить восстановление брака",),
    80: ("создать клан",),
    81: ("мой клан",),
    82: ("клан участники",),
    83: ("клан заявка",),
    84: ("моя заявка в клан",),
    85: ("отменить заявку в клан",),
    86: ("клан выйти",),
    90: ("монетка", "орёл или решка"),
    91: ("кости", "кубик"),
    92: ("случайное число",),
    100: ("заметка создать",),
    101: ("заметка",),
    102: ("заметка удалить",),
    103: ("заметки",),
    104: ("автоответ добавить",),
    105: ("автоответ удалить",),
    106: ("автоответы",),
    110: ("настройки",),
    111: ("настройка",),
    112: ("приветствие текст",),
    113: ("спам",),
}

ALIASES = sorted(
    (
        (alias, code)
        for code, names in COMMANDS.items()
        for alias in names
    ),
    key=lambda item: len(item[0]),
    reverse=True,
)


def _creator_id() -> int:
    """Получает ID создателя бота из config.py."""
    try:
        return int(config.OWNER_TELEGRAM_ID or 0)
    except (AttributeError, TypeError, ValueError):
        return 0


async def ensure_table() -> None:
    """Создаёт таблицу настроек рангов."""
    conn = await db.get_db()
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS command_rank_overrides (
            chat_id INTEGER NOT NULL,
            command_code INTEGER NOT NULL,
            min_rank INTEGER NOT NULL CHECK(min_rank BETWEEN 0 AND 5),
            updated_by INTEGER NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(chat_id, command_code)
        )
        """
    )
    await conn.commit()


def detect_command(text: str) -> int | None:
    """Возвращает код команды, если сообщение начинается с её названия."""
    first_line = (text or "").split("\n", 1)[0].strip().lower()

    for alias, code in ALIASES:
        if re.match(r"^" + re.escape(alias) + r"(?:\s|$)", first_line):
            return code

    return None


async def get_rank(message: Message) -> int:
    """Определяет ранг пользователя в группе."""
    user = message.from_user
    if user is None:
        return 0

    creator_id = _creator_id()
    if creator_id > 0 and message.from_user.id == creator_id:
        return 5

    from handlers.moderation import effective_role

    role = await effective_role(
        message.bot,
        message.chat.id,
        message.from_user.id,
    )

    return {
        "user": 0,
        "agent": 1,
        "moderator": 2,
        "admin": 3,
        "owner": 4,
    }.get(role, 0)


async def get_override(chat_id: int, code: int) -> int | None:
    """Получает переопределённый ранг команды для группы."""
    await ensure_table()

    conn = await db.get_db()
    cursor = await conn.execute(
        """
        SELECT min_rank
        FROM command_rank_overrides
        WHERE chat_id = ? AND command_code = ?
        """,
        (chat_id, code),
    )
    row = await cursor.fetchone()

    if row is None:
        return None

    if isinstance(row, dict):
        return int(row["min_rank"])

    return int(row[0])


class CommandRankMiddleware(BaseMiddleware):
    """Применяет заданный минимальный ранг команды."""

    async def __call__(self, handler, event, data):
        if not isinstance(event, Message) or not event.text:
            return await handler(event, data)

        if event.from_user is None:
            return await handler(event, data)

        if re.match(r"(?i)^\.дк(?:\s|$)", event.text.strip()):
            return await handler(event, data)

        code = detect_command(event.text)
        if code is None:
            return await handler(event, data)

        try:
            minimum = await get_override(event.chat.id, code)
        except Exception:
            await event.reply(
                "⚠️ Не удалось проверить настройки рангов. "
                "Команда не выполнена."
            )
            return

        if minimum is None:
            return await handler(event, data)

        try:
            rank = await get_rank(event)
        except Exception:
            await event.reply(
                "⚠️ Не удалось определить твой ранг. "
                "Команда не выполнена."
            )
            return

        if rank < minimum:
            await event.reply(
                f"⛔ Для этой команды нужен ранг "
                f"{minimum} — {RANK_NAMES[minimum]}."
            )
            return

        return await handler(event, data)


@router.message(F.text.regexp(r"(?i)^\.дк(?:\s|$)"))
async def command_rank_settings(message: Message):
    """Показывает коды команд и настраивает минимальные ранги."""
    if message.from_user is None:
        return

    creator_id = _creator_id()

    # Только владелец ID из config.OWNER_TELEGRAM_ID.
    if creator_id <= 0 or message.from_user.id != creator_id:
        await message.reply(
            "⛔ Настраивать .дк может только создатель бота."
        )
        return

    try:
        await ensure_table()
    except Exception:
        await message.reply(
            "⚠️ Не удалось подготовить таблицу настроек. "
            "Проверь подключение к базе данных."
        )
        return

    parts = message.text.strip().split()

    if len(parts) == 1:
        lines = [
            f"{code} — {', '.join(names)}"
            for code, names in sorted(COMMANDS.items())
        ]
        await message.reply(
            "📋 Коды команд Чапи\n\n"
            + "\n".join(lines)
            + "\n\nФормат: .дк КОД РАНГ"
            + "\nПример: .дк 20 3"
            + "\n\nРанги:"
            + "\n0 — Участник"
            + "\n1 — Младший модератор"
            + "\n2 — Старший модератор"
            + "\n3 — Младший админ"
            + "\n4 — Старший админ"
            + "\n5 — Создатель"
        )
        return

    if (
        len(parts) != 3
        or not parts[1].isdigit()
        or not parts[2].isdigit()
    ):
        await message.reply(
            "❌ Неверный формат.\n"
            "Используй: .дк КОД РАНГ\n"
            "Например: .дк 20 3"
        )
        return

    code = int(parts[1])
    rank = int(parts[2])

    if code not in COMMANDS:
        await message.reply(
            "❌ Неизвестный код команды. Напиши .дк для списка."
        )
        return

    if rank not in RANK_NAMES:
        await message.reply("❌ Ранг должен быть числом от 0 до 5.")
        return

    try:
        conn = await db.get_db()
        await conn.execute(
            """
            INSERT INTO command_rank_overrides
                (chat_id, command_code, min_rank, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, command_code) DO UPDATE SET
                min_rank = excluded.min_rank,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            (
                message.chat.id,
                code,
                rank,
                message.from_user.id,
                time.time(),
            ),
        )
        await conn.commit()
    except Exception:
        await message.reply(
            "⚠️ Не удалось сохранить настройки ранга. "
            "Проверь базу данных и попробуй ещё раз."
        )
        return

    try:
        await db.log_action(
            message.chat.id,
            message.from_user.id,
            "command_rank_set",
            None,
            f"code={code}; min_rank={rank}",
        )
    except Exception:
        pass

    await message.reply(
        f"✅ Для команды «{', '.join(COMMANDS[code])}» "
        f"установлен минимальный ранг {rank} — {RANK_NAMES[rank]}."
    )
