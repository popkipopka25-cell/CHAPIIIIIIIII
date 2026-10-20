"""Модерация: мут, варны, бан, кик, роли, журнал."""
import time
import re
import logging
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Router, F, Bot
from aiogram.types import Message, ChatPermissions
from aiogram.exceptions import TelegramAPIError

import config
import database as db
from utils import (
    ROLE_NAMES, ROLE_LEVELS, parse_duration, format_duration,
    has_at_least, can_act_on, resolve_target, mention, human_duration,
)

logger = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


async def _has_from_user(message: Message) -> bool:
    return message.from_user is not None


router.message.filter(_has_from_user)

MAX_WARNS = 3
AUTO_MUTE_SEC = 86400

ROLE_ALIASES = {
    "админ": "admin",
    "администратор": "admin",
    "модер": "moderator",
    "модератор": "moderator",
    "агент": "agent",
    "участник": "user",
    "юзер": "user",
}

_telegram_role_cache: dict[tuple[int, int], tuple[float, str]] = {}
_ROLE_CACHE_TTL = 30.0
_ROLE_CACHE_LAST_PRUNE = 0.0


# ---------- настройки автоматического наказания ----------

async def _ensure_warn_settings_table():
    conn = await db.get_db()
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS chapi_moderation_settings (
            chat_id INTEGER NOT NULL,
            setting_key TEXT NOT NULL,
            setting_value TEXT NOT NULL,
            PRIMARY KEY (chat_id, setting_key)
        )
    """)
    await conn.commit()


async def _get_setting(chat_id: int, key: str, default: str) -> str:
    await _ensure_warn_settings_table()
    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT setting_value
        FROM chapi_moderation_settings
        WHERE chat_id = ? AND setting_key = ?
        """,
        (chat_id, key),
    )
    row = await cur.fetchone()
    return str(row["setting_value"]) if row else default


async def _set_setting(chat_id: int, key: str, value) -> None:
    await _ensure_warn_settings_table()
    conn = await db.get_db()
    await conn.execute(
        """
        INSERT INTO chapi_moderation_settings
            (chat_id, setting_key, setting_value)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id, setting_key)
        DO UPDATE SET setting_value = excluded.setting_value
        """,
        (chat_id, key, str(value)),
    )
    await conn.commit()


async def get_warn_settings(chat_id: int) -> dict:
    try:
        limit = int(await _get_setting(chat_id, "warn_limit", str(MAX_WARNS)))
    except (TypeError, ValueError):
        limit = MAX_WARNS

    try:
        duration = int(
            await _get_setting(
                chat_id,
                "warn_action_duration_sec",
                str(AUTO_MUTE_SEC),
            )
        )
    except (TypeError, ValueError):
        duration = AUTO_MUTE_SEC

    action = await _get_setting(chat_id, "warn_action", "mute")

    return {
        "limit": max(1, min(20, limit)),
        "action": action if action in {"mute", "ban"} else "mute",
        "duration": max(0, duration),
    }


async def _is_creator(user_id: int) -> bool:
    try:
        return user_id == int(config.OWNER_TELEGRAM_ID)
    except (AttributeError, TypeError, ValueError):
        return False


