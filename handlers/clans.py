"""Базовая система кланов: создание, просмотр, участники, заявка и выход."""
import time
import re
from html import escape

from aiogram import Router, F
from aiogram.types import Message
import database as db

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


async def _has_from_user(message: Message) -> bool:
    """Не обрабатывает команды, отправленные от имени канала без обычного пользователя."""
    return message.from_user is not None


router.message.filter(_has_from_user)


async def _clans_module_enabled(message: Message) -> bool:
    """Читает настройки только для команд кланов, не для каждого сообщения чата."""
    text = (message.text or "").strip().casefold()
    prefixes = (
        "клан", "кланы", "создать клан", "мой клан", "участники клана", "вступить в клан",
        "подать заявку в клан", "выйти из клана", "заявки клана", "принять заявку",
        "отклонить заявку", "исключить из клана", "передать клан", "описание клана",
        "список кланов", "переименовать клан", "удалить клан", "пригласить в клан",
        "мои приглашения", "принять приглашение", "отклонить приглашение", "отменить заявку в клан",
        "назначить зама", "снять зама", "топ кланов", "статистика клана", "моя заявка в клан",
        "роль в клане", "поиск клана", "очистить заявки клана", "отменить приглашения клана",
    )
    if not text.startswith(prefixes):
        return True
    return bool((await db.get_settings(message.chat.id)).get("clans", True))


router.message.filter(_clans_module_enabled)


def _safe(value) -> str:
    """Экранирует пользовательские данные для HTML-разметки сообщений."""
    return escape(str(value or ""), quote=True)


def _clean_name(raw: str) -> str:
    return re.sub(r"\s+", " ", raw).strip()


async def _user_clan(conn, chat_id: int, user_id: int):
    cur = await conn.execute(
        "SELECT c.id, c.name, c.description, c.owner_id, cm.role "
        "FROM clan_members cm JOIN clans c ON c.id=cm.clan_id "
        "WHERE c.chat_id=? AND cm.user_id=? LIMIT 1",
        (chat_id, user_id),
    )
    return await cur.fetchone()


@router.message(F.text.regexp(r"(?i)^создать клан\s+"))
async def create_clan(message: Message):
    """Создать клан; один пользователь может состоять только в одном клане на чат."""
    if not message.from_user or not message.text:
        return
    name = _clean_name(message.text.split(maxsplit=2)[2] if len(message.text.split(maxsplit=2)) > 2 else "")
    if not 3 <= len(name) <= 32:
        await message.reply("Название клана должно содержать от 3 до 32 символов. Пример: создать клан Звёзды")
        return
    conn = await db.get_db()
    if await _user_clan(conn, message.chat.id, message.from_user.id):
        await message.reply("Вы уже состоите в клане. Сначала выйдите из него.")
        return
    cur = await conn.execute(
        "SELECT 1 FROM clans WHERE chat_id=? AND name=? COLLATE NOCASE LIMIT 1",
        (message.chat.id, name),
    )
    if await cur.fetchone():
        await message.reply("В этом чате уже есть клан с таким названием (без учёта регистра).")
        return
    try:
        async with db.transaction() as tx:
            cur = await tx.execute(
                "SELECT 1 FROM clans WHERE chat_id=? AND name=? COLLATE NOCASE LIMIT 1",
                (message.chat.id, name),
            )
            if await cur.fetchone():
                await message.reply("В этом чате уже есть клан с таким названием.")
                return
            if await _user_clan(tx, message.chat.id, message.from_user.id):
                await message.reply("Вы уже состоите в клане. Сначала выйдите из него.")
                return
            cur = await tx.execute(
                "INSERT INTO clans(chat_id,name,owner_id,created_at) VALUES(?,?,?,?)",
                (message.chat.id, name, message.from_user.id, time.time()),
            )
            clan_id = cur.lastrowid
            await tx.execute("INSERT INTO clan_members(clan_id,user_id,role) VALUES(?,?,'owner')", (clan_id, message.from_user.id))
    except Exception as exc:
        # Unique constraint means this name is already used in the current group.
        if "UNIQUE" in str(exc).upper():
            await message.reply("В этом чате уже есть клан с таким названием.")
            return
        raise
    await message.answer(f"🏰 Клан «{_safe(name)}» создан! Вы его владелец.")


@router.message(F.text.regexp(r"(?i)^мой клан$"))
async def show_my_clan(message: Message):
    if not message.from_user:
        return
    conn = await db.get_db()
    clan = await _user_clan(conn, message.chat.id, message.from_user.id)
    if not clan:
        await message.reply("Вы пока не состоите в клане.")
        return
    cur = await conn.execute("SELECT COUNT(*) AS n FROM clan_members WHERE clan_id=?", (clan['id'],))
    count = (await cur.fetchone())['n']
    await message.answer(f"🏰 Клан: {_safe(clan['name'])}\nВаша роль: {clan['role']}\nУчастников: {count}")


@router.message(F.text.regexp(r"(?i)^(участники клана|клан участники)$"))
async def list_clan_members(message: Message):
    if not message.from_user:
        return
    conn = await db.get_db()
    clan = await _user_clan(conn, message.chat.id, message.from_user.id)
    if not clan:
        await message.reply("Сначала вступите в клан.")
        return
    cur = await conn.execute(
        "SELECT cm.user_id, cm.role, u.first_name, u.username FROM clan_members cm "
        "LEFT JOIN users u ON u.user_id=cm.user_id WHERE cm.clan_id=? ORDER BY "
        "CASE cm.role WHEN 'owner' THEN 0 WHEN 'deputy' THEN 1 ELSE 2 END, cm.user_id LIMIT 40",
        (clan['id'],),
    )
    rows = await cur.fetchall()
    lines = [f"🏰 Участники клана «{_safe(clan['name'])}»: "]
    for row in rows:
        display = row['first_name'] or (f"@{row['username']}" if row['username'] else str(row['user_id']))
        lines.append(f"• {_safe(display)} — {_safe(row['role'])}")
    await message.answer("\n".join(lines))


