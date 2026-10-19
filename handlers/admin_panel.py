"""Личная панель владельца бота. Доступна только ID из OWNER_TELEGRAM_ID."""
from html import escape
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.dispatcher.event.bases import UNHANDLED

import database as db
from config import OWNER_TELEGRAM_ID, BOT_NAME

router = Router()

# Незавершённый ввод владельца хранится только в памяти текущего процесса.
_pending_agent_action: dict[int, tuple[str, int]] = {}

BOOL_SETTINGS = {
    "antispam": "Антиспам",
    "greetings": "Приветствия",
    "notes": "Заметки",
    "autoreplies": "Автоответы",
    "count_messages": "Статистика сообщений",
    "games": "Игры",
    "economy": "Экономика",
    "marriages": "Браки",
    "clans": "Кланы",
}


def _is_owner(user_id: int | None) -> bool:
    return bool(user_id and OWNER_TELEGRAM_ID > 0 and user_id == OWNER_TELEGRAM_ID)


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="owner:stats")],
        [InlineKeyboardButton(text="⚙️ Настройки модулей", callback_data="owner:settings")],
        [InlineKeyboardButton(text="🤖 Управление агентами", callback_data="owner:agents")],
        [InlineKeyboardButton(text="🔄 Обновить панель", callback_data="owner:home")],
    ])


async def _stats_text() -> str:
    conn = await db.get_db()
    queries = {
        "users": "SELECT COUNT(*) FROM users",
        "groups": "SELECT COUNT(*) FROM settings WHERE chat_id < 0",
        "clans": "SELECT COUNT(*) FROM clans",
        "memberships": "SELECT COUNT(*) FROM clan_members",
        "active_punishments": "SELECT COUNT(*) FROM punishments WHERE active=1",
        "notes": "SELECT COUNT(*) FROM notes",
        "autoreplies": "SELECT COUNT(*) FROM autoreplies",
    }
    values = {}
    for key, sql in queries.items():
        cur = await conn.execute(sql)
        values[key] = (await cur.fetchone())[0]
    return (
        "📊 <b>Статистика бота</b>\n"
        f"👤 Зарегистрировано пользователей: {values['users']}\n"
        f"👥 Групп с сохранёнными настройками: {values['groups']}\n"
        f"🏰 Кланов: {values['clans']}\n"
        f"🧑‍🤝‍🧑 Участий в кланах: {values['memberships']}\n"
        f"⚠️ Активных записей наказаний: {values['active_punishments']}\n"
        f"📝 Заметок: {values['notes']}\n"
        f"🤖 Автоответов: {values['autoreplies']}"
    )


async def _settings_keyboard() -> tuple[str, InlineKeyboardMarkup]:
    """Показывает список групп; настройки не смешиваются между чатами."""
    conn = await db.get_db()
    cur = await conn.execute("SELECT chat_id FROM settings WHERE chat_id < 0 ORDER BY chat_id")
    rows = await cur.fetchall()
    if not rows:
        return ("⚙️ Настройки модулей\nПока нет групп с сохранёнными настройками. "
                "Добавьте бота в группу и отправьте там сообщение.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="owner:home")]
                ]))
    buttons = [[InlineKeyboardButton(
        text=f"👥 Группа {row['chat_id']}",
        callback_data=f"owner:group:{row['chat_id']}",
    )] for row in rows[:50]]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="owner:home")])
    return ("⚙️ <b>Настройки модулей</b>\nВыберите группу, настройки которой хотите изменить:",
            InlineKeyboardMarkup(inline_keyboard=buttons))