@router.message(F.text.regexp(r"(?i)^настройка варнов(?:\s|$)"))
async def cmd_warn_settings(message: Message):
    if not message.from_user:
        return

    if not await _is_creator(message.from_user.id):
        await message.reply(
            "⛔ Настраивать автоматические варны может только создатель бота."
        )
        return

    settings = await get_warn_settings(message.chat.id)
    parts = message.text.strip().split(maxsplit=3)

    if len(parts) == 2:
        action_name = "мут" if settings["action"] == "mute" else "бан"
        duration = (
            "навсегда"
            if settings["duration"] == 0
            else human_duration(settings["duration"])
        )

        await message.answer(
            "⚙️ Настройки автоматических варнов\n\n"
            f"⚠️ Лимит: {settings['limit']} варна\n"
            f"🚫 Наказание: {action_name}\n"
            f"⏱ Срок: {duration}\n\n"
            "Команды:\n"
            "настройка варнов количество 2\n"
            "настройка варнов наказание мут\n"
            "настройка варнов наказание бан\n"
            "настройка варнов срок 1д\n"
            "настройка варнов срок навсегда"
        )
        return

    if len(parts) != 4:
        await message.reply(
            "Используй:\n"
            "настройка варнов\n"
            "настройка варнов количество 2\n"
            "настройка варнов наказание мут\n"
            "настройка варнов наказание бан\n"
            "настройка варнов срок 1д"
        )
        return

    option = parts[2].lower()
    value = parts[3].strip().lower()

    if option == "количество":
        if not value.isdigit() or not 1 <= int(value) <= 20:
            await message.reply("❌ Укажи число от 1 до 20.")
            return

        await _set_setting(message.chat.id, "warn_limit", int(value))
        await message.reply(
            f"✅ Автоматическое наказание будет выдаваться "
            f"после {int(value)} активных варнов."
        )
        return

    if option == "наказание":
        if value not in {"мут", "бан"}:
            await message.reply("❌ Выбери «мут» или «бан».")
            return

        action = "mute" if value == "мут" else "ban"
        await _set_setting(message.chat.id, "warn_action", action)

        current_duration = settings["duration"]
        if action == "mute" and current_duration == 0:
            await _set_setting(
                message.chat.id,
                "warn_action_duration_sec",
                AUTO_MUTE_SEC,
            )

        await message.reply(f"✅ После лимита варнов будет выдаваться: {value}.")
        return

    if option == "срок":
        if value == "навсегда":
            if settings["action"] != "ban":
                await message.reply(
                    "❌ Сначала выбери бан:\n"
                    "настройка варнов наказание бан"
                )
                return
            duration = 0
        else:
            duration = parse_duration(value)
            if duration is None or duration < 60 or duration > 31536000:
                await message.reply(
                    "❌ Укажи срок от 1 минуты до 365 дней "
                    "(например, 30м, 2ч, 7д). "
                    "Для постоянного бана используй «навсегда»."
                )
                return

        await _set_setting(
            message.chat.id,
            "warn_action_duration_sec",
            duration,
        )
        await message.reply(
            "✅ Срок автоматического наказания: "
            + ("навсегда." if duration == 0 else human_duration(duration) + ".")
        )
        return

    await message.reply("❌ Неизвестная настройка. Напиши «настройка варнов».")


# ---------- роли ----------

async def effective_role(bot: Bot, chat_id: int, user_id: int) -> str:
    global _ROLE_CACHE_LAST_PRUNE

    role = await db.get_role(chat_id, user_id)
    if role not in ROLE_LEVELS:
        role = "user"
    if role == "owner":
        return role

    now = time.monotonic()
    key = (chat_id, user_id)
    cached = _telegram_role_cache.get(key)
    cache_ttl = 5.0 if cached and cached[1] == "unknown" else _ROLE_CACHE_TTL

    if cached and now - cached[0] < cache_ttl:
        telegram_role = cached[1]
    else:
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            telegram_role = (
                "owner" if member.status == "creator"
                else "admin" if member.status == "administrator"
                else "user"
            )
        except TelegramAPIError:
            telegram_role = "unknown"

        _telegram_role_cache[key] = (now, telegram_role)

    if now - _ROLE_CACHE_LAST_PRUNE > 120:
        stale_before = now - _ROLE_CACHE_TTL * 4
        stale_keys = [
            k for k, value in _telegram_role_cache.items()
            if value[0] < stale_before
        ]
        for stale_key in stale_keys:
            _telegram_role_cache.pop(stale_key, None)

        if len(_telegram_role_cache) > 20000:
            newest = sorted(
                _telegram_role_cache.items(),
                key=lambda item: item[1][0],
                reverse=True,
            )[:15000]
            _telegram_role_cache.clear()
            _telegram_role_cache.update(newest)

        _ROLE_CACHE_LAST_PRUNE = now

    if telegram_role == "owner":
        return "owner"
    if telegram_role == "admin" and ROLE_LEVELS.get(role, 0) < ROLE_LEVELS["admin"]:
        return "admin"
    return role