@router.message(F.text.regexp(r"(?i)^вступить в клан\s+"))
async def join_clan_by_name(message: Message):
    """Открытый клан принимает сразу; закрытый — только по приглашению владельца."""
    if not message.from_user or not message.text:
        return
    name = _clean_name(message.text.split(maxsplit=3)[3] if len(message.text.split(maxsplit=3)) > 3 else "")
    if not name:
        await message.reply("Укажите название: вступить в клан Звёзды")
        return
    conn = await db.get_db()
    if await _user_clan(conn, message.chat.id, message.from_user.id):
        await message.reply("Вы уже состоите в клане.")
        return
    cur = await conn.execute("SELECT id,name,privacy FROM clans WHERE chat_id=? AND name=? COLLATE NOCASE LIMIT 1", (message.chat.id, name))
    clan = await cur.fetchone()
    if not clan:
        await message.reply("Клан с таким названием не найден в этом чате.")
        return
    cur = await conn.execute("SELECT 1 FROM clan_bans WHERE clan_id=? AND user_id=?", (clan['id'], message.from_user.id))
    if await cur.fetchone():
        await message.reply("Владелец клана запретил вам вступление.")
        return
    if clan['privacy'] == 'closed':
        await message.reply("Этот клан закрытый. Попросите владельца прислать вам приглашение.")
        return
    try:
        async with db.transaction() as tx:
            if await _user_clan(tx, message.chat.id, message.from_user.id):
                await message.reply("Вы уже состоите в клане.")
                return
            cur = await tx.execute("SELECT privacy FROM clans WHERE id=? AND chat_id=?", (clan['id'], message.chat.id))
            current_clan = await cur.fetchone()
            if not current_clan or current_clan['privacy'] == 'closed':
                await message.reply("Этот клан закрыт или больше не существует.")
                return
            cur = await tx.execute("SELECT 1 FROM clan_bans WHERE clan_id=? AND user_id=?", (clan['id'], message.from_user.id))
            if await cur.fetchone():
                await message.reply("Владелец клана запретил вам вступление.")
                return
            await tx.execute("INSERT INTO clan_members(clan_id,user_id,role) VALUES(?,?,'member')", (clan['id'], message.from_user.id))
            await tx.execute(
                "UPDATE clan_requests SET status='cancelled' WHERE user_id=? AND status='pending' "
                "AND clan_id IN (SELECT id FROM clans WHERE chat_id=?)",
                (message.from_user.id, message.chat.id),
            )
            await tx.execute(
                "UPDATE clan_invites SET status='cancelled' WHERE user_id=? AND status='pending' "
                "AND clan_id IN (SELECT id FROM clans WHERE chat_id=?)",
                (message.from_user.id, message.chat.id),
            )
    except Exception:
        await message.reply("Не удалось вступить. Возможно, вы уже состоите в другом клане.")
        return
    await message.answer(f"🏰 Вы вступили в открытый клан «{_safe(clan['name'])}»!")


@router.message(F.text.regexp(r"(?i)^подать заявку в клан\s+"))
@router.message(F.text.regexp(r"(?i)^клан заявка\s+"))
async def request_to_join_clan(message: Message):
    """Подать заявку владельцу клана, в том числе закрытого."""
    if not message.from_user or not message.text:
        return
    if message.text.casefold().startswith("клан заявка"):
        parts = message.text.split(maxsplit=2)
        name = _clean_name(parts[2] if len(parts) > 2 else "")
    else:
        parts = message.text.split(maxsplit=4)
        name = _clean_name(parts[4] if len(parts) > 4 else "")
    if not name:
        await message.reply("Укажите название: клан заявка Звёзды")
        return
    conn = await db.get_db()
    cur = await conn.execute("SELECT id,name FROM clans WHERE chat_id=? AND name=? COLLATE NOCASE LIMIT 1", (message.chat.id, name))
    clan = await cur.fetchone()
    if not clan:
        await message.reply("Клан с таким названием не найден в этом чате.")
        return
    try:
        async with db.transaction() as tx:
            if await _user_clan(tx, message.chat.id, message.from_user.id):
                await message.reply("Вы уже состоите в клане.")
                return
            cur = await tx.execute("SELECT 1 FROM clan_bans WHERE clan_id=? AND user_id=?", (clan['id'], message.from_user.id))
            if await cur.fetchone():
                await message.reply("Владелец клана запретил вам вступление.")
                return
            cur = await tx.execute(
                "SELECT 1 FROM clan_requests WHERE clan_id=? AND user_id=? AND status='pending' LIMIT 1",
                (clan['id'], message.from_user.id),
            )
            if await cur.fetchone():
                await message.reply("Ваша заявка уже ожидает рассмотрения.")
                return
            await tx.execute(
                "INSERT INTO clan_requests(clan_id,user_id,status) VALUES(?,?,'pending')",
                (clan['id'], message.from_user.id),
            )
    except Exception:
        await message.reply("Не удалось отправить заявку. Попробуйте ещё раз.")
        return
    await message.answer(f"📨 Заявка в клан «{_safe(clan['name'])}» отправлена. Статус можно проверить командой «моя заявка в клан».")


@router.message(F.text.regexp(r"(?i)^(выйти из клана|клан выйти)$"))
async def leave_clan(message: Message):
    if not message.from_user:
        return
    conn = await db.get_db()
    clan = await _user_clan(conn, message.chat.id, message.from_user.id)
    if not clan:
        await message.reply("Вы не состоите в клане.")
        return
    try:
        async with db.transaction() as tx:
            current = await _user_clan(tx, message.chat.id, message.from_user.id)
            if not current:
                await message.reply("Вы уже не состоите в клане.")
                return
            if current['role'] == 'owner':
                cur = await tx.execute("SELECT COUNT(*) AS n FROM clan_members WHERE clan_id=?", (current['id'],))
                count = (await cur.fetchone())['n']
                if count > 1:
                    await message.reply("Владелец не может выйти, пока в клане есть другие участники. Сначала передайте владение или удалите клан.")
                    return
                await tx.execute("DELETE FROM clan_requests WHERE clan_id=?", (current['id'],))
                await tx.execute("DELETE FROM clan_invites WHERE clan_id=?", (current['id'],))
                await tx.execute("DELETE FROM clan_bans WHERE clan_id=?", (current['id'],))
                await tx.execute("DELETE FROM clan_members WHERE clan_id=?", (current['id'],))
                await tx.execute("DELETE FROM clans WHERE id=?", (current['id'],))
            else:
                await tx.execute("DELETE FROM clan_members WHERE clan_id=? AND user_id=?", (current['id'], message.from_user.id))
    except Exception:
        await message.reply("Не удалось выйти из клана. Попробуйте ещё раз.")
        return
    await message.answer(f"Вы вышли из клана «{_safe(clan['name'])}».")


async def _owned_clan(conn, chat_id: int, user_id: int):
    """Вернуть клан, которым пользователь владеет именно в текущем чате."""
    cur = await conn.execute(
        "SELECT c.id, c.name, c.owner_id FROM clans c "
        "JOIN clan_members cm ON cm.clan_id=c.id "
        "WHERE c.chat_id=? AND c.owner_id=? AND cm.user_id=? AND cm.role='owner' LIMIT 1",
        (chat_id, user_id, user_id),
    )
    return await cur.fetchone()


def _target_id(message: Message):
    """Команды администрирования клана принимают числовой Telegram ID."""
    if not message.text:
        return None
    parts = message.text.split()
    if len(parts) < 2:
        return None
    # Берём последний токен: команды могут состоять из двух и трёх слов.
    raw = parts[-1].strip()
    if not raw.isascii() or not raw.isdigit() or len(raw) > 19:
        return None
    value = int(raw)
    return value if 0 < value <= 9_223_372_036_854_775_807 else None


