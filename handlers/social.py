"""Социальные функции: браки с подтверждением обеих сторон."""
import time
import sqlite3
from aiogram import Router, F
from aiogram.types import Message
import database as db
from utils import resolve_target, mention

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


async def _has_from_user(message: Message) -> bool:
    """Не обрабатывает команды, отправленные от имени канала без обычного пользователя."""
    return message.from_user is not None


router.message.filter(_has_from_user)


async def _marriages_module_enabled(message: Message) -> bool:
    """Читает настройки только для брачных команд, не для каждого сообщения чата."""
    text = (message.text or "").strip().casefold()
    prefixes = ("брак", "принять брак", "отклонить брак", "мой брак", "развод", "восстановить брак", "принять восстановление брака", "отклонить восстановление брака")
    if not text.startswith(prefixes):
        return True
    return bool((await db.get_settings(message.chat.id)).get("marriages", True))


router.message.filter(_marriages_module_enabled)


@router.message(F.text.regexp(r"(?i)^брак\s+"))
async def propose_marriage(message: Message):
    """Создать предложение; брак не оформляется без согласия второго участника."""
    if not message.from_user:
        return
    target_id, err = await resolve_target(message, message.text.split()[1:], db)
    if err:
        await message.reply(err)
        return
    if target_id == message.from_user.id:
        await message.reply("Нельзя предложить брак самому себе.")
        return
    await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    target = await db.get_user(target_id)
    if not target and message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == target_id:
        replied_user = message.reply_to_message.from_user
        await db.upsert_user(replied_user.id, replied_user.username, replied_user.first_name)
        target = await db.get_user(target_id)
    if not target:
        await message.reply("Не удалось найти пользователя. Ответьте на его сообщение или используйте известный @username.")
        return
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT 1 FROM marriages WHERE chat_id=? AND (user1 IN (?,?) OR user2 IN (?,?)) LIMIT 1",
        (message.chat.id, message.from_user.id, target_id, message.from_user.id, target_id),
    )
    if await cur.fetchone():
        await message.reply("Один из вас уже состоит в браке.")
        return
    cur = await conn.execute(
        "SELECT 1 FROM proposals WHERE chat_id=? AND from_id=? AND to_id=? AND status='pending'",
        (message.chat.id, message.from_user.id, target_id),
    )
    if await cur.fetchone():
        await message.reply("Предложение этому участнику уже ожидает ответа.")
        return
    await conn.execute(
        "INSERT INTO proposals(chat_id,from_id,to_id,created_at,status) VALUES(?,?,?,?, 'pending')",
        (message.chat.id, message.from_user.id, target_id, time.time()),
    )
    await conn.commit()
    await message.answer(
        f"💍 {mention(message.from_user.id, message.from_user.first_name)} предлагает брак "
        f"{mention(target_id, target['first_name'])}!\n"
        "Для ответа напишите: принять брак или отклонить брак (ответом на это сообщение не требуется)."
    , parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)^принять брак$"))
async def accept_marriage(message: Message):
    if not message.from_user:
        return
    accepted_pair = None
    async with db.tx_lock:
        conn = await db.get_transaction_db()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT id,from_id FROM proposals WHERE chat_id=? AND to_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
                (message.chat.id, message.from_user.id),
            )
            proposal = await cur.fetchone()
            if not proposal:
                await conn.rollback()
                await message.reply("У вас нет ожидающих предложений о браке.")
                return
            a, b = sorted((int(proposal['from_id']), message.from_user.id))
            cur = await conn.execute(
                "SELECT 1 FROM marriages WHERE chat_id=? AND (user1 IN (?,?) OR user2 IN (?,?)) LIMIT 1",
                (message.chat.id, a, b, a, b),
            )
            if await cur.fetchone():
                await conn.execute("UPDATE proposals SET status='cancelled' WHERE id=?", (proposal['id'],))
                await conn.commit()
                await message.reply("Предложение отменено: один из участников уже состоит в браке.")
                return
            await conn.execute("UPDATE proposals SET status='accepted' WHERE id=? AND status='pending'", (proposal['id'],))
            await conn.execute(
                "INSERT INTO marriages(chat_id,user1,user2,since) VALUES(?,?,?,?)",
                (message.chat.id, a, b, time.time()),
            )
            await conn.commit()
            accepted_pair = (a, b)
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()
    if accepted_pair:
        a, b = accepted_pair
        await message.answer(f"💍 Поздравляем! Брак заключён между <code>{a}</code> и <code>{b}</code>.", parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)^отклонить брак$"))