async def _group_settings_keyboard(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    conn = await db.get_db()
    cur = await conn.execute("SELECT 1 FROM settings WHERE chat_id=? AND chat_id < 0", (chat_id,))
    if not await cur.fetchone():
        return ("Группа не найдена в сохранённых настройках.", InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К списку групп", callback_data="owner:settings")]
        ]))
    settings = await db.get_settings(chat_id)
    lines = ["⚙️ <b>Переключатели модулей</b>", f"ID группы: <code>{chat_id}</code>", ""]
    rows = []
    for key, title in BOOL_SETTINGS.items():
        enabled = bool(settings.get(key, False))
        lines.append(f"{'🟢' if enabled else '🔴'} {title}: {'вкл' if enabled else 'выкл'}")
        rows.append([InlineKeyboardButton(
            text=f"{'🟢' if enabled else '🔴'} {title}",
            callback_data=f"owner:toggle:{chat_id}:{key}",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ К списку групп", callback_data="owner:settings")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _agents_keyboard() -> tuple[str, InlineKeyboardMarkup]:
    conn = await db.get_db()
    cur = await conn.execute("SELECT chat_id FROM settings WHERE chat_id < 0 ORDER BY chat_id")
    groups = await cur.fetchall()
    if not groups:
        return "🤖 Управление агентами\nНет зарегистрированных групп. Сначала добавьте бота в группу и дождитесь первого сообщения.", InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="owner:home")]
        ])
    buttons = [[InlineKeyboardButton(text=f"Группа {row['chat_id']}", callback_data=f"owner:agents_group:{row['chat_id']}")] for row in groups[:50]]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="owner:home")])
    return "🤖 <b>Управление агентами</b>\nВыберите группу:", InlineKeyboardMarkup(inline_keyboard=buttons)


async def _agents_group_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    conn = await db.get_db()
    cur = await conn.execute("SELECT 1 FROM settings WHERE chat_id=? AND chat_id<0", (chat_id,))
    if not await cur.fetchone():
        return "Группа не найдена.", InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К группам", callback_data="owner:agents")]
        ])
    cur = await conn.execute(
        "SELECT m.user_id,u.first_name,u.username FROM members m LEFT JOIN users u ON u.user_id=m.user_id "
        "WHERE m.chat_id=? AND m.role='agent' ORDER BY m.user_id LIMIT 20", (chat_id,)
    )
    agents = await cur.fetchall()
    lines = ["🤖 <b>Агенты группы</b>", f"ID группы: <code>{chat_id}</code>"]
    if agents:
        for agent in agents:
            name = agent['first_name'] or (f"@{agent['username']}" if agent['username'] else "без имени")
            lines.append(f"• {escape(str(name)[:50], quote=True)} — <code>{agent['user_id']}</code>")
    else:
        lines.append("Агентов пока нет.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить агента", callback_data=f"owner:agents_add:{chat_id}")],
        [InlineKeyboardButton(text="➖ Снять роль агента", callback_data=f"owner:agents_remove:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ К группам", callback_data="owner:agents")],
    ])
    return "\n".join(lines), keyboard


@router.message(Command("admin", "owner"))
@router.message(F.text.regexp(r"(?i)^админ панель$"))
async def owner_panel(message: Message):
    if not _is_owner(message.from_user.id if message.from_user else None):
        # Не раскрываем существование и устройство панели посторонним.
        return
    await message.answer(
        f"🛠 <b>{escape(BOT_NAME, quote=True)} — панель владельца</b>\nВыберите раздел:",
        reply_markup=_main_keyboard(),
    )


