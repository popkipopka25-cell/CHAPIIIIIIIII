"""Очередь дел антиспама и апелляций; решение остаётся за модераторами."""
import time
from html import escape

from aiogram import Router, F
from aiogram.types import Message
from aiogram.exceptions import TelegramAPIError

import database as db
from handlers.moderation import effective_role, unmute_permissions
from utils import has_at_least, mention, resolve_target

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


async def _has_from_user(message: Message) -> bool:
    """Не обрабатывает команды, отправленные от имени канала без обычного пользователя."""
    return message.from_user is not None


router.message.filter(_has_from_user)


def _is_mod(role: str) -> bool:
    return has_at_least(role, "moderator")


@router.message(F.text.regexp(r"(?i)^обжалование\s+"))
async def submit_appeal(message: Message):
    if not message.from_user:
        return
    body = message.text.split(maxsplit=1)[1].strip() if len(message.text.split(maxsplit=1)) > 1 else ""
    if not body:
        await message.reply("Напишите причину: обжалование считаю мут ошибочным, потому что…")
        return
    if len(body) > 1000:
        await message.reply("Апелляция слишком длинная: максимум 1000 символов.")
        return
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT type,reason,created_at FROM punishments WHERE chat_id=? AND user_id=? AND active=1 ORDER BY id DESC LIMIT 1",
        (message.chat.id, message.from_user.id),
    )
    punishment = await cur.fetchone()
    if not punishment:
        await message.reply("У вас нет активных наказаний, которые можно обжаловать.")
        return
    appeal_text = f"Наказание: {punishment['type']}. Причина: {punishment['reason'] or 'не указана'}.\nАпелляция: {body}"
    await conn.execute(
        "INSERT INTO cases(chat_id,user_id,reason,message_text,status,created_at) VALUES(?,?,?,?, 'pending', ?)",
        (message.chat.id, message.from_user.id, "апелляция наказания", appeal_text[:1500], time.time()),
    )
    await conn.commit()
    await db.log_action(message.chat.id, message.from_user.id, "appeal_submit", message.from_user.id, body[:200])
    await message.answer("📨 Апелляция отправлена модераторам. Наказание не снимается автоматически.")


@router.message(F.text.regexp(r"(?i)^жалоба\s+"))
async def submit_user_report(message: Message):
    """Создаёт дело по жалобе на участника; наказание автоматически не применяется."""
    if not message.from_user or not message.text:
        return
    parts = message.text.split(maxsplit=2)
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        body = " ".join(parts[1:]).strip()
    else:
        if len(parts) < 3:
            await message.reply("Формат: жалоба @user причина (или ответьте на сообщение: жалоба причина)")
            return
        target_id, err = await resolve_target(message, [parts[1]], db)
        if err:
            await message.reply(err)
            return
        body = parts[2].strip()
    if target_id == message.from_user.id:
        await message.reply("Нельзя пожаловаться на самого себя.")
        return
    if not body:
        await message.reply("Добавьте причину жалобы.")
        return
    if len(body) > 1000:
        await message.reply("Текст жалобы слишком длинный: максимум 1000 символов.")
        return
    conn = await db.get_db()
    case_text = f"Отправитель жалобы: {message.from_user.id}\nТекст: {body}"
    await conn.execute(
        "INSERT INTO cases(chat_id,user_id,reason,message_text,status,created_at) VALUES(?,?,?,?,'pending',?)",
        (message.chat.id, target_id, "жалоба пользователя", case_text[:1500], time.time()),
    )
    await conn.commit()
    await db.log_action(message.chat.id, message.from_user.id, "user_report", target_id, body[:200])
    await message.answer("📨 Жалоба отправлена модераторам. Решение по ней будет принято вручную.")


