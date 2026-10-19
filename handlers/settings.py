"""Настройки групп, переключатели модулей и приветствия новых участников."""
from html import escape

from aiogram import Router, F
from aiogram.types import Message

import database as db
from utils import has_at_least, mention
from handlers.moderation import effective_role

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

SETTING_ALIASES = {
    "антиспам": "antispam",
    "приветствия": "greetings",
    "приветствие": "greetings",
    "заметки": "notes",
    "автоответы": "autoreplies",
    "статистика": "count_messages",
    "сообщения": "count_messages",
    "экономика": "economy",
    "браки": "marriages",
    "кланы": "clans",
    "игры": "games",
}
SETTING_LABELS = {
    "antispam": "Антиспам",
    "greetings": "Приветствия",
    "notes": "Заметки",
    "autoreplies": "Автоответы",
    "count_messages": "Статистика сообщений и опыт",
    "economy": "Экономика",
    "marriages": "Браки",
    "clans": "Кланы",
    "games": "Игры",
}


async def _is_admin(message: Message) -> bool:
    if not message.from_user:
        return False
    role = await effective_role(message.bot, message.chat.id, message.from_user.id)
    return has_at_least(role, "admin")


@router.message(F.text.regexp(r"(?i)^настройки$"))
async def show_settings(message: Message):
    settings = await db.get_settings(message.chat.id)
    lines = ["⚙️ Настройки этой группы:"]
    for key, title in SETTING_LABELS.items():
        lines.append(f"{'🟢' if settings.get(key, False) else '🔴'} {title}: {'вкл' if settings.get(key, False) else 'выкл'}")
    action = settings.get("antispam_action", "warn")
    lines.append(f"Антиспам-действие: {escape(str(action))}")
    lines.append("Изменение: настройка <модуль> вкл/выкл")
    lines.append("Действие антиспама: настройка антиспам действие warn/delete/mute/case")
    await message.answer("\n".join(lines))


@router.message(F.text.regexp(r"(?i)^настройка\s+\S+\s+(вкл|выкл|включить|выключить)$"))
async def toggle_setting(message: Message):
    if not await _is_admin(message):
        await message.reply("⛔ Менять настройки могут администраторы группы и выше.")
        return
    parts = message.text.split()
    raw_key = parts[1].lower()
    key = SETTING_ALIASES.get(raw_key)
    if not key:
        await message.reply("Неизвестный модуль. Доступно: антиспам, приветствия, заметки, автоответы, статистика, экономика, браки, кланы, игры.")
        return
    enabled = parts[2].lower() in {"вкл", "включить"}
    await db.set_setting(message.chat.id, key, enabled)
    await db.log_action(message.chat.id, message.from_user.id, "setting_toggle", None, f"{key}={'on' if enabled else 'off'}")
    await message.answer(f"{'🟢' if enabled else '🔴'} {SETTING_LABELS[key]} {'включены' if enabled else 'выключены'}.")


@router.message(F.text.regexp(r"(?i)^настройка\s+антиспам\s+действие\s+(warn|delete|mute|case)$"))
async def set_antispam_action(message: Message):
    if not await _is_admin(message):
        await message.reply("⛔ Менять настройки могут администраторы группы и выше.")
        return
    action = message.text.split()[-1].lower()
    await db.set_setting(message.chat.id, "antispam_action", action)
    await db.log_action(message.chat.id, message.from_user.id, "antispam_action", None, action)
    labels = {"warn": "варн", "delete": "удаление сообщения", "mute": "мут", "case": "создание дела"}
    await message.answer(f"⚙️ Действие антиспама изменено: {labels[action]}.")


@router.message(F.text.regexp(r"(?i)^приветствие текст\s+"))
async def set_welcome_text(message: Message):
    if not await _is_admin(message):
        await message.reply("⛔ Менять приветствие могут администраторы группы и выше.")
        return
    parts = message.text.split(maxsplit=2)
    template = parts[2].strip() if len(parts) > 2 else ""
    if not template:
        await message.reply("Укажите текст приветствия. Переменная {name} заменяется именем нового участника.")
        return
    if len(template) > 500:
        await message.reply("Текст приветствия слишком длинный: максимум 500 символов.")
        return
    await db.set_setting(message.chat.id, "welcome_text", template)
    await db.log_action(message.chat.id, message.from_user.id, "welcome_text", None, "updated")
    await message.answer("✅ Приветствие сохранено. Переменная {name} будет заменена именем участника.")


@router.message(F.new_chat_members)
async def welcome_new_members(message: Message):
    settings = await db.get_settings(message.chat.id)
    if not settings.get("greetings", True):
        return
    template = str(settings.get("welcome_text", "Добро пожаловать, {name}!"))[:500]
    for user in message.new_chat_members:
        if user.is_bot:
            continue
        await db.upsert_user(user.id, user.username, user.first_name)
        text = escape(template, quote=True).replace("{name}", mention(user.id, user.first_name))
        await message.answer(text, parse_mode="HTML")