@router.message(F.text.regexp(r"(?i)^заявки клана$"))
async def list_clan_requests(message: Message):
    """Показать владельцу ожидающие заявки в его клан."""
    if not message.from_user:
        return
    conn = await db.get_db()
    clan = await _owned_clan(conn, message.chat.id, message.from_user.id)
    if not clan:
        await message.reply("Эта команда доступна только владельцу клана.")
        return
    cur = await conn.execute(
        "SELECT cr.user_id, u.first_name, u.username FROM clan_requests cr "
        "LEFT JOIN users u ON u.user_id=cr.user_id "
        "WHERE cr.clan_id=? AND cr.status='pending' ORDER BY cr.id LIMIT 40",
        (clan['id'],),
    )
    rows = await cur.fetchall()
    if not rows:
        await message.answer(f"В клане «{_safe(clan['name'])}» нет ожидающих заявок.")
        return
    lines = [f"📨 Заявки в клан «{_safe(clan['name'])}»: "]
    for row in rows:
        display = row['first_name'] or (f"@{row['username']}" if row['username'] else "участник")
        lines.append(f"• {_safe(display)} — ID {row['user_id']}")
    lines.append("Принять: принять заявку &lt;ID&gt;\nОтклонить: отклонить заявку &lt;ID&gt;")
    await message.answer("\n".join(lines))


@router.message(F.text.regexp(r"(?i)^принять заявку\s+"))
async def accept_clan_request(message: Message):
    if not message.from_user:
        return
    target_id = _target_id(message)
    if not target_id:
        await message.reply("Укажите числовой Telegram ID. Пример: принять заявку 123456789")
        return
    conn = await db.get_db()
    clan = await _owned_clan(conn, message.chat.id, message.from_user.id)
    if not clan:
        await message.reply("Эта команда доступна только владельцу клана.")
        return
    cur = await conn.execute(
        "SELECT id FROM clan_requests WHERE clan_id=? AND user_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
        (clan['id'], target_id),
    )
    request = await cur.fetchone()
    if not request:
        await message.reply("Ожидающая заявка этого пользователя не найдена.")
        return
    if await _user_clan(conn, message.chat.id, target_id):
        await message.reply("Этот пользователь уже состоит в клане. Заявка оставлена без изменений.")
        return
    try:
        async with db.transaction() as tx:
            if await _user_clan(tx, message.chat.id, target_id):
                await message.reply("Этот пользователь уже состоит в клане. Заявка оставлена без изменений.")
                return
            cur = await tx.execute("SELECT 1 FROM clan_bans WHERE clan_id=? AND user_id=?", (clan['id'], target_id))
            if await cur.fetchone():
                await message.reply("Пользователю запрещено вступать в этот клан.")
                return
            cur = await tx.execute(
                "SELECT id FROM clan_requests WHERE clan_id=? AND user_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
                (clan['id'], target_id),
            )
            current_request = await cur.fetchone()
            if not current_request:
                await message.reply("Ожидающая заявка уже рассмотрена.")
                return
            await tx.execute(
                "INSERT INTO clan_members(clan_id,user_id,role) VALUES(?,?,'member')",
                (clan['id'], target_id),
            )
            await tx.execute("UPDATE clan_requests SET status='accepted' WHERE id=?", (current_request['id'],))
            await tx.execute(
                "UPDATE clan_requests SET status='cancelled' WHERE user_id=? AND status='pending' AND id<>? "
                "AND clan_id IN (SELECT id FROM clans WHERE chat_id=?)",
                (target_id, current_request['id'], message.chat.id),
            )
    except Exception:
        await message.reply("Не удалось принять заявку. Проверьте, не вступил ли пользователь в клан одновременно.")
        return
    await message.answer(f"✅ Пользователь {target_id} принят в клан «{_safe(clan['name'])}».")


@router.message(F.text.regexp(r"(?i)^отклонить заявку\s+"))
async def reject_clan_request(message: Message):
    if not message.from_user:
        return
    target_id = _target_id(message)
    if not target_id:
        await message.reply("Укажите числовой Telegram ID. Пример: отклонить заявку 123456789")
        return
    conn = await db.get_db()
    clan = await _owned_clan(conn, message.chat.id, message.from_user.id)
    if not clan:
        await message.reply("Эта команда доступна только владельцу клана.")
        return
    cur = await conn.execute(
        "SELECT id FROM clan_requests WHERE clan_id=? AND user_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
        (clan['id'], target_id),
    )
    request = await cur.fetchone()
    if not request:
        await message.reply("Ожидающая заявка этого пользователя не найдена.")
        return
    await conn.execute("UPDATE clan_requests SET status='rejected' WHERE id=?", (request['id'],))
    await conn.commit()
    await message.answer(f"Заявка пользователя {target_id} отклонена.")


@router.message(F.text.regexp(r"(?i)^(исключить из клана|клан исключить)\s+"))
async def kick_clan_member(message: Message):
    if not message.from_user:
        return
    target_id = _target_id(message)
    if not target_id:
        await message.reply("Укажите числовой Telegram ID. Пример: исключить из клана 123456789")
        return
    conn = await db.get_db()
    clan = await _owned_clan(conn, message.chat.id, message.from_user.id)
    if not clan:
        await message.reply("Исключать участников может только владелец клана.")
        return
    if target_id == message.from_user.id or target_id == clan['owner_id']:
        await message.reply("Нельзя исключить владельца клана этой командой.")
        return
    cur = await conn.execute(
        "SELECT role FROM clan_members WHERE clan_id=? AND user_id=? LIMIT 1",
        (clan['id'], target_id),
    )
    member = await cur.fetchone()
    if not member:
        await message.reply("Этот пользователь не состоит в вашем клане.")
        return
    async with db.transaction() as tx:
        current_clan = await _owned_clan(tx, message.chat.id, message.from_user.id)
        if not current_clan or current_clan['id'] != clan['id']:
            await message.reply("Владение кланом изменилось; повторите команду.")
            return
        cur = await tx.execute("SELECT 1 FROM clan_members WHERE clan_id=? AND user_id=?", (clan['id'], target_id))
        if not await cur.fetchone():
            await message.reply("Этот пользователь уже не состоит в клане.")
            return
        await tx.execute("DELETE FROM clan_members WHERE clan_id=? AND user_id=?", (clan['id'], target_id))
        await tx.execute(
            "UPDATE clan_requests SET status='rejected' WHERE clan_id=? AND user_id=? AND status='pending'",
            (clan['id'], target_id),
        )
    await message.answer(f"Пользователь {target_id} исключён из клана «{_safe(clan['name'])}».")


