"""Базовые команды: помощь, профиль, регистрация пользователей."""
from html import escape
from datetime import datetime
from aiogram import Router, F
from aiogram.types import Message
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart

import database as db
from config import BOT_NAME
from utils import ROLE_NAMES, resolve_target, mention

router = Router()

HELP_TEXT = f"""
<b>{escape(BOT_NAME, quote=True)} — список команд</b>

<b>Основное:</b>
• помощь — этот список
• мой id — ваш Telegram ID
• кто админ — список администраторов Telegram
• инфо группы — информация о чате
• профиль / профиль @user — информация об участнике

<b>Модерация:</b>
• мут @user 10м причина — мут на время (с/м/ч/д)
• мут [@user] [срок] [причина] — по умолчанию 1 час
• размут / снять мут / анмут @user — снять мут
• варн @user причина — предупреждение
• варны @user — список варнов
• мои варны / варнлист — личные варны / общий список (модераторы+)
• снять варн @user — убрать последний варн
• снять все варны @user — очистить все варны участника
• проверить мут @user — проверить состояние мута (модераторы+)
• муты / банлист / причина @user — списки и причина наказания (модераторы+)
• бан @user причина / бан @user 1д причина — бан
• разбан @user — снять бан
• кик @user — исключить из группы

<b>Экономика (чапы):</b>
• баланс / баланс @user — баланс чапов
• бонус — ежедневная награда (раз в 24 часа)
• перевести @user 100 — перевести чапы
• топ — рейтинг богачей
• магазин / купить &lt;товар&gt; / инвентарь / подарить @user &lt;товар&gt;
• история — последние операции
• задания / задания забрать / достижения — ежедневные цели и награды
• премиум статус [@user] — проверить премиум; админы: премиум выдать @user 30д / премиум снять @user
• статус / статус установить &lt;текст&gt; — персональный статус (премиум)
• выдать @user 100 / забрать @user 100 — для админов
• товар добавить &lt;имя&gt; &lt;цена&gt; / товар удалить &lt;имя&gt; — для админов

<b>Браки:</b>
• брак @user — предложить брак
• принять брак / отклонить брак — ответить на предложение
• мой брак — информация о браке
• развод — расторгнуть брак
• восстановить брак — запросить взаимное восстановление в течение 3 дней
• принять/отклонить восстановление брака — ответить на запрос

<b>Кланы:</b>
• создать клан Название
• мой клан / клан участники
• клан заявка Название / моя заявка в клан / отменить заявку в клан / клан выйти

<b>Игры:</b>
• монетка / орёл или решка — подбросить монетку
• кости / кубик — бросить кубик
• случайное число [от] [до] — случайное число от 1 до 100 или в своём диапазоне

<b>Заметки:</b>
• заметка создать имя (ответом на текст)
• заметка имя / заметка удалить имя / заметки
• автоответ добавить триггер => ответ / автоответ удалить триггер / автоответы

<b>Панель владельца:</b>
• /admin — личная панель владельца бота (только OWNER_TELEGRAM_ID)
• агент добавить &lt;ID_группы&gt; &lt;ID_пользователя&gt;
• агент удалить &lt;ID_группы&gt; &lt;ID_пользователя&gt;
• агенты &lt;ID_группы&gt; — команды управления агентами только в личном чате с ботом

<b>Настройки (админы):</b>
• настройки — просмотр
• настройка антиспам вкл/выкл (также приветствия, заметки, автоответы, статистика, экономика, браки, кланы, игры)
• настройка антиспам действие warn/delete/mute/case
• приветствие текст &lt;текст&gt; — настроить сообщение для новых участников
• роль @user модератор — назначить роль
• журнал — последние действия модерации
• ответьте на сообщение и напишите «спам» — агент отправит его на ручную проверку
• жалоба @user причина (или ответом на сообщение) — отправить жалобу модераторам
• обжалование &lt;текст&gt; — подать апелляцию на активное наказание
• дела / дело ID / дело принять ID / дело отклонить ID — очередь дел для модераторов
""".strip()


@router.message(CommandStart())
@router.message(F.text.regexp(r"(?i)^(помощь|help|команды)$"))
async def cmd_help(message: Message):
    if message.from_user:
        await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(HELP_TEXT, parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)^(мой id|мой айди|id)$"))