async def _prepare(
    message: Message,
    required: str,
    skip: int = 1,
    first_line: bool = False,
):
    if not message.from_user:
        await message.reply(
            "Эту команду нужно отправить от обычного аккаунта, не от имени канала."
        )
        return None

    source = message.text.split("\n", 1)[0] if first_line else message.text
    args = source.split()[skip:]
    actor = await effective_role(
        message.bot, message.chat.id, message.from_user.id
    )

    if not has_at_least(actor, required):
        await message.reply(f"⛔ Нужна роль не ниже «{ROLE_NAMES[required]}».")
        return None

    target_id, err = await resolve_target(message, args, db)
    if err:
        await message.reply(err)
        return None

    if not message.reply_to_message and args:
        args = args[1:]

    if target_id == message.from_user.id:
        await message.reply("Нельзя применить это к себе.")
        return None

    target_role = await effective_role(message.bot, message.chat.id, target_id)
    if not can_act_on(actor, target_role):
        await message.reply("⛔ Нельзя: у цели такая же или более высокая роль.")
        return None

    return target_id, args


def _split_time(args: list[str]):
    if args:
        if len(args) > 1:
            sec = parse_duration(f"{args[0]} {args[1]}")
            if sec:
                return sec, " ".join(args[2:]) or None

            if (
                args[0].strip().lstrip("-").isdigit()
                and re.fullmatch(r"[a-zа-яё]+", args[1].strip().lower())
            ):
                return -1, (
                    "Некорректный срок. Используйте, например: "
                    "10м, 2ч или 3д (максимум 365 дней)."
                )

        sec = parse_duration(args[0])
        if sec:
            return sec, " ".join(args[1:]) or None

        if re.fullmatch(r"-?\d+\s*[a-zа-яё]+", args[0].strip().lower()):
            return -1, (
                "Некорректный срок. Используйте, например: "
                "10м, 2ч или 3д (максимум 365 дней)."
            )

    return None, " ".join(args) or None


async def _name(uid: int) -> str:
    user = await db.get_user(uid)
    return mention(uid, user["first_name"] if user else None)


async def _add_punishment(
    chat_id, user_id, ptype, reason, by_id, until=None
):
    conn = await db.get_db()
    await conn.execute(
        """
        INSERT INTO punishments
            (chat_id,user_id,type,reason,by_id,created_at,until)
        VALUES (?,?,?,?,?,?,?)
        """,
        (chat_id, user_id, ptype, reason, by_id, time.time(), until),
    )
    await conn.commit()


async def _deactivate(chat_id, user_id, ptype):
    conn = await db.get_db()
    await conn.execute(
        """
        UPDATE punishments
        SET active=0
        WHERE chat_id=? AND user_id=? AND type=? AND active=1
        """,
        (chat_id, user_id, ptype),
    )
    await conn.commit()


def unmute_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )


async def do_mute(bot: Bot, chat_id: int, user_id: int, seconds: int):
    until = datetime.now(timezone.utc) + timedelta(seconds=max(seconds, 60))
    await bot.restrict_chat_member(
        chat_id,
        user_id,
        ChatPermissions(can_send_messages=False),
        until_date=until,
    )


# ---------- мут ----------

def _display_name(user) -> str:
    if user is None:
        return "Неизвестный"
    full = " ".join(
        x for x in (
            user.first_name,
            getattr(user, "last_name", None),
        ) if x
    )
    return full or (f"@{user.username}" if user.username else str(user.id))


async def _target_mention(message: Message, target: int) -> str:
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == target:
        return mention(target, _display_name(reply.from_user))
    return await _name(target)


def split_command(text: str) -> tuple[list[str], str | None]:
    first, _, rest = (text or "").partition("\n")
    return first.split(), (rest.strip() or None)


def format_mute_message(
    target_html: str,
    moderator_html: str,
    seconds: int,
    reason: str | None,
) -> str:
    lines = [
        f"🔇 {target_html} лишается права слова на {human_duration(seconds)}"
    ]
    if reason:
        lines.append(f"💬 Причина: {escape(reason, quote=True)}")
        lines.append(f"🦸 Модератор: {moderator_html}")
    else:
        lines.append(f"👺 Модератор: {moderator_html}")
    return "\n".join(lines)


def format_unmute_message(target_html: str) -> str:
    return (
        f"✅ Пользователю {target_html} вернули право слова. "
        "Можете свободно общаться! Но лучше следите за языком…"
    )