@router.message(F.text.regexp(r"(?i)^(передать клан|клан передать)\s+"))
async def transfer_clan_ownership(message: Message):
    if not message.from_user:
        return
    target_id = _target_id(message)
    if not target_id:
        await message.reply("Укажите ID действующего участника. Пример: передать клан 123456789")
        return
    conn = await db.get_db()
    clan = await _owned_clan(conn, message.chat.id, message.from_user.id)
    if not clan:
        await message.reply("Передать владение может только текущий владелец клана.")
        return
    if target_id == message.from_user.id:
        await message.reply("Вы уже являетесь владельцем этого клана.")
        return
    cur = await conn.execute(
        "SELECT user_id FROM clan_members WHERE clan_id=? AND user_id=? LIMIT 1",
        (clan['id'], target_id),
    )
    if not await cur.fetchone():
        await message.reply("Передать клан можно только действующему участнику. Сначала пригласите его и примите заявку.")
        return
    try:
        async with db.transaction() as tx:
            current_clan = await _owned_clan(tx, message.chat.id, message.from_user.id)
            if not current_clan or current_clan['id'] != clan['id']:
                await message.reply("Владение кланом изменилось; повторите команду.")
                return
            cur = await tx.execute("SELECT 1 FROM clan_members WHERE clan_id=? AND user_id=?", (clan['id'], target_id))
            if not await cur.fetchone():
                await message.reply("Пользователь больше не состоит в клане.")
                return
            await tx.execute("UPDATE clan_members SET role='member' WHERE clan_id=? AND user_id=?", (clan['id'], message.from_user.id))
            await tx.execute("UPDATE clan_members SET role='owner' WHERE clan_id=? AND user_id=?", (clan['id'], target_id))
            await tx.execute("UPDATE clans SET owner_id=? WHERE id=? AND chat_id=?", (target_id, clan['id'], message.chat.id))
    except Exception:
        await message.reply("Не удалось передать владение. Изменения отменены.")
        return
    await message.answer(f"👑 Владение кланом «{_safe(clan['name'])}» передано пользователю {target_id}.")


# Дополнительные команды этапа 6 (20 новых обработчиков).
async def _member_clan(conn, chat_id: int, user_id: int):
    return await _user_clan(conn, chat_id, user_id)

async def _send_owner_only(message: Message, conn):
    if not message.from_user:
        return None
    clan = await _owned_clan(conn, message.chat.id, message.from_user.id)
    if not clan:
        await message.reply("Команда доступна только владельцу вашего клана.")
        return None
    return clan

@router.message(F.text.regexp(r"(?i)^описание клана\s+"))
async def set_clan_description(message: Message):
    """1. Установить описание своего клана."""
    if not message.from_user or not message.text: return
    parts = message.text.split(maxsplit=2)
    description = parts[2].strip() if len(parts) > 2 else ""
    if len(description) > 300:
        await message.reply("Описание слишком длинное: максимум 300 символов."); return
    conn = await db.get_db(); clan = await _send_owner_only(message, conn)
    if not clan: return
    await conn.execute("UPDATE clans SET description=? WHERE id=?", (description, clan['id']))
    await conn.commit()
    await message.answer("Описание клана обновлено." if description else "Описание клана очищено.")

@router.message(F.text.regexp(r"(?i)^клан инфо\s+"))
async def clan_info(message: Message):
    """2. Посмотреть информацию о клане по названию."""
    if not message.text: return
    parts = message.text.split(maxsplit=2)
    name = _clean_name(parts[2] if len(parts) > 2 else "")
    if not name:
        await message.reply("Укажите название: клан инфо Название")
        return
    conn = await db.get_db()
    cur = await conn.execute("SELECT * FROM clans WHERE chat_id=? AND name=? COLLATE NOCASE LIMIT 1", (message.chat.id, name))
    clan = await cur.fetchone()
    if not clan:
        await message.reply("Клан не найден в этом чате."); return
    cur = await conn.execute("SELECT COUNT(*) n FROM clan_members WHERE clan_id=?", (clan['id'],)); members = (await cur.fetchone())['n']
    await message.answer(f"🏰 {_safe(clan['name'])}\n👑 Владелец ID: {clan['owner_id']}\n👥 Участников: {members}\n📝 {_safe(clan['description'] or 'Описание не задано.')}")

@router.message(F.text.regexp(r"(?i)^(список кланов|кланы)$"))
async def list_clans(message: Message):
    """3. Список кланов текущего чата."""
    conn = await db.get_db()
    cur = await conn.execute("SELECT c.id,c.name,c.description,COUNT(cm.user_id) members FROM clans c LEFT JOIN clan_members cm ON cm.clan_id=c.id WHERE c.chat_id=? GROUP BY c.id ORDER BY members DESC,c.name COLLATE NOCASE LIMIT 15", (message.chat.id,))
    rows = await cur.fetchall()
    if not rows: await message.answer("В этом чате пока нет кланов."); return
    await message.answer("🏰 Кланы чата:\n" + "\n".join(f"• {_safe(r['name'])} — {r['members']} уч.\n  {_safe((r['description'] or 'Без описания')[:100])}" for r in rows))

@router.message(F.text.regexp(r"(?i)^(переименовать клан|клан изменить)\s+"))
async def rename_clan(message: Message):
    """4. Переименовать свой клан."""
    if not message.from_user or not message.text: return
    parts = message.text.split(maxsplit=2)
    name = _clean_name(parts[2] if len(parts) > 2 else "")
    if not 3 <= len(name) <= 32: await message.reply("Новое название должно быть длиной от 3 до 32 символов."); return
    conn = await db.get_db(); clan = await _send_owner_only(message, conn)
    if not clan: return
    try:
        async with db.transaction() as tx:
            current_clan = await _owned_clan(tx, message.chat.id, message.from_user.id)
            if not current_clan or current_clan['id'] != clan['id']:
                await message.reply("Владение кланом изменилось; повторите команду.")
                return
            cur = await tx.execute(
                "SELECT 1 FROM clans WHERE chat_id=? AND name=? COLLATE NOCASE AND id<>? LIMIT 1",
                (message.chat.id, name, clan['id']),
            )
            if await cur.fetchone():
                await message.reply("Такое название уже занято в этом чате.")
                return
            await tx.execute("UPDATE clans SET name=? WHERE id=?", (name, clan['id']))
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            await message.reply("Такое название уже занято в этом чате.")
            return
        raise
    await message.answer(f"Название клана изменено на «{_safe(name)}».")

@router.message(F.text.regexp(r"(?i)^(удалить клан|клан удалить)$"))
async def delete_clan(message: Message):
    """5. Удалить свой клан вместе с заявками, приглашениями и составом."""
    if not message.from_user: return
    conn = await db.get_db(); clan = await _send_owner_only(message, conn)
    if not clan: return
    async with db.transaction() as tx:
        current_clan = await _owned_clan(tx, message.chat.id, message.from_user.id)
        if not current_clan or current_clan['id'] != clan['id']:
            await message.reply("Владение кланом изменилось; повторите команду.")
            return
        await tx.execute("DELETE FROM clan_requests WHERE clan_id=?", (clan['id'],))
        await tx.execute("DELETE FROM clan_invites WHERE clan_id=?", (clan['id'],))
        await tx.execute("DELETE FROM clan_bans WHERE clan_id=?", (clan['id'],))
        await tx.execute("DELETE FROM clan_members WHERE clan_id=?", (clan['id'],))
        await tx.execute("DELETE FROM clans WHERE id=?", (clan['id'],))
    await message.answer(f"Клан «{_safe(clan['name'])}» удалён.")