async def cmd_my_id(message: Message):
    if not message.from_user:
        await message.reply("Не удалось определить ID отправителя.")
        return
    await message.answer(f"🆔 Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(F.text.regexp(r"(?i)^(кто админ|админы группы|администраторы группы)$"))
async def cmd_group_admins(message: Message):
    try:
        admins = await message.bot.get_chat_administrators(message.chat.id)
    except TelegramAPIError:
        await message.reply("Не удалось получить список администраторов через Telegram API.")
        return
    lines = []
    for member in admins[:50]:
        user = member.user
        role = "создатель" if member.status == "creator" else "администратор"
        lines.append(f"• {mention(user.id, user.first_name)} — {role}")
    if not lines:
        await message.answer("Telegram не вернул список администраторов.")
        return
    await message.answer("👮 <b>Администраторы группы:</b>\n" + "\n".join(lines), parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)^(инфо группы|информация о группе)$"))
async def cmd_group_info(message: Message):
    chat = message.chat
    try:
        count = await message.bot.get_chat_member_count(chat.id)
    except TelegramAPIError:
        count = None
    title = escape(str(chat.title or "Личный чат"), quote=True)
    member_line = f"👥 Участников: {count}\n" if count is not None else ""
    await message.answer(
        f"ℹ️ <b>Информация о чате</b>\n🏷 Название: {title}\n🆔 ID: <code>{chat.id}</code>\n{member_line}"
        f"💬 Тип: {escape(str(chat.type), quote=True)}",
        parse_mode="HTML",
    )


@router.message(F.text.regexp(r"(?i)^(профиль|мой профиль)(?:\s+(?:@\w+|\d+))?$"))
async def cmd_profile(message: Message):
    if not message.from_user:
        await message.reply("Профиль доступен только обычным аккаунтам, не сообщениям от имени канала.")
        return
    await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    chat_id = message.chat.id
    args = message.text.split()[1:] if message.text else []
    target_id, err = await resolve_target(message, args, db)
    if err:
        if args or message.reply_to_message:
            await message.reply(err)
            return
        target_id = message.from_user.id
    user = await db.get_user(target_id)
    if not user:
        await message.answer("Этот пользователь ещё не писал в группе — данных нет.")
        return
    role = await db.get_role(chat_id, target_id)
    await db.ensure_member(chat_id, target_id)
    cur = await (await db.get_db()).execute(
        "SELECT balance FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, target_id)
    )
    row = await cur.fetchone()
    balance = row["balance"] if row else 0
    cur = await (await db.get_db()).execute(
        "SELECT COUNT(*) AS c FROM punishments WHERE chat_id = ? AND user_id = ? AND type = 'warn' AND active = 1",
        (chat_id, target_id),
    )
    warns = (await cur.fetchone())["c"]
    premium_until = await db.premium_expiry(chat_id, target_id)
    premium_text = datetime.fromtimestamp(premium_until).strftime("%d.%m.%Y") if premium_until else "нет"
    premium_status = await db.premium_profile_status(chat_id, target_id)
    status_line = f"\n✨ Статус: {escape(premium_status, quote=True)}" if premium_status else ""
    text = (
        f"👤 <b>Профиль:</b> {mention(target_id, user['first_name'])}\n"
        f"🆔 ID: <code>{target_id}</code>\n"
        f"🎖 Роль: {ROLE_NAMES.get(role, role)}\n"
        f"⭐ Уровень: {user['level']} (опыт: {user['xp']})\n"
        f"💰 Баланс: {balance}\n"
        f"✉️ Сообщений: {user['messages_count']}\n"
        f"⚠️ Активных варнов: {warns}\n"
        f"💎 Премиум до: {premium_text}{status_line}"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(F.text)
async def track_message(message: Message):
    """Считает сообщения и опыт для всех участников (если включено в настройках)."""
    if (not message.from_user or message.from_user.is_bot
            or message.chat.type not in {"group", "supergroup"}):
        return
    await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    settings = await db.get_settings(message.chat.id)
    if settings.get("count_messages", True):
        from config import XP_PER_MESSAGE
        new_level = await db.bump_message_stats(message.chat.id, message.from_user.id, XP_PER_MESSAGE)
        if new_level:
            await message.answer(f"🎉 {mention(message.from_user.id, message.from_user.first_name)}, поздравляем! Новый уровень: <b>{new_level}</b>.", parse_mode="HTML")
    else:
        await db.bump_task_progress(message.chat.id, message.from_user.id, "messages")
