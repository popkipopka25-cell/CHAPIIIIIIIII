"""Автоответы группы: триггер => ответ, хранение в SQLite."""
import time
from html import escape

from aiogram import Router, F
from aiogram.types import Message
from aiogram.dispatcher.event.bases import UNHANDLED

import database as db
from handlers.moderation import effective_role
from utils import has_at_least, mention

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


async def _has_from_user(message: Message) -> bool:
    """Не обрабатывает команды, отправленные от имени канала без обычного пользователя."""
    return message.from_user is not None


router.message.filter(_has_from_user)

# Автоответ не должен перехватывать команды других модулей.
_RESERVED_COMMANDS = {
    "помощь", "help", "команды", "админ панель", "профиль", "мой профиль", "мой id", "мой айди",
    "кто админ", "админы группы", "администраторы группы", "инфо группы", "информация о группе",
    "жалоба", "мут", "муты", "проверить мут", "размут", "варн", "варны", "мои варны", "варнлист",
    "снять варн", "снять все варны", "бан", "банлист", "причина", "разбан", "кик", "роль", "журнал",
    "настройки", "настройка", "приветствие", "заметка", "заметки", "автоответ",
    "автоответы", "спам", "пожаловаться", "баланс", "бонус", "ежедневная",
    "ежедневный", "перевести", "перевод", "топ", "магазин", "купить", "инвентарь",
    "подарить", "задания", "достижения", "история", "премиум", "статус", "выдать", "забрать", "товар",
    "брак", "принять брак", "отклонить брак", "мой брак", "брак статус", "развод",
    "восстановить брак", "принять восстановление брака", "отклонить восстановление брака",
    "создать клан", "мой клан", "клан", "участники клана", "вступить в клан", "подать заявку в клан",
    "выйти из клана", "заявки клана", "принять заявку", "отклонить заявку", "описание клана",
    "клан инфо", "список кланов", "кланы", "переименовать клан", "удалить клан", "пригласить в клан",
    "мои приглашения", "принять приглашение", "отклонить приглашение", "назначить зама", "снять зама",
    "топ кланов", "статистика клана", "моя заявка в клан", "роль в клане", "поиск клана",
    "очистить заявки клана", "отменить приглашения клана", "дела", "дело", "обжалование",
    "орёл", "орел", "монетка", "кости", "кубик", "случайное число",
}


def _is_reserved_command(text: str) -> bool:
    normalized = text.strip().casefold()
    if normalized.startswith("/"):
        return True
    return any(normalized == command or normalized.startswith(command + " ") for command in _RESERVED_COMMANDS)


async def _enabled(message: Message) -> bool:
    return bool((await db.get_settings(message.chat.id)).get("autoreplies", True))


async def _can_manage(message: Message) -> bool:
    if not message.from_user:
        return False
    role = await effective_role(message.bot, message.chat.id, message.from_user.id)
    return has_at_least(role, "moderator")


@router.message(F.text.regexp(r"(?i)^автоответ добавить\s+"))
async def add_autoreply(message: Message):
    if not await _enabled(message):
        await message.reply("Автоответы в этой группе выключены.")
        return
    if not await _can_manage(message):
        await message.reply("⛔ Добавлять автоответы могут модераторы и выше.")
        return
    payload = message.text.split(maxsplit=2)[2] if len(message.text.split(maxsplit=2)) > 2 else ""
    if "=>" not in payload:
        await message.reply("Формат: автоответ добавить привет => Привет! Чем помочь?")
        return
    trigger, response = (part.strip() for part in payload.split("=>", 1))
    if not trigger or not response:
        await message.reply("Триггер и ответ не должны быть пустыми.")
        return
    if len(trigger) > 64 or len(response) > 500:
        await message.reply("Максимум: 64 символа для триггера и 500 для ответа.")
        return
    if _is_reserved_command(trigger):
        await message.reply("Нельзя использовать команду бота или модерации как триггер автоответа.")
        return
    conn = await db.get_db()
    try:
        await conn.execute(
            "INSERT INTO autoreplies(chat_id,trigger_key,trigger_text,response,created_by,created_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(chat_id,trigger_key) DO UPDATE SET trigger_text=excluded.trigger_text, response=excluded.response, created_by=excluded.created_by, created_at=excluded.created_at",
            (message.chat.id, trigger.casefold(), trigger, response, message.from_user.id, time.time()),
        )
        await conn.commit()
    except Exception:
        await conn.rollback()
        await message.reply("Не удалось сохранить автоответ. Попробуйте ещё раз.")
        return
    await db.log_action(message.chat.id, message.from_user.id, "autoreply_set", None, trigger)
    await message.answer(f"✅ Автоответ для «{escape(trigger, quote=True)}» сохранён.")


@router.message(F.text.regexp(r"(?i)^автоответ удалить\s+"))
async def delete_autoreply(message: Message):
    if not await _enabled(message):
        await message.reply("Автоответы в этой группе выключены.")
        return
    if not await _can_manage(message):
        await message.reply("⛔ Удалять автоответы могут модераторы и выше.")
        return
    trigger = message.text.split(maxsplit=2)[2].strip() if len(message.text.split(maxsplit=2)) > 2 else ""
    if not trigger:
        await message.reply("Укажите триггер: автоответ удалить привет")
        return
    conn = await db.get_db()
    cur = await conn.execute("DELETE FROM autoreplies WHERE chat_id=? AND trigger_key=?", (message.chat.id, trigger.casefold()))
    await conn.commit()
    if cur.rowcount:
        await db.log_action(message.chat.id, message.from_user.id, "autoreply_delete", None, trigger)
        await message.answer(f"🗑 Автоответ «{escape(trigger, quote=True)}» удалён.")
    else:
        await message.reply("Такого автоответа нет.")


@router.message(F.text.regexp(r"(?i)^автоответы$"))
async def list_autoreplies(message: Message):
    if not await _enabled(message):
        await message.reply("Автоответы в этой группе выключены.")
        return
    conn = await db.get_db()
    cur = await conn.execute("SELECT trigger_text FROM autoreplies WHERE chat_id=? ORDER BY trigger_key LIMIT 40", (message.chat.id,))
    rows = await cur.fetchall()
    if not rows:
        await message.answer("Автоответов пока нет.")
        return
    await message.answer("🤖 <b>Автоответы:</b>\n" + "\n".join(f"• {escape(row['trigger_text'], quote=True)}" for row in rows))


@router.message(F.text)
async def match_autoreply(message: Message):
    if not message.from_user or message.from_user.is_bot or not message.text or not await _enabled(message):
        return UNHANDLED
    normalized = message.text.strip().casefold()
    # Не отвечаем на управляющие команды и слишком длинные сообщения.
    if not normalized or len(normalized) > 500 or _is_reserved_command(normalized):
        return UNHANDLED
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT trigger_text,response FROM autoreplies WHERE chat_id=? AND trigger_key=? LIMIT 1",
        (message.chat.id, normalized),
    )
    row = await cur.fetchone()
    if not row:
        return UNHANDLED
    response = escape(str(row["response"]), quote=True)
    response = response.replace("{name}", mention(message.from_user.id, message.from_user.first_name))
    await message.answer(response, parse_mode="HTML")