@router.message(F.text.regexp(r"(?i)^дела$"))
async def list_cases(message: Message):
    if not message.from_user or not _is_mod(await effective_role(message.bot, message.chat.id, message.from_user.id)):
        await message.reply("⛔ Очередь дел доступна модераторам и выше.")
        return
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT id,user_id,reason,created_at FROM cases WHERE chat_id=? AND status='pending' ORDER BY id DESC LIMIT 20",
        (message.chat.id,),
    )
    rows = await cur.fetchall()
    if not rows:
        await message.answer("📁 Ожидающих дел нет.")
        return
    lines = ["📁 <b>Ожидающие дела:</b>"]
    for row in rows:
        lines.append(
            f"#{row['id']} — {mention(row['user_id'], str(row['user_id']))}; "
            f"{escape(str(row['reason'] or 'без причины'), quote=True)}; "
            f"{time.strftime('%d.%m %H:%M', time.localtime(row['created_at']))}"
        )
    lines.append("Просмотр: дело ID. Решение: дело принять ID / дело отклонить ID [причина].")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)^дело\s+\d+$"))
async def show_case(message: Message):
    if not message.from_user or not _is_mod(await effective_role(message.bot, message.chat.id, message.from_user.id)):
        await message.reply("⛔ Просматривать дела могут модераторы и выше.")
        return
    case_id = int(message.text.split()[1])
    conn = await db.get_db()
    cur = await conn.execute("SELECT * FROM cases WHERE chat_id=? AND id=?", (message.chat.id, case_id))
    row = await cur.fetchone()
    if not row:
        await message.reply("Дело не найдено в этой группе.")
        return
    raw_message = str(row['message_text'] or '')
    if len(raw_message) > 600:
        raw_message = raw_message[:600] + "…"
    text = (
        f"📁 <b>Дело #{row['id']}</b>\nПользователь: {mention(row['user_id'], str(row['user_id']))}\n"
        f"Статус: {escape(str(row['status']), quote=True)}\nПричина: {escape(str(row['reason'] or ''), quote=True)}\n"
        f"Сообщение: {escape(raw_message, quote=True)}"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)^дело\s+(принять|отклонить)\s+\d+(?:\s+.+)?$"))
async def decide_case(message: Message):
    if not message.from_user or not _is_mod(await effective_role(message.bot, message.chat.id, message.from_user.id)):
        await message.reply("⛔ Решать дела могут модераторы и выше.")
        return
    parts = message.text.split(maxsplit=3)
    decision = parts[1].lower()
    case_id = int(parts[2])
    note = parts[3].strip() if len(parts) > 3 else ""
    status = "accepted" if decision == "принять" else "rejected"
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT user_id,reason FROM cases WHERE chat_id=? AND id=? AND status='pending'",
        (message.chat.id, case_id),
    )
    case_row = await cur.fetchone()
    if not case_row:
        await message.reply("Ожидающее дело не найдено или уже рассмотрено.")
        return

    # Принятие апелляции отменяет последнее активное наказание. Обычные дела
    # только получают решение и не запускают автоматическое наказание.
    if status == "accepted" and str(case_row["reason"] or "").startswith("апелляция наказания"):
        cur = await conn.execute(
            "SELECT id,type FROM punishments WHERE chat_id=? AND user_id=? AND active=1 ORDER BY id DESC LIMIT 1",
            (message.chat.id, case_row["user_id"]),
        )
        punishment = await cur.fetchone()
        if punishment:
            try:
                if punishment["type"] == "mute":
                    await message.bot.restrict_chat_member(
                        message.chat.id, case_row["user_id"],
                        unmute_permissions(),
                    )
                elif punishment["type"] == "ban":
                    await message.bot.unban_chat_member(message.chat.id, case_row["user_id"], only_if_banned=True)
            except TelegramAPIError:
                await message.reply("Не удалось отменить наказание через Telegram. Проверьте права бота; дело оставлено открытым.")
                return
            await conn.execute("UPDATE punishments SET active=0 WHERE id=?", (punishment["id"],))

    cur = await conn.execute(
        "UPDATE cases SET status=?,decided_by=? WHERE chat_id=? AND id=? AND status='pending'",
        (status, message.from_user.id, message.chat.id, case_id),
    )
    await conn.commit()
    if not cur.rowcount:
        await message.reply("Дело уже было рассмотрено другим модератором.")
        return
    await db.log_action(message.chat.id, message.from_user.id, "case_" + status, int(case_row["user_id"]), f"#{case_id} {note}".strip())
    verdict = "принято" if status == "accepted" else "отклонено"
    extra = f"\nКомментарий: {escape(note, quote=True)}" if note else ""
    await message.answer(f"✅ Дело #{case_id} {verdict}.{extra}", parse_mode="HTML")