async def reject_marriage(message: Message):
    if not message.from_user:
        return
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT id FROM proposals WHERE chat_id=? AND to_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
        (message.chat.id, message.from_user.id),
    )
    row = await cur.fetchone()
    if not row:
        await message.reply("Нет ожидающих предложений о браке.")
        return
    await conn.execute("UPDATE proposals SET status='rejected' WHERE id=?", (row['id'],))
    await conn.commit()
    await message.answer("Предложение о браке отклонено.")


@router.message(F.text.regexp(r"(?i)^(мой брак|брак статус)$"))
async def show_marriage(message: Message):
    if not message.from_user:
        return
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT user1,user2,since FROM marriages WHERE chat_id=? AND (user1=? OR user2=?) LIMIT 1",
        (message.chat.id, message.from_user.id, message.from_user.id),
    )
    row = await cur.fetchone()
    if not row:
        await message.answer("💔 Вы сейчас не состоите в браке.")
        return
    partner_id = row['user2'] if row['user1'] == message.from_user.id else row['user1']
    partner = await db.get_user(partner_id)
    days = max(0, int((time.time() - float(row['since'])) // 86400))
    await message.answer(f"💍 Ваш партнёр: {mention(partner_id, partner['first_name'] if partner else None)}\nВ браке: {days} дн.", parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)^развод$"))
async def divorce(message: Message):
    if not message.from_user:
        return
    async with db.tx_lock:
        conn = await db.get_transaction_db()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT user1,user2,since FROM marriages WHERE chat_id=? AND (user1=? OR user2=?) LIMIT 1",
                (message.chat.id, message.from_user.id, message.from_user.id),
            )
            row = await cur.fetchone()
            if not row:
                await conn.rollback()
                await message.reply("Вы не состоите в браке.")
                return
            await conn.execute(
                "INSERT INTO marriage_history(chat_id,user1,user2,since,divorced_at) VALUES(?,?,?,?,?)",
                (message.chat.id, row['user1'], row['user2'], row['since'], time.time()),
            )
            await conn.execute("DELETE FROM marriages WHERE chat_id=? AND user1=?", (message.chat.id, row['user1']))
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()
    await message.answer("💔 Брак расторгнут. В течение 3 дней можно предложить взаимное восстановление.")


@router.message(F.text.regexp(r"(?i)^восстановить брак$"))
async def propose_marriage_restore(message: Message):
    if not message.from_user:
        return
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT 1 FROM marriages WHERE chat_id=? AND (user1=? OR user2=?) LIMIT 1",
        (message.chat.id, message.from_user.id, message.from_user.id),
    )
    if await cur.fetchone():
        await message.reply("Вы уже состоите в браке.")
        return
    now = time.time()
    cur = await conn.execute(
        "SELECT id,user1,user2 FROM marriage_history WHERE chat_id=? AND (user1=? OR user2=?) "
        "AND restored_at IS NULL AND divorced_at>=? ORDER BY divorced_at DESC LIMIT 1",
        (message.chat.id, message.from_user.id, message.from_user.id, now - 3 * 86400),
    )
    history = await cur.fetchone()
    if not history:
        await message.reply("Нет брака, который можно восстановить. Восстановление доступно только в течение 3 дней после развода.")
        return
    partner_id = history['user2'] if history['user1'] == message.from_user.id else history['user1']
    cur = await conn.execute(
        "SELECT 1 FROM marriages WHERE chat_id=? AND (user1=? OR user2=?) LIMIT 1",
        (message.chat.id, partner_id, partner_id),
    )
    if await cur.fetchone():
        await message.reply("Ваш бывший партнёр уже состоит в другом браке.")
        return
    cur = await conn.execute(
        "SELECT 1 FROM marriage_restore_proposals WHERE chat_id=? AND history_id=? AND status='pending' LIMIT 1",
        (message.chat.id, history['id']),
    )
    if await cur.fetchone():
        await message.reply("Предложение о восстановлении уже ожидает ответа.")
        return
    try:
        await conn.execute(
            "INSERT INTO marriage_restore_proposals(chat_id,history_id,from_id,to_id,created_at,status) VALUES(?,?,?,?,?,'pending')",
            (message.chat.id, history['id'], message.from_user.id, partner_id, now),
        )
        await conn.commit()
    except sqlite3.IntegrityError:
        await message.reply("Предложение о восстановлении уже ожидает ответа.")
        return
    partner = await db.get_user(partner_id)
    await message.answer(
        f"💞 {mention(message.from_user.id, message.from_user.first_name)} предлагает восстановить прежний брак "
        f"с {mention(partner_id, partner['first_name'] if partner else None)}.\n"
        "Для согласия напишите «принять восстановление брака», для отказа — «отклонить восстановление брака».",
        parse_mode="HTML",
    )


