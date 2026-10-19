"""Модерация: мут, варны, бан, кик, роли, журнал."""
import time
import re
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Router, F, Bot
from aiogram.types import Message, ChatPermissions
from aiogram.exceptions import TelegramAPIError

import database as db
from utils import (
    ROLE_NAMES, ROLE_LEVELS, parse_duration, format_duration,
    has_at_least, can_act_on, resolve_target, mention, human_duration,
)
import logging

logger = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


async def _has_from_user(message: Message) -> bool:
    """Не обрабатывает команды, отправленные от имени канала без обычного пользователя."""
    return message.from_user is not None


router.message.filter(_has_from_user)

MAX_WARNS = 3            # после 3 варнов — автомут
AUTO_MUTE_SEC = 86400    # на 1 день

ROLE_ALIASES = {
    "админ": "admin", "администратор": "admin",
    "модер": "moderator", "модератор": "moderator",
    "агент": "agent", "участник": "user", "юзер": "user",
}


_telegram_role_cache: dict[tuple[int, int], tuple[float, str]] = {}
_ROLE_CACHE_TTL = 30.0
_ROLE_CACHE_LAST_PRUNE = 0.0


async def effective_role(bot: Bot, chat_id: int, user_id: int) -> str:
    """Роль из базы + права Telegram с коротким кэшем API, чтобы не делать запрос на каждое сообщение."""
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
            telegram_role = "owner" if member.status == "creator" else "admin" if member.status == "administrator" else "user"
        except TelegramAPIError:
            # При сетевой ошибке не прерываем обработку сообщений; повторяем запрос
            # через несколько секунд, а не на каждом сообщении.
            telegram_role = "unknown"
        _telegram_role_cache[key] = (now, telegram_role)

    if now - _ROLE_CACHE_LAST_PRUNE > 120:
        stale_before = now - _ROLE_CACHE_TTL * 4
        stale_keys = [k for k, value in _telegram_role_cache.items() if value[0] < stale_before]
        for stale_key in stale_keys:
            _telegram_role_cache.pop(stale_key, None)
        if len(_telegram_role_cache) > 20_000:
            newest = sorted(_telegram_role_cache.items(), key=lambda item: item[1][0], reverse=True)[:15_000]
            _telegram_role_cache.clear()
            _telegram_role_cache.update(newest)
        _ROLE_CACHE_LAST_PRUNE = now

    if telegram_role == "owner":
        return "owner"
    if telegram_role == "admin" and ROLE_LEVELS.get(role, 0) < ROLE_LEVELS["admin"]:
        return "admin"
    return role


async def _prepare(message: Message, required: str, skip: int = 1, first_line: bool = False):
    """Проверяет права и цель. Возвращает (target_id, rest_args) или None."""
    if not message.from_user:
        await message.reply("Эту команду нужно отправить от обычного аккаунта, не от имени канала.")
        return None
    source = message.text.split("\n", 1)[0] if first_line else message.text
    args = source.split()[skip:]
    actor = await effective_role(message.bot, message.chat.id, message.from_user.id)
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
        # Поддерживаем как сокращения «10м», так и полные формы «10 минут».
        if len(args) > 1:
            sec = parse_duration(f"{args[0]} {args[1]}")
            if sec:
                return sec, " ".join(args[2:]) or None
            # Если срок разбит на два слова (например, «1 месяц»), не превращаем
            # неизвестную единицу измерения в бессрочный бан.
            if args[0].strip().lstrip("-").isdigit() and re.fullmatch(r"[a-zа-яё]+", args[1].strip().lower()):
                return -1, "Некорректный срок. Используйте, например: 10м, 2ч или 3д (максимум 365 дней)."
        sec = parse_duration(args[0])
        if sec:
            return sec, " ".join(args[1:]) or None
        # Не превращаем ошибочный срок вроде 999д или -1д в текст причины.
        if re.fullmatch(r"-?\d+\s*[a-zа-яё]+", args[0].strip().lower()):
            return -1, "Некорректный срок. Используйте, например: 10м, 2ч или 3д (максимум 365 дней)."
    return None, " ".join(args) or None