@router.message(F.text.regexp(r"(?i)^мут(\s|$)"))
async def cmd_mute(message: Message):
    res = await _prepare(message, "moderator", first_line=True)
    if not res:
        return

    target, _ = res
    words, extra_reason = split_command(message.text)
    args = words[1:]

    if not message.reply_to_message and args:
        args = args[1:]

    sec, reason = _split_time(args)
    if sec == -1:
        await message.reply(reason)
        return

    if extra_reason:
        reason = f"{reason} {extra_reason}" if reason else extra_reason

    if reason and len(reason) > 500:
        reason = reason[:500] + "…"

    sec = sec or 3600

    try:
        await do_mute(message.bot, message.chat.id, target, sec)
    except TelegramAPIError as e:
        await message.reply(
            "❌ Не удалось выдать мут "
            "(проверьте права бота): "
            f"{escape(str(getattr(e, 'message', '') or e or 'неизвестная ошибка'), quote=True)}"
        )
        return

    try:
        await _deactivate(message.chat.id, target, "mute")
        await _add_punishment(
            message.chat.id,
            target,
            "mute",
            reason,
            message.from_user.id,
            time.time() + sec,
        )
        await db.log_action(
            message.chat.id,
            message.from_user.id,
            "mute",
            target,
            f"{sec}с: {reason or '—'}",
        )
    except Exception:
        logger.exception("Мут выдан в Telegram, но не записан в базу")
        await message.reply(
            "⚠️ Мут выдан в Telegram, но не сохранён в истории бота."
        )

    moderator = mention(
        message.from_user.id,
        _display_name(message.from_user),
    )
    await message.answer(
        format_mute_message(
            await _target_mention(message, target),
            moderator,
            sec,
            reason,
        )
    )


@router.message(F.text.regexp(r"(?i)^проверить мут(\s|$)"))
async def cmd_check_mute(message: Message):
    if not message.from_user:
        await message.reply("Проверка доступна только обычным аккаунтам.")
        return

    actor = await effective_role(
        message.bot, message.chat.id, message.from_user.id
    )
    if not has_at_least(actor, "moderator"):
        await message.reply("⛔ Проверять статус мута могут модераторы и выше.")
        return

    args = message.text.split()[2:]
    target, err = await resolve_target(message, args, db)
    if err:
        await message.reply(err)
        return

    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT reason, until
        FROM punishments
        WHERE chat_id=? AND user_id=? AND type='mute' AND active=1
        ORDER BY id DESC LIMIT 1
        """,
        (message.chat.id, target),
    )
    row = await cur.fetchone()

    telegram_muted = False
    try:
        member = await message.bot.get_chat_member(message.chat.id, target)
        telegram_muted = (
            member.status == "restricted"
            and not getattr(member, "can_send_messages", True)
        )
    except TelegramAPIError:
        pass

    if row and row["until"] is not None and float(row["until"]) <= time.time():
        await conn.execute(
            """
            UPDATE punishments SET active=0
            WHERE chat_id=? AND user_id=? AND type='mute'
              AND active=1 AND until<=?
            """,
            (message.chat.id, target, time.time()),
        )
        await conn.commit()
        row = None

    if not row and not telegram_muted:
        await message.answer(f"✅ {await _name(target)} сейчас не в муте.")
        return

    until = float(row["until"]) if row and row["until"] is not None else None
    if until:
        remaining = max(0, int(until - time.time()))
        duration = format_duration(remaining)
        expiry = datetime.fromtimestamp(until).strftime("%d.%m.%Y %H:%M")
        await message.answer(
            f"🔇 {await _name(target)} в муте. Осталось: {duration}. "
            f"До: {expiry}. Причина: "
            f"{escape(str(row['reason'] or 'не указана'), quote=True)}"
        )
    else:
        await message.answer(
            f"🔇 {await _name(target)} ограничен в Telegram. "
            "В базе нет срока окончания."
        )


@router.message(F.text.regexp(r"(?i)^(размут|снять мут|анмут|говори)(\s|$)"))
async def cmd_unmute(message: Message):
    skip = 2 if message.text.lower().startswith("снять мут") else 1
    res = await _prepare(message, "moderator", skip=skip)
    if not res:
        return

    target, _ = res
    try:
        await message.bot.restrict_chat_member(
            message.chat.id,
            target,
            unmute_permissions(),
        )
    except TelegramAPIError as e:
        await message.reply(
            f"Не удалось размутить: "
            f"{escape(str(getattr(e, 'message', '') or e or 'неизвестная ошибка'), quote=True)}"
        )
        return

    try:
        await _deactivate(message.chat.id, target, "mute")
        await db.log_action(
            message.chat.id,
            message.from_user.id,
            "unmute",
            target,
        )
    except Exception:
        logger.exception("Размут выполнен, но не записан в базу")

    await message.answer(
        format_unmute_message(await _target_mention(message, target))
    )


# ---------- варны ----------

async def count_warns(chat_id, user_id) -> int:
    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM punishments
        WHERE chat_id=? AND user_id=? AND type='warn' AND active=1
        """,
        (chat_id, user_id),
    )
    row = await cur.fetchone()
    return int(row["c"]) if row else 0