@router.message(F.text.regexp(r"(?i)^(пригласить в клан|клан пригласить)\s+"))
async def invite_to_clan(message: Message):
    """6. Пригласить пользователя по Telegram ID."""
    if not message.from_user: return
    target_id = _target_id(message)
    if not target_id: await message.reply("Пример: пригласить в клан 123456789"); return
    conn = await db.get_db(); clan = await _send_owner_only(message, conn)
    if not clan: return
    if target_id == message.from_user.id: await message.reply("Нельзя приглашать самого себя."); return
    try:
        async with db.transaction() as tx:
            current_clan = await _owned_clan(tx, message.chat.id, message.from_user.id)
            if not current_clan or current_clan['id'] != clan['id']:
                await message.reply("Владение кланом изменилось; повторите команду.")
                return
            if await _member_clan(tx, message.chat.id, target_id):
                await message.reply("Этот пользователь уже состоит в клане.")
                return
            cur = await tx.execute("SELECT 1 FROM clan_bans WHERE clan_id=? AND user_id=?", (clan['id'], target_id))
            if await cur.fetchone():
                await message.reply("Пользователю запрещено вступать в этот клан.")
                return
            cur = await tx.execute("SELECT id FROM clan_invites WHERE clan_id=? AND user_id=? AND status='pending' LIMIT 1", (clan['id'], target_id))
            if await cur.fetchone():
                await message.reply("Приглашение этому пользователю уже отправлено.")
                return
            await tx.execute("INSERT INTO clan_invites(clan_id,user_id,inviter_id,status,created_at) VALUES(?,?,?,'pending',?)", (clan['id'], target_id, message.from_user.id, time.time()))
    except Exception:
        await message.reply("Не удалось создать приглашение. Попробуйте ещё раз.")
        return
    await message.answer(f"📨 Приглашение в клан «{_safe(clan['name'])}» создано для ID {target_id}.")

@router.message(F.text.regexp(r"(?i)^мои приглашения$"))
async def my_clan_invites(message: Message):
    """7. Показать приглашения пользователя."""
    if not message.from_user: return
    conn = await db.get_db()
    cur = await conn.execute("SELECT c.name,ci.created_at FROM clan_invites ci JOIN clans c ON c.id=ci.clan_id WHERE ci.user_id=? AND ci.status='pending' AND c.chat_id=? ORDER BY ci.id DESC LIMIT 30", (message.from_user.id, message.chat.id))
    rows = await cur.fetchall()
    if not rows: await message.answer("У вас нет ожидающих приглашений в кланы."); return
    await message.answer("📨 Ваши приглашения:\n" + "\n".join(f"• {_safe(r['name'])} — принять: принять приглашение {_safe(r['name'])}" for r in rows))

@router.message(F.text.regexp(r"(?i)^принять приглашение\s+"))
async def accept_clan_invite(message: Message):
    """8. Принять приглашение по названию клана."""
    if not message.from_user or not message.text: return
    parts = message.text.split(maxsplit=2)
    name = _clean_name(parts[2] if len(parts) > 2 else "")
    if not name:
        await message.reply("Укажите название клана для принятия приглашения.")
        return
    conn = await db.get_db()
    if await _member_clan(conn, message.chat.id, message.from_user.id): await message.reply("Вы уже состоите в клане."); return
    cur = await conn.execute("SELECT ci.id,ci.clan_id,c.name FROM clan_invites ci JOIN clans c ON c.id=ci.clan_id WHERE c.chat_id=? AND c.name=? COLLATE NOCASE AND ci.user_id=? AND ci.status='pending' ORDER BY ci.id DESC LIMIT 1", (message.chat.id, name, message.from_user.id))
    inv = await cur.fetchone()
    if not inv: await message.reply("Ожидающее приглашение в этот клан не найдено."); return
    cur = await conn.execute("SELECT 1 FROM clan_bans WHERE clan_id=? AND user_id=?", (inv['clan_id'], message.from_user.id))
    if await cur.fetchone():
        await message.reply("Владелец клана запретил вам вступление.")
        return
    try:
        async with db.transaction() as tx:
            if await _user_clan(tx, message.chat.id, message.from_user.id):
                await message.reply("Вы уже состоите в клане.")
                return
            cur = await tx.execute(
                "SELECT 1 FROM clan_bans WHERE clan_id=? AND user_id=?", (inv['clan_id'], message.from_user.id)
            )
            if await cur.fetchone():
                await message.reply("Владелец клана запретил вам вступление.")
                return
            cur = await tx.execute("SELECT id FROM clan_invites WHERE id=? AND status='pending'", (inv['id'],))
            if not await cur.fetchone():
                await message.reply("Приглашение уже не действует.")
                return
            await tx.execute("INSERT INTO clan_members(clan_id,user_id,role) VALUES(?,?,'member')", (inv['clan_id'], message.from_user.id))
            await tx.execute("UPDATE clan_invites SET status='accepted' WHERE id=?", (inv['id'],))
            await tx.execute(
                "UPDATE clan_invites SET status='cancelled' WHERE user_id=? AND status='pending' AND id<>? "
                "AND clan_id IN (SELECT id FROM clans WHERE chat_id=?)",
                (message.from_user.id, inv['id'], message.chat.id),
            )
            await tx.execute(
                "UPDATE clan_requests SET status='cancelled' WHERE user_id=? AND status='pending' "
                "AND clan_id IN (SELECT id FROM clans WHERE chat_id=?)",
                (message.from_user.id, message.chat.id),
            )
    except Exception:
        await message.reply("Не удалось принять приглашение. Возможно, вы уже вступили в другой клан.")
        return
    await message.answer(f"Вы вступили в клан «{_safe(inv['name'])}»!")

@router.message(F.text.regexp(r"(?i)^отклонить приглашение\s+"))
async def reject_clan_invite(message: Message):
    """9. Отклонить приглашение."""
    if not message.from_user or not message.text: return
    parts = message.text.split(maxsplit=2)
    name = _clean_name(parts[2] if len(parts) > 2 else "")
    if not name:
        await message.reply("Укажите название клана для отклонения приглашения.")
        return
    conn = await db.get_db()
    cur = await conn.execute("UPDATE clan_invites SET status='rejected' WHERE id=(SELECT ci.id FROM clan_invites ci JOIN clans c ON c.id=ci.clan_id WHERE c.chat_id=? AND c.name=? COLLATE NOCASE AND ci.user_id=? AND ci.status='pending' ORDER BY ci.id DESC LIMIT 1)", (message.chat.id, name, message.from_user.id))
    await conn.commit()
    if cur.rowcount: await message.answer(f"Приглашение в клан «{_safe(name)}» отклонено.")
    else: await message.reply("Ожидающее приглашение в этот клан не найдено.")