async def _name(uid: int) -> str:
    u = await db.get_user(uid)
    return mention(uid, u["first_name"] if u else None)


async def _add_punishment(chat_id, user_id, ptype, reason, by_id, until=None):
    conn = await db.get_db()
    await conn.execute(
        "INSERT INTO punishments (chat_id,user_id,type,reason,by_id,created_at,until) VALUES (?,?,?,?,?,?,?)",
        (chat_id, user_id, ptype, reason, by_id, time.time(), until),
    )
    await conn.commit()


async def _deactivate(chat_id, user_id, ptype):
    conn = await db.get_db()
    await conn.execute(
        "UPDATE punishments SET active=0 WHERE chat_id=? AND user_id=? AND type=? AND active=1",
        (chat_id, user_id, ptype),
    )
    await conn.commit()


def unmute_permissions() -> ChatPermissions:
    """Разрешения на отправку сообщений и медиа после снятия мута."""
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
        chat_id, user_id, ChatPermissions(can_send_messages=False), until_date=until
    )


# ---------- мут ----------

def _display_name(user) -> str:
    """Имя из объекта Telegram: имя + фамилия, иначе @username, иначе ID."""
    if user is None:
        return "Неизвестный"
    full = " ".join(x for x in (user.first_name, getattr(user, "last_name", None)) if x)
    return full or (f"@{user.username}" if user.username else str(user.id))


async def _target_mention(message: Message, target: int) -> str:
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == target:
        return mention(target, _display_name(reply.from_user))
    return await _name(target)


def split_command(text: str) -> tuple[list[str], str | None]:
    """Первая строка — команда с аргументами, остальные строки — причина."""
    first, _, rest = (text or "").partition("\n")
    return first.split(), (rest.strip() or None)


def format_mute_message(target_html: str, moderator_html: str, seconds: int, reason: str | None) -> str:
    lines = [f"🔇 {target_html} лишается права слова на {human_duration(seconds)}"]
    if reason:
        lines.append(f"💬 Причина: {escape(reason, quote=True)}")
        lines.append(f"🦸 Модератор: {moderator_html}")
    else:
        lines.append(f"👺 Модератор: {moderator_html}")
    return "\n".join(lines)


def format_unmute_message(target_html: str) -> str:
    return (f"✅ Пользователю {target_html} вернули право слова. "
            "Можете свободно общаться! Но лучше следите за языком…")


@router.message(F.text.regexp(r"(?i)^мут(\s|$)"))
async def cmd_mute(message: Message):
    res = await _prepare(message, "moderator", first_line=True)
    if not res:
        return
    target, _ = res
    words, extra_reason = split_command(message.text)
    args = words[1:]
    if not message.reply_to_message and args:
        args = args[1:]  # первый аргумент — цель
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
        await message.reply(f"❌ Не удалось выдать мут (проверьте, что бот — администратор с правом ограничивать участников): {escape(str(getattr(e, 'message', '') or e or 'неизвестная ошибка'), quote=True)}")
        return
    try:
        await _deactivate(message.chat.id, target, "mute")  # повторный мут заменяет прежний
        await _add_punishment(message.chat.id, target, "mute", reason, message.from_user.id, time.time() + sec)
        await db.log_action(message.chat.id, message.from_user.id, "mute", target, f"{sec}с: {reason or '—'}")
    except Exception:
        logger.exception("Мут выдан в Telegram, но не записан в базу")
        await message.reply("⚠️ Мут выдан в Telegram, но не сохранён в истории бота (ошибка базы).")
    moderator = mention(message.from_user.id, _display_name(message.from_user))
    await message.answer(format_mute_message(await _target_mention(message, target), moderator, sec, reason))