@router.callback_query(F.data.startswith("owner:"))
async def owner_panel_callbacks(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    action = callback.data or "owner:home"
    if action == "owner:home":
        await callback.message.edit_text(
            f"🛠 <b>{escape(BOT_NAME, quote=True)} — панель владельца</b>\nВыберите раздел:",
            reply_markup=_main_keyboard(),
        )
    elif action == "owner:stats":
        await callback.message.edit_text(
            await _stats_text(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="owner:stats")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="owner:home")],
            ]),
        )
    elif action == "owner:agents":
        text, keyboard = await _agents_keyboard()
        await callback.message.edit_text(text, reply_markup=keyboard)
    elif action.startswith("owner:agents_group:"):
        raw_chat_id = action.split(":", 2)[2]
        if not raw_chat_id.lstrip("-").isdigit():
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        text, keyboard = await _agents_group_view(int(raw_chat_id))
        await callback.message.edit_text(text, reply_markup=keyboard)
    elif action.startswith("owner:agents_add:") or action.startswith("owner:agents_remove:"):
        parts = action.split(":")
        if len(parts) != 3 or not parts[2].lstrip("-").isdigit():
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        chat_id = int(parts[2])
        if chat_id >= 0:
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        action_name = "add" if parts[1] == "agents_add" else "remove"
        _pending_agent_action[callback.from_user.id] = (action_name, chat_id)
        verb = "добавить" if action_name == "add" else "снять роль агента с"
        await callback.message.answer(
            f"Отправьте числовой Telegram ID участника, с которого нужно {verb} в группе <code>{chat_id}</code>.\n"
            "Для отмены отправьте /cancel.", parse_mode="HTML"
        )
    elif action == "owner:settings":
        text, keyboard = await _settings_keyboard()
        await callback.message.edit_text(text, reply_markup=keyboard)
    elif action.startswith("owner:group:"):
        raw_chat_id = action.split(":", 2)[2]
        if not raw_chat_id.lstrip("-").isdigit():
            await callback.answer("Некорректная группа.", show_alert=True)
            return
        text, keyboard = await _group_settings_keyboard(int(raw_chat_id))
        await callback.message.edit_text(text, reply_markup=keyboard)
    elif action.startswith("owner:toggle:"):
        parts = action.split(":")
        if len(parts) != 4 or not parts[2].lstrip("-").isdigit() or parts[3] not in BOOL_SETTINGS:
            await callback.answer("Некорректная настройка.", show_alert=True)
            return
        chat_id, key = int(parts[2]), parts[3]
        settings = await db.get_settings(chat_id)
        await db.set_setting(chat_id, key, not bool(settings.get(key, False)))
        text, keyboard = await _group_settings_keyboard(chat_id)
        await callback.message.edit_text(text, reply_markup=keyboard)
    else:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    await callback.answer()


@router.message(F.chat.type == "private", F.text.regexp(r"(?i)^агент добавить\s+-\d+\s+\d+$"))
async def owner_add_agent(message: Message):
    """Владелец назначает агента в указанной группе из личного чата с ботом."""
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    _pending_agent_action.pop(message.from_user.id, None)
    parts = message.text.split()
    if len(parts[2]) > 19 or len(parts[3]) > 19:
        await message.reply("ID слишком длинный.")
        return
    chat_id, user_id = int(parts[2]), int(parts[3])
    if chat_id >= 0 or user_id <= 0 or user_id > 9_223_372_036_854_775_807:
        await message.reply("Нужны отрицательный ID группы и положительный ID пользователя.")
        return
    conn = await db.get_db()
    cur = await conn.execute("SELECT 1 FROM settings WHERE chat_id=?", (chat_id,))
    if not await cur.fetchone():
        await message.reply("Эта группа ещё не зарегистрирована. Добавьте бота в группу и дождитесь первого сообщения.")
        return
    current = await db.get_role(chat_id, user_id)
    if current not in {"user", "agent"}:
        await message.reply(f"У пользователя уже есть роль «{escape(current)}». Сначала проверьте её вручную.")
        return
    await db.set_role(chat_id, user_id, "agent")
    await db.log_action(chat_id, message.from_user.id, "owner_agent_add", user_id, "agent")
    await message.answer(f"✅ Пользователь <code>{user_id}</code> назначен агентом в группе <code>{chat_id}</code>.")


@router.message(F.chat.type == "private", F.text.regexp(r"(?i)^агент удалить\s+-\d+\s+\d+$"))
async def owner_remove_agent(message: Message):
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    _pending_agent_action.pop(message.from_user.id, None)
    parts = message.text.split()
    if len(parts[2]) > 19 or len(parts[3]) > 19:
        await message.reply("ID слишком длинный.")
        return
    chat_id, user_id = int(parts[2]), int(parts[3])
    if chat_id >= 0 or user_id <= 0 or user_id > 9_223_372_036_854_775_807:
        await message.reply("Нужны отрицательный ID группы и положительный ID пользователя.")
        return
    current = await db.get_role(chat_id, user_id)
    if current != "agent":
        await message.reply("В этой группе у пользователя нет роли агента.")
        return
    await db.set_role(chat_id, user_id, "user")
    await db.log_action(chat_id, message.from_user.id, "owner_agent_remove", user_id, "agent → user")
    await message.answer(f"✅ Роль агента снята с <code>{user_id}</code> в группе <code>{chat_id}</code>.")