@router.message(F.text.regexp(r"(?i)^отменить заявку в клан$"))
async def cancel_clan_request(message: Message):
    """10. Отменить свою последнюю ожидающую заявку в текущем чате."""
    if not message.from_user: return
    conn = await db.get_db()
    cur = await conn.execute("UPDATE clan_requests SET status='cancelled' WHERE id=(SELECT cr.id FROM clan_requests cr JOIN clans c ON c.id=cr.clan_id WHERE c.chat_id=? AND cr.user_id=? AND cr.status='pending' ORDER BY cr.id DESC LIMIT 1)", (message.chat.id, message.from_user.id))
    await conn.commit()
    await message.answer("Заявка отменена." if cur.rowcount else "У вас нет ожидающей заявки в этом чате.")

@router.message(F.text.regexp(r"(?i)^назначить зама\s+"))
async def appoint_clan_deputy(message: Message):
    """11. Назначить действующего участника заместителем."""
    if not message.from_user: return
    target_id = _target_id(message)
    if not target_id: await message.reply("Пример: назначить зама 123456789"); return
    conn = await db.get_db(); clan = await _send_owner_only(message, conn)
    if not clan: return
    cur = await conn.execute("SELECT role FROM clan_members WHERE clan_id=? AND user_id=?", (clan['id'], target_id)); row = await cur.fetchone()
    if not row: await message.reply("Этот пользователь не состоит в вашем клане."); return
    if target_id == clan['owner_id']: await message.reply("Владелец уже имеет высшую роль."); return
    await conn.execute("UPDATE clan_members SET role='deputy' WHERE clan_id=? AND user_id=?", (clan['id'], target_id)); await conn.commit()
    await message.answer(f"Пользователь {target_id} назначен заместителем клана.")

@router.message(F.text.regexp(r"(?i)^снять зама\s+"))
async def remove_clan_deputy(message: Message):
    """12. Снять роль заместителя."""
    if not message.from_user: return
    target_id = _target_id(message)
    if not target_id: await message.reply("Пример: снять зама 123456789"); return
    conn = await db.get_db(); clan = await _send_owner_only(message, conn)
    if not clan: return
    cur = await conn.execute("UPDATE clan_members SET role='member' WHERE clan_id=? AND user_id=? AND role='deputy'", (clan['id'], target_id)); await conn.commit()
    await message.answer(f"Роль заместителя снята с ID {target_id}." if cur.rowcount else "Этот пользователь не является заместителем вашего клана.")

@router.message(F.text.regexp(r"(?i)^топ кланов$"))
async def clan_leaderboard(message: Message):
    """13. Рейтинг кланов по числу участников."""
    conn = await db.get_db(); cur = await conn.execute("SELECT c.name,COUNT(cm.user_id) members FROM clans c LEFT JOIN clan_members cm ON cm.clan_id=c.id WHERE c.chat_id=? GROUP BY c.id ORDER BY members DESC,c.name COLLATE NOCASE LIMIT 10", (message.chat.id,)); rows = await cur.fetchall()
    if not rows: await message.answer("Пока нет кланов для рейтинга."); return
    await message.answer("🏆 Топ кланов по числу участников:\n" + "\n".join(f"{i}. {_safe(r['name'])} — {r['members']}" for i,r in enumerate(rows,1)))

@router.message(F.text.regexp(r"(?i)^статистика клана$"))
async def clan_statistics(message: Message):
    """14. Статистика клана, в котором состоит пользователь."""
    if not message.from_user: return
    conn = await db.get_db(); clan = await _member_clan(conn, message.chat.id, message.from_user.id)
    if not clan: await message.reply("Вы не состоите в клане."); return
    cur = await conn.execute("SELECT COUNT(*) n FROM clan_members WHERE clan_id=?", (clan['id'],)); members = (await cur.fetchone())['n']
    cur = await conn.execute("SELECT COUNT(*) n FROM clan_requests WHERE clan_id=? AND status='pending'", (clan['id'],)); requests = (await cur.fetchone())['n']
    cur = await conn.execute("SELECT COUNT(*) n FROM clan_invites WHERE clan_id=? AND status='pending'", (clan['id'],)); invites = (await cur.fetchone())['n']
    await message.answer(f"📊 Статистика «{_safe(clan['name'])}»\nУчастников: {members}\nОжидающих заявок: {requests}\nОжидающих приглашений: {invites}")

@router.message(F.text.regexp(r"(?i)^моя заявка в клан$"))
async def my_clan_request_status(message: Message):
    """15. Проверить статус последней заявки пользователя в текущем чате."""
    if not message.from_user: return
    conn = await db.get_db(); cur = await conn.execute("SELECT c.name,cr.status FROM clan_requests cr JOIN clans c ON c.id=cr.clan_id WHERE c.chat_id=? AND cr.user_id=? ORDER BY cr.id DESC LIMIT 1", (message.chat.id, message.from_user.id)); row = await cur.fetchone()
    if not row: await message.answer("Вы ещё не отправляли заявку в клан в этом чате."); return
    labels = {'pending':'ожидает рассмотрения','accepted':'принята','rejected':'отклонена','cancelled':'отменена'}
    await message.answer(f"Последняя заявка в «{_safe(row['name'])}»: {_safe(labels.get(row['status'], row['status']))}.")

@router.message(F.text.regexp(r"(?i)^роль в клане$"))
async def clan_role_status(message: Message):
    """16. Показать свою роль в клане."""
    if not message.from_user: return
    conn = await db.get_db(); clan = await _member_clan(conn, message.chat.id, message.from_user.id)
    if not clan: await message.answer("Вы не состоите в клане."); return
    roles = {'owner':'владелец','deputy':'заместитель','member':'участник'}
    await message.answer(f"Ваша роль в клане «{_safe(clan['name'])}»: {roles.get(clan['role'], clan['role'])}.")

@router.message(F.text.regexp(r"(?i)^поиск клана\s+"))
async def search_clans(message: Message):
    """17. Поиск кланов по части названия."""
    if not message.text: return
    parts = message.text.split(maxsplit=2)
    query = _clean_name(parts[2] if len(parts) > 2 else "")
    if len(query) < 2: await message.reply("Введите хотя бы 2 символа для поиска."); return
    conn = await db.get_db(); cur = await conn.execute("SELECT c.name,c.description,COUNT(cm.user_id) members FROM clans c LEFT JOIN clan_members cm ON cm.clan_id=c.id WHERE c.chat_id=? AND c.name LIKE ? COLLATE NOCASE GROUP BY c.id ORDER BY members DESC LIMIT 20", (message.chat.id, f"%{query}%")); rows = await cur.fetchall()
    if not rows: await message.answer("Подходящих кланов не найдено."); return
    await message.answer("🔎 Результаты поиска:\n" + "\n".join(f"• {_safe(r['name'])} — {r['members']} уч. — {_safe(r['description'] or 'без описания')}" for r in rows))