@router.message(F.text.regexp(r"(?i)^проверить мут(\s|$)"))
async def cmd_check_mute(message: Message):
    """Показывает известный боту статус мута и оставшийся срок."""
    if not message.from_user:
        await message.reply("Проверка доступна только обычным аккаунтам.")
        return
    actor = await effective_role(message.bot, message.chat.id, message.from_user.id)
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
        "SELECT reason, until FROM punishments WHERE chat_id=? AND user_id=? AND type='mute' AND active=1 ORDER BY id DESC LIMIT 1",
        (message.chat.id, target),
    )
    row = await cur.fetchone()
    telegram_muted = False
    try:
        member = await message.bot.get_chat_member(message.chat.id, target)
        telegram_muted = member.status == "restricted" and not getattr(member, "can_send_messages", True)
    except TelegramAPIError:
        # База всё ещё может содержать достоверную запись, даже если Telegram API
        # временно недоступен или участник покинул группу.
        pass
    if row and row["until"] is not None and float(row["until"]) <= time.time():
        await conn.execute("UPDATE punishments SET active=0 WHERE chat_id=? AND user_id=? AND type='mute' AND active=1 AND until<=?", (message.chat.id, target, time.time()))
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
        await message.answer(f"🔇 {await _name(target)} в муте. Осталось: {duration}. До: {expiry}. Причина: {escape(str(row['reason'] or 'не указана'), quote=True)}")
    else:
        await message.answer(f"🔇 {await _name(target)} ограничен в отправке сообщений в Telegram. В базе нет срока окончания.")


@router.message(F.text.regexp(r"(?i)^(размут|снять мут|анмут|говори)(\s|$)"))
async def cmd_unmute(message: Message):
    skip = 2 if message.text.lower().startswith("снять мут") else 1
    res = await _prepare(message, "moderator", skip=skip)
    if not res:
        return
    target, _ = res
    try:
        await message.bot.restrict_chat_member(
            message.chat.id, target,
            unmute_permissions(),
        )
    except TelegramAPIError as e:
        await message.reply(f"Не удалось размутить: {escape(str(getattr(e, 'message', '') or e or 'неизвестная ошибка'), quote=True)}")
        return
    try:
        await _deactivate(message.chat.id, target, "mute")
        await db.log_action(message.chat.id, message.from_user.id, "unmute", target)
    except Exception:
        logger.exception("Размут выполнен, но не записан в базу")
    await message.answer(format_unmute_message(await _target_mention(message, target)))


# ---------- варны ----------

@router.message(F.text.regexp(r"(?i)^варн(\s|$)"))
async def cmd_warn(message: Message):
    res = await _prepare(message, "moderator")
    if not res:
        return
    target, args = res
    reason = " ".join(args) or "без причины"
    await _add_punishment(message.chat.id, target, "warn", reason, message.from_user.id)
    await db.log_action(message.chat.id, message.from_user.id, "warn", target, reason)
    count = await count_warns(message.chat.id, target)
    text = f"⚠️ {await _name(target)} получил варн ({count}/{MAX_WARNS}).\nПричина: {escape(reason, quote=True)}"
    if count >= MAX_WARNS:
        try:
            await do_mute(message.bot, message.chat.id, target, AUTO_MUTE_SEC)
            await _deactivate(message.chat.id, target, "warn")
            await _add_punishment(
                message.chat.id, target, "mute", "автоматический мут за лимит варнов",
                message.bot.id, time.time() + AUTO_MUTE_SEC,
            )
            await db.log_action(
                message.chat.id, message.bot.id, "auto_mute", target,
                f"{MAX_WARNS} варна; срок {AUTO_MUTE_SEC}с",
            )
            text += f"\n🔇 Достигнут лимит — мут на {format_duration(AUTO_MUTE_SEC)}, варны сброшены."
        except TelegramAPIError:
            text += "\n(Не удалось выдать автомут — сделайте бота админом.)"
    await message.answer(text)


async def count_warns(chat_id, user_id) -> int:
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT COUNT(*) c FROM punishments WHERE chat_id=? AND user_id=? AND type='warn' AND active=1",
        (chat_id, user_id),
    )
    return (await cur.fetchone())["c"]