@router.message(F.chat.type == "private", F.text.regexp(r"(?i)^агенты\s+-\d+$"))
async def owner_list_agents(message: Message):
    if not _is_owner(message.from_user.id if message.from_user else None):
        return
    _pending_agent_action.pop(message.from_user.id, None)
    raw_chat_id = message.text.split()[1]
    if len(raw_chat_id) > 20:
        await message.reply("ID группы слишком длинный.")
        return
    chat_id = int(raw_chat_id)
    if chat_id >= 0:
        await message.reply("Укажите отрицательный ID группы.")
        return
    conn = await db.get_db()
    cur = await conn.execute(
        "SELECT m.user_id,u.first_name,u.username FROM members m "
        "LEFT JOIN users u ON u.user_id=m.user_id WHERE m.chat_id=? AND m.role='agent' ORDER BY m.user_id LIMIT 20",
        (chat_id,),
    )
    rows = await cur.fetchall()
    if not rows:
        await message.answer(f"В группе <code>{chat_id}</code> агентов пока нет.")
        return
    lines = []
    for row in rows:
        name = row["first_name"] or ("@" + row["username"] if row["username"] else "без имени")
        lines.append(f"• {escape(str(name)[:50], quote=True)} — <code>{row['user_id']}</code>")
    await message.answer(f"🤖 <b>Агенты группы <code>{chat_id}</code>:</b>\n" + "\n".join(lines))


@router.message(F.chat.type == "private", F.text)
async def owner_agent_pending_input(message: Message):
    """Обрабатывает ID участника, введённый через кнопки панели владельца."""
    if not message.from_user or not _is_owner(message.from_user.id):
        return UNHANDLED
    pending = _pending_agent_action.get(message.from_user.id)
    if not pending:
        return UNHANDLED
    raw = message.text.strip()
    if raw.casefold() in {"/cancel", "отмена", "отменить"}:
        _pending_agent_action.pop(message.from_user.id, None)
        await message.answer("Действие с агентом отменено.")
        return
    if not raw.isascii() or not raw.isdigit() or len(raw) > 19 or int(raw) <= 0 or int(raw) > 9_223_372_036_854_775_807:
        await message.reply("Нужен корректный положительный числовой Telegram ID. Или отправьте /cancel.")
        return
    action, chat_id = pending
    user_id = int(raw)
    conn = await db.get_db()
    cur = await conn.execute("SELECT 1 FROM settings WHERE chat_id=? AND chat_id<0", (chat_id,))
    if not await cur.fetchone():
        _pending_agent_action.pop(message.from_user.id, None)
        await message.reply("Группа больше не зарегистрирована. Откройте панель и выберите группу заново.")
        return
    current = await db.get_role(chat_id, user_id)
    if action == "add":
        if current not in {"user", "agent"}:
            await message.reply(f"У пользователя уже роль «{escape(current, quote=True)}». Её нельзя заменить агентом автоматически.")
            return
        await db.set_role(chat_id, user_id, "agent")
        await db.log_action(chat_id, message.from_user.id, "owner_agent_add", user_id, "agent")
        result = f"✅ Пользователь <code>{user_id}</code> назначен агентом в группе <code>{chat_id}</code>."
    else:
        if current != "agent":
            await message.reply("У этого пользователя нет роли агента в выбранной группе.")
            return
        await db.set_role(chat_id, user_id, "user")
        await db.log_action(chat_id, message.from_user.id, "owner_agent_remove", user_id, "agent → user")
        result = f"✅ Роль агента снята с <code>{user_id}</code> в группе <code>{chat_id}</code>."
    _pending_agent_action.pop(message.from_user.id, None)
    await message.answer(result, parse_mode="HTML")
