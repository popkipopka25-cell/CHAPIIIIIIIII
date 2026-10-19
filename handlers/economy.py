"""Команды экономики: баланс, бонус, переводы, топ, магазин, инвентарь, история."""
from html import escape


from aiogram import Router, F
from aiogram.types import Message

import database as db
import economy
from utils import has_at_least, resolve_target, mention, format_duration
from handlers.moderation import effective_role

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


async def _has_from_user(message: Message) -> bool:
    """Не обрабатывает команды, отправленные от имени канала без обычного пользователя."""
    return message.from_user is not None


router.message.filter(_has_from_user)


async def _economy_on(message: Message) -> bool:
    settings = await db.get_settings(message.chat.id)
    if not settings.get("economy", True):
        await message.reply("Экономика в этой группе выключена (настройка «economy»).")
        return False
    return True


@router.message(F.text.regexp(r"(?i)^баланс(\s|$)"))
async def cmd_balance(message: Message):
    if not await _economy_on(message):
        return
    args = message.text.split()[1:]
    target_id, err = await resolve_target(message, args, db)
    if err:
        if args or message.reply_to_message:
            await message.reply(err)
            return
        target_id = message.from_user.id
    balance = await economy.get_balance(message.chat.id, target_id)
    if target_id == message.from_user.id:
        await message.answer(f"💰 Ваш баланс: <b>{balance}</b> чапов.")
    else:
        user = await db.get_user(target_id)
        await message.answer(
            f"💰 Баланс {mention(target_id, user['first_name'] if user else None)}: "
            f"<b>{balance}</b> чапов."
        )


@router.message(F.text.regexp(r"(?i)^(бонус|ежедневная|ежедневный бонус)$"))
async def cmd_daily(message: Message):
    if not await _economy_on(message):
        return
    reward, wait = await economy.claim_daily(message.chat.id, message.from_user.id)
    if reward is not None:
        balance = await economy.get_balance(message.chat.id, message.from_user.id)
        await message.answer(
            f"🎁 Вы получили ежедневный бонус: <b>{reward}</b> чапов!\n"
            f"💰 Баланс: {balance} чапов."
        )
    else:
        await message.answer(
            f"⏳ Бонус уже получен. Следующий через {format_duration(wait)}."
        )


@router.message(F.text.regexp(r"(?i)^(перевести|перевод)\s+"))
async def cmd_transfer(message: Message):
    if not await _economy_on(message):
        return
    parts = message.text.split()[1:]
    if not parts:
        await message.reply("Формат: перевести @user 100 (или ответом на сообщение: перевести 100)")
        return
    # сумма — последний числовой аргумент
    amount = None
    target_args = parts
    if parts[-1].isascii() and parts[-1].isdigit():
        amount = int(parts[-1])
        target_args = parts[:-1]
    target_id, err = await resolve_target(message, target_args, db)
    if err:
        await message.reply(err)
        return
    if amount is None:
        await message.reply("Укажите сумму: перевести @user 100")
        return
    ok, error = await economy.transfer(
        message.chat.id, message.from_user.id, target_id, amount
    )
    if not ok:
        await message.reply(f"❌ {error}")
        return
    user = await db.get_user(target_id)
    await message.answer(
        f"✅ Переведено <b>{amount}</b> чапов → "
        f"{mention(target_id, user['first_name'] if user else None)}."
    )