@router.message(F.text.regexp(r"(?i)^мои варны$"))
async def cmd_my_warns(message: Message):
    if not message.from_user:
        return
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT reason,created_at FROM punishments WHERE chat_id=? AND user_id=? AND type='warn' AND active=1 ORDER BY id DESC LIMIT 20",
        (message.chat.id, message.from_user.id),
    )
    rows = await cur.fetchall()
    if not rows:
        await message.answer("✅ У вас нет активных варнов.")
        return
    lines = [f"{i}. {escape(str(row['reason'] or 'без причины'), quote=True)} — {datetime.fromtimestamp(row['created_at']):%d.%m %H:%M}" for i, row in enumerate(rows, 1)]
    await message.answer(f"⚠️ Ваши варны ({len(rows)}):\n" + "\n".join(lines))


@router.message(F.text.regexp(r"(?i)^варнлист$"))
async def cmd_warn_list(message: Message):
    if not message.from_user or not has_at_least(await effective_role(message.bot, message.chat.id, message.from_user.id), "moderator"):
        await message.reply("⛔ Варнлист доступен модераторам и выше.")
        return
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT user_id,reason,created_at FROM punishments WHERE chat_id=? AND type='warn' AND active=1 ORDER BY id DESC LIMIT 20",
        (message.chat.id,),
    )
    rows = await cur.fetchall()
    if not rows:
        await message.answer("В активном варнлисте пусто.")
        return
    lines = [f"• {mention(row['user_id'], str(row['user_id']))}: {escape(str(row['reason'] or 'без причины'), quote=True)} — {datetime.fromtimestamp(row['created_at']):%d.%m %H:%M}" for row in rows]
    await message.answer("⚠️ <b>Активный варнлист:</b>\n" + "\n".join(lines), parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)^снять все варны(\s|$)"))
async def cmd_unwarn_all(message: Message):
    res = await _prepare(message, "moderator", skip=3)
    if not res:
        return
    target, _ = res
    conn = await db.get_db()
    cur = await conn.execute(
        "UPDATE punishments SET active=0 WHERE chat_id=? AND user_id=? AND type='warn' AND active=1",
        (message.chat.id, target),
    )
    await conn.commit()
    if cur.rowcount:
        await db.log_action(message.chat.id, message.from_user.id, "unwarn_all", target, str(cur.rowcount))
        await message.answer(f"✅ Снято варнов: {cur.rowcount}. Пользователь: {await _name(target)}.")
    else:
        await message.answer("У этого пользователя нет активных варнов.")


@router.message(F.text.regexp(r"(?i)^муты$"))
async def cmd_mute_list(message: Message):
    if not message.from_user or not has_at_least(await effective_role(message.bot, message.chat.id, message.from_user.id), "moderator"):
        await message.reply("⛔ Список мутов доступен модераторам и выше.")
        return
    now = time.time()
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT user_id,reason,until FROM punishments WHERE chat_id=? AND type='mute' AND active=1 AND (until IS NULL OR until>?) ORDER BY id DESC LIMIT 20",
        (message.chat.id, now),
    )
    rows = await cur.fetchall()
    if not rows:
        await message.answer("Активных мутов нет.")
        return
    lines = []
    for row in rows:
        if row["until"] is None:
            expiry = "без срока в базе"
        else:
            expiry = datetime.fromtimestamp(row["until"]).strftime("до %d.%m %H:%M")
        lines.append(f"• {await _name(row['user_id'])} — {escape(str(row['reason'] or 'без причины'), quote=True)} ({expiry})")
    await message.answer("🔇 <b>Активные муты:</b>\n" + "\n".join(lines), parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)^(банлист|список банов|баны)$"))