@router.message(F.text.regexp(r"(?i)^принять восстановление брака$"))
async def accept_marriage_restore(message: Message):
    if not message.from_user:
        return
    restored = False
    async with db.tx_lock:
        conn = await db.get_transaction_db()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT id,history_id,from_id FROM marriage_restore_proposals "
                "WHERE chat_id=? AND to_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
                (message.chat.id, message.from_user.id),
            )
            proposal = await cur.fetchone()
            if not proposal:
                await conn.rollback()
                await message.reply("У вас нет ожидающих предложений о восстановлении брака.")
                return
            cur = await conn.execute(
                "SELECT user1,user2,since,divorced_at,restored_at FROM marriage_history WHERE id=? AND chat_id=?",
                (proposal['history_id'], message.chat.id),
            )
            history = await cur.fetchone()
            now = time.time()
            if not history or history['restored_at'] is not None or now - history['divorced_at'] > 3 * 86400:
                await conn.execute("UPDATE marriage_restore_proposals SET status='expired' WHERE id=?", (proposal['id'],))
                await conn.commit()
                await message.reply("Срок восстановления истёк или этот брак уже восстановлен.")
                return
            cur = await conn.execute(
                "SELECT 1 FROM marriages WHERE chat_id=? AND (user1 IN (?,?) OR user2 IN (?,?)) LIMIT 1",
                (message.chat.id, history['user1'], history['user2'], history['user1'], history['user2']),
            )
            if await cur.fetchone():
                await conn.execute("UPDATE marriage_restore_proposals SET status='cancelled' WHERE id=?", (proposal['id'],))
                await conn.commit()
                await message.reply("Восстановление отменено: один из вас уже состоит в браке.")
                return
            a, b = sorted((int(history['user1']), int(history['user2'])))
            await conn.execute(
                "INSERT INTO marriages(chat_id,user1,user2,since) VALUES(?,?,?,?)",
                (message.chat.id, a, b, history['since']),
            )
            await conn.execute("UPDATE marriage_history SET restored_at=? WHERE id=?", (now, proposal['history_id']))
            await conn.execute("UPDATE marriage_restore_proposals SET status='accepted' WHERE id=?", (proposal['id'],))
            await conn.commit()
            restored = True
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()
    if restored:
        await message.answer("💍 Прежний брак восстановлен по взаимному согласию!")


@router.message(F.text.regexp(r"(?i)^отклонить восстановление брака$"))
async def reject_marriage_restore(message: Message):
    if not message.from_user:
        return
    conn = await db.get_db()
    cur = await conn.execute(
        "UPDATE marriage_restore_proposals SET status='rejected' WHERE id=("
        "SELECT id FROM marriage_restore_proposals WHERE chat_id=? AND to_id=? AND status='pending' "
        "ORDER BY created_at DESC LIMIT 1)",
        (message.chat.id, message.from_user.id),
    )
    await conn.commit()
    await message.answer("Предложение о восстановлении отклонено." if cur.rowcount else "Нет ожидающих предложений о восстановлении.")