@router.message(F.text.regexp(r"(?i)^топ(\s|$)"))
async def cmd_top(message: Message):
    if not await _economy_on(message):
        return
    rows = await economy.top_richest(message.chat.id, 10)
    if not rows:
        await message.answer("Пока никто не участвует в экономике.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows):
        place = medals[i] if i < 3 else f"{i + 1}."
        name = r["first_name"] or r["username"] or f"ID {r['user_id']}"
        lines.append(f"{place} {mention(r['user_id'], name)} — {r['balance']} чапов")
    await message.answer("🏆 <b>Топ богачей:</b>\n" + "\n".join(lines))


@router.message(F.text.regexp(r"(?i)^магазин$"))
async def cmd_shop(message: Message):
    if not await _economy_on(message):
        return
    items = await economy.list_shop(message.chat.id)
    if not items:
        await message.answer("Магазин пуст.")
        return
    lines = [f"• <b>{escape(i['name'], quote=True)}</b> — {i['price']} чапов. {escape((i['description'] or '')[:80], quote=True)}" for i in items]
    await message.answer(
        "🛒 <b>Магазин:</b>\n" + "\n".join(lines) + "\n\nКупить: купить &lt;название&gt;"
    )


@router.message(F.text.regexp(r"(?i)^купить\s+(.+)$"))
async def cmd_buy(message: Message):
    if not await _economy_on(message):
        return
    item = message.text.split(maxsplit=1)[1].strip()
    ok, text = await economy.buy_item(message.chat.id, message.from_user.id, item)
    await message.answer(("✅ " if ok else "❌ ") + escape(text, quote=True))


@router.message(F.text.regexp(r"(?i)^инвентарь(\s|$)"))
async def cmd_inventory(message: Message):
    if not await _economy_on(message):
        return
    args = message.text.split()[1:]
    target_id, err = await resolve_target(message, args, db)
    if err:
        if args or message.reply_to_message:
            await message.reply(err)
            return
        target_id = message.from_user.id
    rows = await economy.get_inventory(message.chat.id, target_id)
    if not rows:
        await message.answer("🎒 Инвентарь пуст.")
        return
    lines = [f"• {escape(r['item'], quote=True)} ×{r['qty']}" for r in rows]
    await message.answer("🎒 <b>Инвентарь:</b>\n" + "\n".join(lines))


@router.message(F.text.regexp(r"(?i)^задания$"))
async def cmd_daily_tasks(message: Message):
    if not await _economy_on(message):
        return
    tasks = await economy.get_daily_tasks(message.chat.id, message.from_user.id)
    lines = ["📋 <b>Ежедневные задания (обновляются по UTC):</b>"]
    for task in tasks:
        if task["claimed"]:
            status = "награда получена"
        elif task["progress"] >= task["goal"]:
            status = "готово — можно забрать"
        else:
            status = f"{task['progress']}/{task['goal']}"
        lines.append(f"• {escape(task['title'], quote=True)} — {status} (+{task['reward']} чапов)")
    lines.append("Забрать награды: задания забрать")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)^задания забрать$"))
async def cmd_claim_tasks(message: Message):
    if not await _economy_on(message):
        return
    amount, completed = await economy.claim_daily_tasks(message.chat.id, message.from_user.id)
    if not amount:
        await message.reply("Пока нет выполненных заданий с неполученной наградой.")
        return
    balance = await economy.get_balance(message.chat.id, message.from_user.id)
    await message.answer(f"🎉 Получено {amount} чапов за {len(completed)} заданий!\nБаланс: {balance} чапов.")