async def cmd_ban_list(message: Message):
    if not message.from_user or not has_at_least(await effective_role(message.bot, message.chat.id, message.from_user.id), "moderator"):
        await message.reply("⛔ Банлист доступен модераторам и выше.")
        return
    now = time.time()
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT user_id,reason,until FROM punishments WHERE chat_id=? AND type='ban' AND active=1 AND (until IS NULL OR until>?) ORDER BY id DESC LIMIT 20",
        (message.chat.id, now),
    )
    rows = await cur.fetchall()
    if not rows:
        await message.answer("Активных банов в базе нет.")
        return
    lines = []
    for row in rows:
        expiry = "навсегда" if row["until"] is None else datetime.fromtimestamp(row["until"]).strftime("до %d.%m %H:%M")
        lines.append(f"• {await _name(row['user_id'])} — {escape(str(row['reason'] or 'без причины'), quote=True)} ({expiry})")
    await message.answer("🚫 <b>Активный банлист бота:</b>\n" + "\n".join(lines), parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)^причина(\s|$)"))
async def cmd_punishment_reason(message: Message):
    if not message.from_user or not has_at_least(await effective_role(message.bot, message.chat.id, message.from_user.id), "moderator"):
        await message.reply("⛔ Смотреть причины наказаний могут модераторы и выше.")
        return
    target, err = await resolve_target(message, message.text.split()[1:], db)
    if err:
        await message.reply(err)
        return
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT type,reason,by_id,created_at,until FROM punishments WHERE chat_id=? AND user_id=? AND active=1 ORDER BY id DESC LIMIT 1",
        (message.chat.id, target),
    )
    row = await cur.fetchone()
    if not row:
        await message.answer(f"У {await _name(target)} нет активных наказаний в базе.")
        return
    by = await db.get_user(int(row["by_id"])) if row["by_id"] else None
    by_name = by["first_name"] if by else str(row["by_id"] or "неизвестно")
    until = "без срока" if row["until"] is None else datetime.fromtimestamp(row["until"]).strftime("%d.%m.%Y %H:%M")
    await message.answer(
        f"📋 <b>Последнее наказание:</b> {escape(str(row['type']), quote=True)}\n"
        f"👤 Пользователь: {await _name(target)}\n"
        f"🛡 Выдал: {mention(int(row['by_id']), by_name) if row['by_id'] else escape(by_name, quote=True)}\n"
        f"🕒 Дата: {datetime.fromtimestamp(row['created_at']):%d.%m.%Y %H:%M}\n"
        f"⌛ До: {until}\nПричина: {escape(str(row['reason'] or 'не указана'), quote=True)}",
        parse_mode="HTML",
    )


@router.message(F.text.regexp(r"(?i)^варны(\s|$)"))
async def cmd_warns(message: Message):
    args = message.text.split()[1:]
    target, err = await resolve_target(message, args, db)
    if err:
        if args:
            await message.reply(err)
            return
        target = message.from_user.id
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT reason, created_at FROM punishments WHERE chat_id=? AND user_id=? AND type='warn' AND active=1 ORDER BY id",
        (message.chat.id, target),
    )
    rows = await cur.fetchall()
    if not rows:
        await message.answer(f"✅ У {await _name(target)} нет активных варнов.")
        return
    lines = [f"{i}. {escape(str(r['reason'] or ''), quote=True)} ({datetime.fromtimestamp(r['created_at']):%d.%m %H:%M})"
             for i, r in enumerate(rows, 1)]
    await message.answer(f"⚠️ Варны {await _name(target)} ({len(rows)}/{MAX_WARNS}):\n" + "\n".join(lines))


@router.message(F.text.regexp(r"(?i)^снять варн(\s|$)"))
async def cmd_unwarn(message: Message):
    res = await _prepare(message, "moderator", skip=2)
    if not res:
        return
    target, _ = res
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT id FROM punishments WHERE chat_id=? AND user_id=? AND type='warn' AND active=1 ORDER BY id DESC LIMIT 1",
        (message.chat.id, target),
    )
    row = await cur.fetchone()
    if not row:
        await message.reply("Активных варнов нет.")
        return
    await conn.execute("UPDATE punishments SET active=0 WHERE id=?", (row["id"],))
    await conn.commit()
    await db.log_action(message.chat.id, message.from_user.id, "unwarn", target)
    await message.answer(f"✅ Снят последний варн с {await _name(target)}.")