@router.message(F.text.regexp(r"(?i)^клан тип (открытый|закрытый)$"))
async def set_clan_privacy(message: Message):
    """Владелец переключает клан между открытым и закрытым режимом."""
    if not message.from_user or not message.text:
        return
    mode = "open" if message.text.lower().endswith("открытый") else "closed"
    conn = await db.get_db()
    clan = await _send_owner_only(message, conn)
    if not clan:
        return
    await conn.execute("UPDATE clans SET privacy=? WHERE id=?", (mode, clan["id"]))
    await conn.commit()
    label = "открытый: вступить может любой, если нет запрета" if mode == "open" else "закрытый: вступление только по приглашению"
    await message.answer(f"Настройки клана обновлены. Теперь клан {label}.")


@router.message(F.text.regexp(r"(?i)^клан вступить\s+\d+$"))
async def join_clan_by_number(message: Message):
    """Вступить в открытый клан по номеру из списка кланов."""
    if not message.from_user or not message.text:
        return
    try:
        clan_id = int(message.text.split()[-1])
    except ValueError:
        await message.reply("Пример: клан вступить 12")
        return
    conn = await db.get_db()
    if await _member_clan(conn, message.chat.id, message.from_user.id):
        await message.reply("Вы уже состоите в клане.")
        return
    cur = await conn.execute("SELECT id,name,privacy FROM clans WHERE id=? AND chat_id=?", (clan_id, message.chat.id))
    clan = await cur.fetchone()
    if not clan:
        await message.reply("Клан с таким номером не найден в этом чате.")
        return
    cur = await conn.execute("SELECT 1 FROM clan_bans WHERE clan_id=? AND user_id=?", (clan_id, message.from_user.id))
    if await cur.fetchone():
        await message.reply("Владелец клана запретил вам вступление.")
        return
    if clan["privacy"] == "closed":
        await message.reply("Клан закрытый. Для вступления нужно приглашение владельца.")
        return
    try:
        async with db.transaction() as tx:
            if await _user_clan(tx, message.chat.id, message.from_user.id):
                await message.reply("Вы уже состоите в клане.")
                return
            cur = await tx.execute("SELECT privacy,name FROM clans WHERE id=? AND chat_id=?", (clan_id, message.chat.id))
            current_clan = await cur.fetchone()
            if not current_clan or current_clan['privacy'] == 'closed':
                await message.reply("Клан закрыт или больше не существует.")
                return
            cur = await tx.execute("SELECT 1 FROM clan_bans WHERE clan_id=? AND user_id=?", (clan_id, message.from_user.id))
            if await cur.fetchone():
                await message.reply("Владелец клана запретил вам вступление.")
                return
            await tx.execute("INSERT INTO clan_members(clan_id,user_id,role) VALUES(?,?,'member')", (clan_id, message.from_user.id))
            await tx.execute(
                "UPDATE clan_invites SET status='cancelled' WHERE user_id=? AND status='pending' "
                "AND clan_id IN (SELECT id FROM clans WHERE chat_id=?)",
                (message.from_user.id, message.chat.id),
            )
    except Exception:
        await message.reply("Не удалось вступить. Возможно, вы уже состоите в другом клане.")
        return
    await message.answer(f"🏰 Вы вступили в клан «{_safe(clan['name'])}»!")


@router.message(F.text.regexp(r"(?i)^клан запретить\s+\d+$"))
async def ban_from_clan(message: Message):
    """Запретить пользователю вступать в собственный клан."""
    if not message.from_user or not message.text:
        return
    target_id = _target_id(message)
    if not target_id:
        await message.reply("Пример: клан запретить 123456789")
        return
    conn = await db.get_db()
    clan = await _send_owner_only(message, conn)
    if not clan:
        return
    if target_id == clan["owner_id"]:
        await message.reply("Нельзя запретить вступление самому себе.")
        return
    async with db.transaction() as tx:
        current_clan = await _owned_clan(tx, message.chat.id, message.from_user.id)
        if not current_clan or current_clan['id'] != clan['id']:
            await message.reply("Владение кланом изменилось; повторите команду.")
            return
        await tx.execute("DELETE FROM clan_members WHERE clan_id=? AND user_id=?", (clan["id"], target_id))
        await tx.execute("INSERT OR REPLACE INTO clan_bans(clan_id,user_id,by_id,created_at) VALUES(?,?,?,?)", (clan["id"], target_id, message.from_user.id, time.time()))
        await tx.execute("UPDATE clan_invites SET status='cancelled' WHERE clan_id=? AND user_id=? AND status='pending'", (clan["id"], target_id))
    await message.answer(f"Пользователю {target_id} запрещено вступать в клан «{_safe(clan['name'])}».")


@router.message(F.text.regexp(r"(?i)^клан разрешить\s+\d+$"))
async def unban_from_clan(message: Message):
    """Снять запрет на вступление в клан."""
    if not message.from_user or not message.text:
        return
    target_id = _target_id(message)
    if not target_id:
        await message.reply("Пример: клан разрешить 123456789")
        return
    conn = await db.get_db()
    clan = await _send_owner_only(message, conn)
    if not clan:
        return
    cur = await conn.execute("DELETE FROM clan_bans WHERE clan_id=? AND user_id=?", (clan["id"], target_id))
    await conn.commit()
    await message.answer("Запрет снят." if cur.rowcount else "У этого пользователя нет запрета в вашем клане.")


@router.message(F.text.regexp(r"(?i)^клан коины\s+\d+$"))
async def contribute_clan_coins(message: Message):
    """Внести положительное количество валюты участника в казну клана."""
    if not message.from_user or not message.text:
        return
    try:
        amount = int(message.text.split()[-1])
    except ValueError:
        await message.reply("Пример: клан коины 100")
        return
    if amount <= 0 or amount > 10**9:
        await message.reply("Сумма должна быть от 1 до 1 000 000 000.")
        return
    conn = await db.get_db()
    clan = await _member_clan(conn, message.chat.id, message.from_user.id)
    if not clan:
        await message.reply("Сначала вступите в клан.")
        return
    await db.ensure_member(message.chat.id, message.from_user.id)
    async with db.tx_lock:
        tx_conn = await db.get_transaction_db()
        try:
            await tx_conn.execute("BEGIN IMMEDIATE")
            cur = await tx_conn.execute("SELECT 1 FROM clan_members WHERE clan_id=? AND user_id=?", (clan["id"], message.from_user.id))
            if not await cur.fetchone():
                await tx_conn.rollback()
                await message.reply("Вы больше не состоите в этом клане.")
                return
            cur = await tx_conn.execute("SELECT id FROM clans WHERE id=? AND chat_id=?", (clan["id"], message.chat.id))
            if not await cur.fetchone():
                await tx_conn.rollback()
                await message.reply("Клан больше не существует.")
                return
            cur = await tx_conn.execute("SELECT balance FROM members WHERE chat_id=? AND user_id=?", (message.chat.id, message.from_user.id))
            row = await cur.fetchone()
            if not row or row["balance"] < amount:
                await tx_conn.rollback()
                await message.reply("Недостаточно средств на балансе.")
                return
            await tx_conn.execute("UPDATE members SET balance=balance-? WHERE chat_id=? AND user_id=?", (amount, message.chat.id, message.from_user.id))
            cur = await tx_conn.execute("UPDATE clans SET coins=coins+? WHERE id=? AND chat_id=?", (amount, clan["id"], message.chat.id))
            if cur.rowcount != 1:
                await tx_conn.rollback()
                await message.reply("Не удалось пополнить казну: клан не найден.")
                return
            await tx_conn.commit()
        except Exception:
            await tx_conn.rollback()
            raise
        finally:
            await tx_conn.close()
    await message.answer(f"🪙 В казну клана «{_safe(clan['name'])}» внесено {amount}. Спасибо за вклад!")