@router.message(F.text.regexp(r"(?i)^достижения$"))
async def cmd_achievements(message: Message):
    if not await _economy_on(message):
        return
    uid, chat_id = message.from_user.id, message.chat.id
    user = await db.get_user(uid)
    if not user:
        await db.upsert_user(uid, message.from_user.username, message.from_user.first_name)
        user = await db.get_user(uid)
    _ = await economy.get_balance(chat_id, uid)
    conn = await db.get_db()
    cur = await conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE chat_id=? AND from_id=? AND kind='transfer'", (chat_id, uid))
    transfers = (await cur.fetchone())["n"]
    cur = await conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE chat_id=? AND from_id=? AND kind='purchase'", (chat_id, uid))
    purchases = (await cur.fetchone())["n"]
    cur = await conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS n FROM transactions WHERE chat_id=? AND to_id=? "
        "AND kind IN ('daily','transfer','admin_give','task_reward')",
        (chat_id, uid),
    )
    total_received = int((await cur.fetchone())["n"] or 0)
    cur = await conn.execute("SELECT 1 FROM clan_members cm JOIN clans c ON c.id=cm.clan_id WHERE c.chat_id=? AND cm.user_id=? LIMIT 1", (chat_id, uid))
    in_clan = bool(await cur.fetchone())
    cur = await conn.execute("SELECT 1 FROM marriages WHERE chat_id=? AND (user1=? OR user2=?) LIMIT 1", (chat_id, uid, uid))
    married = bool(await cur.fetchone())
    if not married:
        cur = await conn.execute("SELECT 1 FROM marriage_history WHERE chat_id=? AND (user1=? OR user2=?) LIMIT 1", (chat_id, uid, uid))
        married = bool(await cur.fetchone())
    checks = [
        ("Первый шаг", int(user["messages_count"]) >= 1, "написать сообщение"),
        ("Общительный", int(user["messages_count"]) >= 100, "100 сообщений"),
        ("Легенда чата", int(user["messages_count"]) >= 1000, "1000 сообщений"),
        ("Уровень 5", int(user["level"]) >= 5, "достичь 5 уровня"),
        ("Состоятельный", total_received >= 5000, "получить суммарно 5000 чапов"),
        ("Торговец", transfers >= 10, "совершить 10 переводов"),
        ("Первая покупка", purchases >= 1, "купить предмет"),
        ("Семьянин", married, "заключить брак по взаимному согласию"),
        ("Клановый", in_clan, "вступить в клан"),
    ]
    unlocked = sum(1 for _, ok, _ in checks if ok)
    lines = [f"🏅 <b>Достижения: {unlocked}/{len(checks)}</b>"]
    for title, ok, requirement in checks:
        lines.append(f"{'🏆' if ok else '🔒'} {escape(title, quote=True)} — {'получено' if ok else requirement}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(F.text.regexp(r"(?i)^подарить\s+"))
async def cmd_gift_item(message: Message):
    if not await _economy_on(message):
        return
    if not message.from_user:
        return
    parts = message.text.split()[1:]
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        item_name = " ".join(parts)
    else:
        if len(parts) < 2:
            await message.reply("Формат: подарить @user предмет (или ответом на сообщение: подарить предмет)")
            return
        target_id, err = await resolve_target(message, parts[:1], db)
        if err:
            await message.reply(err)
            return
        item_name = " ".join(parts[1:])
    ok, result = await economy.gift_item(message.chat.id, message.from_user.id, target_id, item_name)
    if not ok:
        await message.reply("❌ " + escape(result, quote=True))
        return
    await db.log_action(message.chat.id, message.from_user.id, "item_gift", target_id, item_name.lower())
    recipient = await db.get_user(target_id)
    await message.answer(
        f"🎁 {mention(message.from_user.id, message.from_user.first_name)} подарил предмет "
        f"«{escape(item_name, quote=True)}» пользователю {mention(target_id, recipient['first_name'] if recipient else None)}.",
        parse_mode="HTML",
    )


@router.message(F.text.regexp(r"(?i)^история$"))
async def cmd_history(message: Message):
    if not await _economy_on(message):
        return
    uid = message.from_user.id
    rows = await economy.get_history(message.chat.id, uid, 10)
    if not rows:
        await message.answer("Операций пока не было.")
        return
    lines = [economy.describe_tx(r, uid) for r in rows]
    await message.answer("📜 <b>Последние операции:</b>\n" + "\n".join(lines))


# ---------- административные команды ----------

async def _admin_amount(message: Message, verb: str):
    """Общая логика для «выдать»/«забрать». Возвращает (target_id, amount) или None."""
    role = await effective_role(message.bot, message.chat.id, message.from_user.id)
    if not has_at_least(role, "admin"):
        await message.reply("⛔ Эта команда доступна администраторам и выше.")
        return None
    parts = message.text.split()[1:]
    amount = None
    if parts and parts[-1].isascii() and parts[-1].lstrip("-").isdigit():
        amount = int(parts[-1])
        parts = parts[:-1]
    target_id, err = await resolve_target(message, parts, db)
    if err:
        await message.reply(err)
        return None
    if amount is None or amount <= 0:
        await message.reply(f"Формат: {verb} @user 100 — сумма должна быть положительной.")
        return None
    return target_id, amount


@router.message(F.text.regexp(r"(?i)^выдать\s+"))
async def cmd_give(message: Message):
    if not await _economy_on(message):
        return
    res = await _admin_amount(message, "выдать")
    if not res:
        return
    target_id, amount = res
    ok, text = await economy.admin_change_balance(
        message.chat.id, message.from_user.id, target_id, amount
    )
    if ok:
        await db.log_action(message.chat.id, message.from_user.id, "give", target_id, str(amount))
    await message.answer(("✅ " if ok else "❌ ") + escape(text, quote=True))


@router.message(F.text.regexp(r"(?i)^забрать\s+"))
async def cmd_take(message: Message):
    if not await _economy_on(message):
        return
    res = await _admin_amount(message, "забрать")
    if not res:
        return
    target_id, amount = res
    ok, text = await economy.admin_change_balance(
        message.chat.id, message.from_user.id, target_id, -amount
    )
    if ok:
        await db.log_action(message.chat.id, message.from_user.id, "take", target_id, str(amount))
    await message.answer(("✅ " if ok else "❌ ") + escape(text, quote=True))


# ---------- управление магазином (админы) ----------

@router.message(F.text.regexp(r"(?i)^товар добавить\s+"))
async def cmd_item_add(message: Message):
    role = await effective_role(message.bot, message.chat.id, message.from_user.id)
    if not has_at_least(role, "admin"):
        await message.reply("⛔ Управлять магазином могут администраторы и выше.")
        return
    # Цена — первое отдельное целое число после названия. Это позволяет
    # использовать названия из нескольких слов и необязательное описание.
    tokens = message.text.split()
    if len(tokens) < 4:
        await message.reply("Формат: товар добавить &lt;название&gt; &lt;цена&gt; [описание]")
        return
    price_index = next(
        (i for i in range(2, len(tokens)) if tokens[i].isascii() and tokens[i].isdigit()),
        None,
    )
    if price_index is None or price_index == 2:
        await message.reply("Укажите название и цену. Пример: товар добавить золотая корона 500 редкий предмет")
        return
    name = " ".join(tokens[2:price_index])
    try:
        price = int(tokens[price_index])
    except (ValueError, OverflowError):
        await message.reply("Цена указана некорректно.")
        return
    desc = " ".join(tokens[price_index + 1:])
    ok, text = await economy.add_shop_item(message.chat.id, name, price, desc)
    await message.answer(("✅ " if ok else "❌ ") + escape(text, quote=True))


@router.message(F.text.regexp(r"(?i)^товар удалить\s+"))
async def cmd_item_remove(message: Message):
    if not message.from_user:
        await message.reply("Управлять магазином можно только обычным аккаунтом.")
        return
    role = await effective_role(message.bot, message.chat.id, message.from_user.id)
    if not has_at_least(role, "admin"):
        await message.reply("⛔ Управлять магазином могут администраторы и выше.")
        return
    parts = message.text.split(maxsplit=2)
    name = parts[2].strip() if len(parts) > 2 else ""
    if not name:
        await message.reply("Укажите название товара.")
        return
    if await economy.remove_shop_item(message.chat.id, name):
        await message.answer(f"🗑 Товар «{escape(name.lower(), quote=True)}» удалён из магазина.")
    else:
        await message.reply(f"Товара «{escape(name, quote=True)}» нет в магазине.")