# ---------- бан / кик ----------

def _merge_reason(reason: str | None, extra: str | None) -> str | None:
    if extra:
        reason = f"{reason} {extra}" if reason else extra
    if reason and len(reason) > 500:
        reason = reason[:500] + "…"
    return reason


def _tg_error(e: Exception) -> str:
    return escape(str(getattr(e, "message", "") or e or "неизвестная ошибка"), quote=True)


def format_ban_message(target_html: str, moderator_html: str, seconds: int | None, reason: str | None) -> str:
    term = f"на {human_duration(seconds)}" if seconds else "навсегда"
    lines = [f"🚫 {target_html} заблокирован в чате {term}"]
    if reason:
        lines.append(f"💬 Причина: {escape(reason, quote=True)}")
    lines.append(f"🦸 Модератор: {moderator_html}")
    return "\n".join(lines)


def format_unban_message(target_html: str) -> str:
    return f"✅ {target_html} разблокирован и может снова вступить в чат."


def format_kick_message(target_html: str, moderator_html: str, reason: str | None) -> str:
    lines = [f"👢 {target_html} исключён из чата"]
    if reason:
        lines.append(f"💬 Причина: {escape(reason, quote=True)}")
    lines.append(f"🦸 Модератор: {moderator_html}")
    return "\n".join(lines)


def _rest_args(message: Message) -> tuple[list[str], str | None]:
    words, extra = split_command(message.text)
    args = words[1:]
    if not message.reply_to_message and args:
        args = args[1:]  # первый аргумент — цель
    return args, extra


@router.message(F.text.regexp(r"(?i)^(бан|забанить)(\s|$)"))
async def cmd_ban(message: Message):
    res = await _prepare(message, "admin", first_line=True)
    if not res:
        return
    target, _ = res
    args, extra = _rest_args(message)
    sec, reason = _split_time(args)
    if sec == -1:
        await message.reply(reason)
        return
    reason = _merge_reason(reason, extra)
    until = datetime.now(timezone.utc) + timedelta(seconds=sec) if sec else None
    try:
        await message.bot.ban_chat_member(message.chat.id, target, until_date=until)
    except TelegramAPIError as e:
        await message.reply(f"❌ Не удалось заблокировать (бот должен быть администратором с правом блокировать участников): {_tg_error(e)}")
        return
    try:
        await _deactivate(message.chat.id, target, "ban")  # повторный бан заменяет прежний
        await _add_punishment(message.chat.id, target, "ban", reason, message.from_user.id,
                              time.time() + sec if sec else None)
        await db.log_action(message.chat.id, message.from_user.id, "ban", target, f"{sec or 'навсегда'}: {reason or '—'}")
    except Exception:
        logger.exception("Бан выдан в Telegram, но не записан в базу")
        await message.reply("⚠️ Бан выдан в Telegram, но не сохранён в истории бота (ошибка базы).")
    moderator = mention(message.from_user.id, _display_name(message.from_user))
    await message.answer(format_ban_message(await _target_mention(message, target), moderator, sec, reason))


@router.message(F.text.regexp(r"(?i)^(разбан|разбанить|анбан)(\s|$)"))
async def cmd_unban(message: Message):
    res = await _prepare(message, "admin", first_line=True)
    if not res:
        return
    target, _ = res
    try:
        member = await message.bot.get_chat_member(message.chat.id, target)
        if member.status != "kicked":
            await _deactivate(message.chat.id, target, "ban")
            await message.reply(f"ℹ️ {await _target_mention(message, target)} не заблокирован в этом чате.")
            return
    except TelegramAPIError:
        pass  # статус неизвестен — пробуем снять бан в любом случае
    try:
        await message.bot.unban_chat_member(message.chat.id, target, only_if_banned=True)
    except TelegramAPIError as e:
        await message.reply(f"❌ Не удалось разблокировать: {_tg_error(e)}")
        return
    try:
        await _deactivate(message.chat.id, target, "ban")
        await db.log_action(message.chat.id, message.from_user.id, "unban", target)
    except Exception:
        logger.exception("Разбан выполнен, но не записан в базу")
    await message.answer(format_unban_message(await _target_mention(message, target)))