@router.message(F.text.regexp(r"(?i)^варн(\s|$)"))
async def cmd_warn(message: Message):
    res = await _prepare(message, "moderator")
    if not res:
        return

    target, args = res
    reason = (" ".join(args) or "без причины")[:500]

    await _add_punishment(
        message.chat.id,
        target,
        "warn",
        reason,
        message.from_user.id,
    )
    await db.log_action(
        message.chat.id,
        message.from_user.id,
        "warn",
        target,
        reason,
    )

    settings = await get_warn_settings(message.chat.id)
    count = await count_warns(message.chat.id, target)

    text = (
        f"⚠️ {await _name(target)} получил варн "
        f"({count}/{settings['limit']}).\n"
        f"Причина: {escape(reason, quote=True)}"
    )

    if count >= settings["limit"]:
        action = settings["action"]
        duration = settings["duration"]

        try:
            if action == "mute":
                duration = max(60, duration)
                await do_mute(
                    message.bot,
                    message.chat.id,
                    target,
                    duration,
                )
                punishment_type = "mute"
                action_name = "auto_mute"
                punishment_reason = "автоматический мут за лимит варнов"
            else:
                until = (
                    datetime.now(timezone.utc) + timedelta(seconds=duration)
                    if duration > 0
                    else None
                )
                await message.bot.ban_chat_member(
                    message.chat.id,
                    target,
                    until_date=until,
                )
                punishment_type = "ban"
                action_name = "auto_ban"
                punishment_reason = "автоматический бан за лимит варнов"

        except TelegramAPIError as e:
            logger.exception("Не удалось применить автоматическое наказание")
            text += (
                "\n❌ Не удалось применить автоматическое наказание. "
                "Проверь права бота в группе. Варны сохранены."
            )
            await message.answer(text)
            return

        await _deactivate(message.chat.id, target, "warn")
        await _deactivate(message.chat.id, target, punishment_type)

        await _add_punishment(
            message.chat.id,
            target,
            punishment_type,
            punishment_reason,
            message.bot.id,
            time.time() + duration if duration > 0 else None,
        )

        await db.log_action(
            message.chat.id,
            message.bot.id,
            action_name,
            target,
            (
                f"{settings['limit']} варнов; срок {duration}с"
                if duration > 0
                else f"{settings['limit']} варнов; навсегда"
            ),
        )

        if punishment_type == "mute":
            text += (
                f"\n🔇 Достигнут лимит — мут на "
                f"{human_duration(duration)}. Варны сброшены."
            )
        else:
            term = human_duration(duration) if duration > 0 else "навсегда"
            text += f"\n🚫 Достигнут лимит — бан {term}. Варны сброшены."

    await message.answer(text)