@router.message(F.text.regexp(r"(?i)^клан созвать всех$"))
async def summon_clan_members(message: Message):
    """Сформировать упоминание известных участников клана без массовых сообщений."""
    if not message.from_user:
        return
    conn = await db.get_db()
    clan = await _member_clan(conn, message.chat.id, message.from_user.id)
    if not clan:
        await message.reply("Вы не состоите в клане.")
        return
    if clan["role"] not in ("owner", "deputy"):
        await message.reply("Созывать участников может владелец или заместитель.")
        return
    cur = await conn.execute("SELECT cm.user_id,u.first_name,u.username FROM clan_members cm LEFT JOIN users u ON u.user_id=cm.user_id WHERE cm.clan_id=? ORDER BY cm.user_id LIMIT 40", (clan["id"],))
    rows = await cur.fetchall()
    mentions = []
    for row in rows:
        if row["username"]:
            mentions.append("@" + row["username"])
        elif row["first_name"]:
            name = row["first_name"].replace("[", "").replace("]", "")
            mentions.append(f'<a href="tg://user?id={int(row["user_id"])}">{_safe(name)}</a>')
    if not mentions:
        await message.answer("Не удалось создать упоминания: участники ещё не взаимодействовали с ботом.")
        return
    await message.answer(f"📣 Созыв клана «{_safe(clan['name'])}»:\n" + " ".join(mentions[:40]))


@router.message(F.text.regexp(r"(?i)^клан номер$"))
async def my_clan_number(message: Message):
    if not message.from_user:
        return
    conn = await db.get_db()
    clan = await _member_clan(conn, message.chat.id, message.from_user.id)
    if not clan:
        await message.answer("Вы не состоите в клане.")
        return
    await message.answer(f"Номер вашего клана «{_safe(clan['name'])}»: {clan['id']}")


@router.message(F.text.regexp(r"(?i)^клан казна$"))
async def show_clan_treasury(message: Message):
    if not message.from_user:
        return
    conn = await db.get_db()
    clan = await _member_clan(conn, message.chat.id, message.from_user.id)
    if not clan:
        await message.answer("Вы не состоите в клане.")
        return
    cur = await conn.execute("SELECT coins,privacy FROM clans WHERE id=?", (clan["id"],))
    row = await cur.fetchone()
    await message.answer(f"🏦 Казна клана «{_safe(clan['name'])}»: {row['coins']} коинов\nТип: {'открытый' if row['privacy']=='open' else 'закрытый'}")


@router.message(F.text.regexp(r"(?i)^клан участники\s+\d+$"))
async def list_members_by_clan_number(message: Message):
    if not message.text:
        return
    try:
        clan_id = int(message.text.split()[-1])
    except ValueError:
        await message.reply("Пример: клан участники 12")
        return
    conn = await db.get_db()
    cur = await conn.execute("SELECT id,name FROM clans WHERE id=? AND chat_id=?", (clan_id, message.chat.id))
    clan = await cur.fetchone()
    if not clan:
        await message.reply("Клан не найден в этом чате.")
        return
    cur = await conn.execute("SELECT cm.user_id,cm.role,u.first_name,u.username FROM clan_members cm LEFT JOIN users u ON u.user_id=cm.user_id WHERE cm.clan_id=? ORDER BY CASE cm.role WHEN 'owner' THEN 0 WHEN 'deputy' THEN 1 ELSE 2 END,cm.user_id LIMIT 40", (clan_id,))
    rows = await cur.fetchall()
    lines = [f"👥 Участники клана «{_safe(clan['name'])}»:"]
    for row in rows:
        label = row['first_name'] or ("@" + row['username'] if row['username'] else str(row['user_id']))
        lines.append(f"• {_safe(label)} — {_safe(row['role'])}")
    await message.answer("\n".join(lines))


@router.message(F.text.regexp(r"(?i)^клан помощь$"))
async def clan_help(message: Message):
    """18. Краткая справка по командам кланов."""
    await message.answer("🏰 Команды кланов:\nсоздать клан <название>\nмой клан\nучастники клана\nвступить в клан <название> (для открытого клана)\nклан вступить <номер>\nклан тип открытый / клан тип закрытый\nклан запретить <ID> / клан разрешить <ID>\nклан коины <число> / клан казна\nклан созвать всех\nпригласить в клан <ID>\nмои приглашения\nпринять приглашение <название>\nотклонить приглашение <название>\nописание клана <текст>\nпереименовать клан <название>\nудалить клан\nназначить зама <ID> / снять зама <ID>\nпередать клан <ID>\nисключить из клана <ID>\nвыйти из клана\nсписок кланов / топ кланов / поиск клана <часть названия>")

@router.message(F.text.regexp(r"(?i)^очистить заявки клана$"))
async def clear_clan_requests(message: Message):
    """19. Владелец закрывает все ожидающие заявки своего клана."""
    if not message.from_user: return
    conn = await db.get_db(); clan = await _send_owner_only(message, conn)
    if not clan: return
    cur = await conn.execute("UPDATE clan_requests SET status='rejected' WHERE clan_id=? AND status='pending'", (clan['id'],)); await conn.commit()
    await message.answer(f"Закрыто заявок: {cur.rowcount}.")

@router.message(F.text.regexp(r"(?i)^отменить приглашения клана$"))
async def cancel_clan_invites(message: Message):
    """20. Владелец отменяет все ожидающие приглашения своего клана."""
    if not message.from_user: return
    conn = await db.get_db(); clan = await _send_owner_only(message, conn)
    if not clan: return
    cur = await conn.execute("UPDATE clan_invites SET status='cancelled' WHERE clan_id=? AND status='pending'", (clan['id'],)); await conn.commit()
    await message.answer(f"Отменено приглашений: {cur.rowcount}.")