@router.message(F.text.regexp(r"(?i)^(кик|кикнуть|выгнать)(\s|$)"))
async def cmd_kick(message: Message):
    res = await _prepare(message, "moderator", first_line=True)
    if not res:
        return
    target, _ = res
    args, extra = _rest_args(message)
    reason = _merge_reason(" ".join(args) or None, extra)
    try:
        member = await message.bot.get_chat_member(message.chat.id, target)
        if member.status in {"left", "kicked"}:
            await message.reply(f"ℹ️ {await _target_mention(message, target)} уже не состоит в чате.")
            return
    except TelegramAPIError:
        pass
    try:
        await message.bot.ban_chat_member(message.chat.id, target)
    except TelegramAPIError as e:
        await message.reply(f"❌ Не удалось исключить (бот должен быть администратором с правом блокировать участников): {_tg_error(e)}")
        return
    try:
        await message.bot.unban_chat_member(message.chat.id, target, only_if_banned=True)
    except TelegramAPIError:
        logger.exception("Кик: не удалось снять временную блокировку")
        await message.reply("⚠️ Участник исключён, но остался заблокирован. Снимите блокировку командой «разбан».")
    try:
        await db.log_action(message.chat.id, message.from_user.id, "kick", target, reason)
    except Exception:
        logger.exception("Кик выполнен, но не записан в базу")
    moderator = mention(message.from_user.id, _display_name(message.from_user))
    await message.answer(format_kick_message(await _target_mention(message, target), moderator, reason))


# ---------- роли и журнал ----------

@router.message(F.text.regexp(r"(?i)^роль(\s|$)"))
async def cmd_role(message: Message):
    if not message.from_user:
        await message.reply("Назначать роли можно только обычным аккаунтам.")
        return
    args = message.text.split()[1:]
    actor = await effective_role(message.bot, message.chat.id, message.from_user.id)
    if not has_at_least(actor, "admin"):
        await message.reply("⛔ Назначать роли могут только администраторы.")
        return
    target, err = await resolve_target(message, args, db)
    if err:
        await message.reply(err)
        return
    if not message.reply_to_message:
        args = args[1:]
    role = ROLE_ALIASES.get(args[0].lower()) if args else None
    if not role:
        await message.reply("Укажите роль: админ, модератор, агент или участник.")
        return
    target_role = await effective_role(message.bot, message.chat.id, target)
    if ROLE_LEVELS[role] >= ROLE_LEVELS[actor] or not can_act_on(actor, target_role):
        await message.reply("⛔ Нельзя выдать роль равную или выше своей.")
        return
    await db.set_role(message.chat.id, target, role)
    await db.log_action(message.chat.id, message.from_user.id, "role", target, role)
    await message.answer(f"🎖 {await _name(target)} теперь: {ROLE_NAMES[role]}.")


@router.message(F.text.regexp(r"(?i)^(журнал|лог)$"))
async def cmd_log(message: Message):
    actor = await effective_role(message.bot, message.chat.id, message.from_user.id)
    if not has_at_least(actor, "moderator"):
        await message.reply("⛔ Журнал доступен модераторам и выше.")
        return
    rows = await db.get_log(message.chat.id, 15)
    if not rows:
        await message.answer("Журнал пуст.")
        return
    lines = [
        f"{datetime.fromtimestamp(r['created_at']):%d.%m %H:%M} — {r['action']} "
        f"→ {r['target_id']} {escape(str(r['details'] or ''), quote=True)}".strip()
        for r in rows
    ]
    await message.answer("📜 <b>Последние действия:</b>\n" + "\n".join(lines))
