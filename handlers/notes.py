"""Заметки: сохранённые тексты группы. Создавать/удалять могут модераторы, читать — все."""
from html import escape
from aiogram import Router, F
from aiogram.types import Message

import database as db
from utils import has_at_least
from handlers.moderation import effective_role

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


async def _notes_module_enabled(message: Message) -> bool:
    """Проверяет настройки только для команд заметок, а не для каждого сообщения чата."""
    text = (message.text or "").strip().casefold()
    if not text.startswith(("заметка", "заметки")):
        return True
    return bool((await db.get_settings(message.chat.id)).get("notes", True))


router.message.filter(_notes_module_enabled)


async def _can_edit(message: Message) -> bool:
    if not message.from_user:
        return False
    role = await effective_role(message.bot, message.chat.id, message.from_user.id)
    return has_at_least(role, "moderator")


@router.message(F.text.regexp(r"(?i)^заметка создать\s+(\S+)"))
async def note_create(message: Message):
    if not await _can_edit(message):
        await message.reply("⛔ Создавать заметки могут модераторы и выше.")
        return
    name = message.text.split()[2].lower()
    src = message.reply_to_message
    text = (src.text or src.caption) if src else " ".join(message.text.split()[3:])
    if not text:
        await message.reply("Ответьте на сообщение с текстом или напишите текст после имени.")
        return
    if len(name) > 32:
        await message.reply("Имя заметки должно быть не длиннее 32 символов.")
        return
    if len(text) > 600:
        await message.reply("Текст заметки слишком длинный: максимум 600 символов.")
        return
    conn = await db.get_db()
    await conn.execute(
        "INSERT INTO notes (chat_id, name, text) VALUES (?,?,?) "
        "ON CONFLICT(chat_id, name) DO UPDATE SET text=excluded.text",
        (message.chat.id, name, text),
    )
    await conn.commit()
    await db.log_action(message.chat.id, message.from_user.id, "note_create", None, name)
    await message.answer(f"📝 Заметка «{escape(name, quote=True)}» сохранена.")


@router.message(F.text.regexp(r"(?i)^заметка удалить\s+(\S+)"))
async def note_delete(message: Message):
    if not await _can_edit(message):
        await message.reply("⛔ Удалять заметки могут модераторы и выше.")
        return
    name = message.text.split()[2].lower()
    if len(name) > 32:
        await message.reply("Имя заметки должно быть не длиннее 32 символов.")
        return
    conn = await db.get_db()
    cur = await conn.execute(
        "DELETE FROM notes WHERE chat_id=? AND name=?", (message.chat.id, name)
    )
    await conn.commit()
    if cur.rowcount:
        await db.log_action(message.chat.id, message.from_user.id, "note_delete", None, name)
        await message.answer(f"🗑 Заметка «{escape(name, quote=True)}» удалена.")
    else:
        await message.reply(f"Заметки «{escape(name, quote=True)}» нет.")


@router.message(F.text.regexp(r"(?i)^заметка\s+(\S+)$"))
async def note_get(message: Message):
    settings = await db.get_settings(message.chat.id)
    if not settings.get("notes", True):
        return
    name = message.text.split()[1].lower()
    if len(name) > 32:
        await message.reply("Имя заметки должно быть не длиннее 32 символов.")
        return
    if name in ("создать", "удалить"):
        return
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT text FROM notes WHERE chat_id=? AND name=?", (message.chat.id, name)
    )
    row = await cur.fetchone()
    if row:
        await message.answer(f"📝 <b>{escape(name, quote=True)}</b>\n{escape(row['text'], quote=True)}")
    else:
        await message.reply(f"Заметки «{escape(name, quote=True)}» нет. Список: команда «заметки».")


@router.message(F.text.regexp(r"(?i)^заметки$"))
async def notes_list(message: Message):
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT name FROM notes WHERE chat_id=? ORDER BY name LIMIT 50", (message.chat.id,)
    )
    rows = await cur.fetchall()
    if not rows:
        await message.answer("Заметок пока нет.")
        return
    await message.answer("📝 <b>Заметки:</b> " + ", ".join(escape(r["name"], quote=True) for r in rows))
