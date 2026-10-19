"""Премиум-статус группы: ручная выдача администраторами, без платёжных обещаний."""

from datetime import datetime
from html import escape

from aiogram import Router, F
from aiogram.types import Message

import database as db
from handlers.moderation import effective_role
from utils import has_at_least, can_act_on, parse_duration, resolve_target, mention

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


async def _has_from_user(message: Message) -> bool:
    """Не обрабатывает команды, отправленные от имени канала без обычного пользователя."""
    return message.from_user is not None


router.message.filter(_has_from_user)


@router.message(F.text.regexp(r"(?i)^премиум(?: статус)?$") )
async def premium_self_status(message: Message):
    if not message.from_user:
        return
    expires = await db.premium_expiry(message.chat.id, message.from_user.id)
    if not expires:
        await message.answer("💎 У вас нет активного премиум-статуса в этой группе.")
        return
    until = datetime.fromtimestamp(expires).strftime("%d.%m.%Y %H:%M")
    await message.answer(f"💎 Премиум активен до <b>{until}</b>.")


@router.message(F.text.regexp(r"(?i)^премиум статус\s+"))
async def premium_other_status(message: Message):
    if not message.from_user:
        return
    parts = message.text.split()[2:]
    target_id, err = await resolve_target(message, parts, db)
    if err:
        await message.reply(err)
        return
    expires = await db.premium_expiry(message.chat.id, target_id)
    user = await db.get_user(target_id)
    if not expires:
        await message.answer(f"У {mention(target_id, user['first_name'] if user else None)} нет активного премиум-статуса.")
        return
    until = datetime.fromtimestamp(expires).strftime("%d.%m.%Y %H:%M")
    await message.answer(f"💎 Премиум у {mention(target_id, user['first_name'] if user else None)} активен до <b>{until}</b>.", parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)^статус$"))
async def premium_profile_status_view(message: Message):
    if not message.from_user:
        return
    status_text = await db.premium_profile_status(message.chat.id, message.from_user.id)
    if status_text is None:
        await message.reply("💎 Пользовательский статус доступен только с активным премиумом.")
    elif not status_text:
        await message.answer("Ваш премиум-статус пока не задан. Установить: статус установить &lt;текст&gt;")
    else:
        await message.answer(f"✨ Ваш статус: {escape(status_text, quote=True)}")


@router.message(F.text.regexp(r"(?i)^статус установить\s+"))
async def premium_profile_status_set(message: Message):
    if not message.from_user:
        return
    parts = message.text.split(maxsplit=2)
    status_text = parts[2].strip() if len(parts) > 2 else ""
    if not status_text:
        await message.reply("Укажите текст: статус установить Люблю космос")
        return
    if len(status_text) > 80:
        await message.reply("Статус не должен превышать 80 символов.")
        return
    if await db.premium_profile_status(message.chat.id, message.from_user.id) is None:
        await message.reply("💎 Установить пользовательский статус можно только с активным премиумом.")
        return
    if not await db.set_premium_profile_status(message.chat.id, message.from_user.id, status_text):
        await message.reply("Премиум истёк. Обновите статус после продления.")
        return
    await message.answer(f"✨ Премиум-статус профиля сохранён: {escape(status_text, quote=True)}")


@router.message(F.text.regexp(r"(?i)^премиум выдать\s+"))
async def premium_grant(message: Message):
    if not message.from_user:
        return
    actor_role = await effective_role(message.bot, message.chat.id, message.from_user.id)
    if not has_at_least(actor_role, "admin"):
        await message.reply("⛔ Выдавать премиум могут администраторы группы и выше.")
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("Формат: премиум выдать @user 30д (максимум 365 дней за раз).")
        return
    duration = parse_duration(parts[-1])
    if not duration:
        await message.reply("Укажите срок, например 30д или 12ч (максимум 365 дней).")
        return
    target_args = parts[2:-1]
    target_id, err = await resolve_target(message, target_args, db)
    if err:
        await message.reply(err)
        return
    target_role = await effective_role(message.bot, message.chat.id, target_id)
    if not can_act_on(actor_role, target_role):
        await message.reply("Нельзя менять премиум-статус участнику с равной или более высокой ролью.")
        return
    expires = await db.grant_premium(message.chat.id, target_id, duration, message.from_user.id)
    await db.log_action(message.chat.id, message.from_user.id, "premium_grant", target_id, f"{duration}s")
    user = await db.get_user(target_id)
    until = datetime.fromtimestamp(expires).strftime("%d.%m.%Y %H:%M")
    await message.answer(f"💎 Премиум выдан {mention(target_id, user['first_name'] if user else None)} до <b>{until}</b>.", parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)^премиум снять\s+"))
async def premium_remove(message: Message):
    if not message.from_user:
        return
    actor_role = await effective_role(message.bot, message.chat.id, message.from_user.id)
    if not has_at_least(actor_role, "admin"):
        await message.reply("⛔ Снимать премиум могут администраторы группы и выше.")
        return
    target_args = message.text.split()[2:]
    target_id, err = await resolve_target(message, target_args, db)
    if err:
        await message.reply(err)
        return
    target_role = await effective_role(message.bot, message.chat.id, target_id)
    if not can_act_on(actor_role, target_role):
        await message.reply("Нельзя менять премиум-статус участнику с равной или более высокой ролью.")
        return
    removed = await db.remove_premium(message.chat.id, target_id)
    if removed:
        await db.log_action(message.chat.id, message.from_user.id, "premium_remove", target_id, "premium removed")
        await message.answer(f"Премиум-статус пользователя {target_id} снят.")
    else:
        await message.reply("У этого пользователя нет премиум-статуса в этой группе.")