@router.message(F.text.regexp(r"(?i)^мои варны$"))
async def cmd_my_warns(message: Message):
    if not message.from_user:
        return

    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT reason, created_at
        FROM punishments
        WHERE chat_id=? AND user_id=? AND type='warn' AND active=1
        ORDER BY id DESC LIMIT 20
        """,
        (message.chat.id, message.from_user.id),
    )
    rows = await cur.fetchall()

    if not rows:
        await message.answer("✅ У вас нет активных варнов.")
        return

    lines = [
        f"{i}. {escape(str(row['reason'] or 'без причины'), quote=True)} — "
        f"{datetime.fromtimestamp(row['created_at']):%d.%m %H:%M}"
        for i, row in enumerate(rows, 1)
    ]
    await message.answer(
        f"⚠️ Ваши варны ({len(rows)}):\n" + "\n".join(lines)
    )


@router.message(F.text.regexp(r"(?i)^варнлист$"))
async def cmd_warn_list(message: Message):
    if not message.from_user or not has_at_least(
        await effective_role(message.bot, message.chat.id, message.from_user.id),
        "moderator",
    ):
        await message.reply("⛔ Варнлист доступен модераторам и выше.")
        return

    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT user_id, reason, created_at
        FROM punishments
        WHERE chat_id=? AND type='warn' AND active=1
        ORDER BY id DESC LIMIT 20
        """,
        (message.chat.id,),
    )
    rows = await cur.fetchall()

    if not rows:
        await message.answer("В активном варнлисте пусто.")
        return

    lines = [
        f"• {mention(row['user_id'], str(row['user_id']))}: "
        f"{escape(str(row['reason'] or 'без причины'), quote=True)} — "
        f"{datetime.fromtimestamp(row['created_at']):%d.%m %H:%M}"
        for row in rows
    ]
    await message.answer(
        "⚠️ <b>Активный варнлист:</b>\n" + "\n".join(lines),
        parse_mode="HTML",
    )


@router.message(F.text.regexp(r"(?i)^снять все варны(\s|$)"))
async def cmd_unwarn_all(message: Message):
    res = await _prepare(message, "moderator", skip=3)
    if not res:
        return

    target, _ = res
    conn = await db.get_db()
    cur = await conn.execute(
        """
        UPDATE punishments SET active=0
        WHERE chat_id=? AND user_id=? AND type='warn' AND active=1
        """,
        (message.chat.id, target),
    )
    await conn.commit()

    if cur.rowcount:
        await db.log_action(
            message.chat.id,
            message.from_user.id,
            "unwarn_all",
            target,
            str(cur.rowcount),
        )
        await message.answer(
            f"✅ Снято варнов: {cur.rowcount}. "
            f"Пользователь: {await _name(target)}."
        )
    else:
        await message.answer("У этого пользователя нет активных варнов.")


@router.message(F.text.regexp(r"(?i)^муты$"))
async def cmd_mute_list(message: Message):
    if not message.from_user or not has_at_least(
        await effective_role(message.bot, message.chat.id, message.from_user.id),
        "moderator",
    ):
        await message.reply("⛔ Список мутов доступен модераторам и выше.")
        return

    now = time.time()
    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT user_id, reason, until
        FROM punishments
        WHERE chat_id=? AND type='mute' AND active=1
          AND (until IS NULL OR until>?)
        ORDER BY id DESC LIMIT 20
        """,
        (message.chat.id, now),
    )
    rows = await cur.fetchall()

    if not rows:
        await message.answer("Активных мутов нет.")
        return

    lines = []
    for row in rows:
        expiry = (
            "без срока в базе"
            if row["until"] is None
            else datetime.fromtimestamp(row["until"]).strftime("до %d.%m %H:%M")
        )
        lines.append(
            f"• {await _name(row['user_id'])} — "
            f"{escape(str(row['reason'] or 'без причины'), quote=True)} "
            f"({expiry})"
        )

    await message.answer(
        "🔇 <b>Активные муты:</b>\n" + "\n".join(lines),
        parse_mode="HTML",
    )


@router.message(F.text.regexp(r"(?i)^(банлист|список банов|баны)$"))
async def cmd_ban_list(message: Message):
    if not message.from_user or not has_at_least(
        await effective_role(message.bot, message.chat.id, message.from_user.id),
        "moderator",
    ):
        await message.reply("⛔ Банлист доступен модераторам и выше.")
        return

    now = time.time()
    conn = await db.get_db()
    cur = await conn.execute(
        """
        SELECT user_id, reason, until
        FROM punishments
        WHERE chat_id=? AND type='ban' AND active=1
          AND (until IS NULL OR until>?)
        ORDER BY id DESC LIMIT 20
        """,
        (message
