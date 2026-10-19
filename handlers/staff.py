"""Секретные команды владельца и штата: +агент, -агент, +админ, -админ, !штат,
!бан, !разбан, кнопка снятия спам-блока, !включить/!выключить, !функции, !журнал штата."""
import re
from datetime import datetime
from html import escape

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import staff_core as sc

router = Router()

_ID_RE = re.compile(r"-?\d{1,15}")


def _fmt_time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "—"


def _target(message: Message, arg: str) -> int | None:
    """ID берётся из аргумента, иначе — из сообщения, на которое ответили."""
    m = _ID_RE.search(arg or "")
    if m:
        return int(m.group())
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    return None


def _unblock_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🔓 Разблокировать спам-блок", callback_data=f"sb:unblock:{user_id}")]])


@router.message(F.text.regexp(r"^[+-](агент|админ)(\s|$)", flags=re.I))
async def staff_assign(message: Message):
    uid = message.from_user.id
    if not await sc.get_staff_role(uid):
        return  # секретная команда: посторонним молчим
    text = message.text.strip()
    sign, word = text[0], text[1:].split()[0].casefold()
    role = sc.ROLE_AGENT if word == "агент" else sc.ROLE_ADMIN
    target = _target(message, text[1 + len(word):])
    if not target:
        await message.answer("Укажите Telegram ID или ответьте на сообщение пользователя.")
        return
    fn = sc.assign_role if sign == "+" else sc.remove_role
    _ok, msg = await fn(uid, target, role)
    await message.answer(msg, parse_mode="HTML")


@router.message(F.text.casefold() == "!штат")
async def staff_list(message: Message):
    if not await sc.has_perm(message.from_user.id, "staff.view"):
        return
    lines = ["👥 <b>Штат бота</b>", f"{sc.ROLE_TITLES['owner']}: <code>{sc.config.OWNER_TELEGRAM_ID}</code>"]
    staff = await sc.list_staff()
    for row in staff:
        lines.append(f"{sc.ROLE_TITLES.get(row['role'], row['role'])}: <code>{row['user_id']}</code> "
                     f"(назначил {row['granted_by']}, {_fmt_time(row['granted_at'])})")
    if not staff:
        lines.append("Других сотрудников нет.")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold() == "!журнал штата")
async def staff_journal(message: Message):
    if not await sc.has_perm(message.from_user.id, "log.view"):
        return
    rows = await sc.get_staff_log(25)
    lines = ["📜 <b>Журнал действий штата</b>"]
    for r in rows:
        actor = "авто" if not r["actor_id"] else r["actor_id"]
        lines.append(f"{_fmt_time(r['created_at'])} · {actor} · {escape(r['action'])} · "
                     f"{r['target_id'] or '—'} · {escape(r['result'] or '')}")
    if not rows:
        lines.append("Пока пусто.")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(F.text.regexp(r"^!бан(\s|$)", flags=re.I))
async def spamblock_info(message: Message):
    uid = message.from_user.id
    if not await sc.has_perm(uid, "spamblock.view"):
        return
    target = _target(message, message.text[4:])
    if not target:
        await message.answer("Укажите Telegram ID или ответьте на сообщение пользователя.")
        return
    rec = await sc.spamblock_get(target)
    if not rec:
        await message.answer(f"✅ Пользователь <code>{target}</code> не находится в спам-блоке.", parse_mode="HTML")
        return
    who = "автоматическая система" if rec["source"] == "auto" else f"сотрудник <code>{rec['added_by']}</code>"
    text = (f"⛔ <b>Спам-блок</b>: <code>{target}</code>\n"
            f"Причина: {escape(rec['reason'] or '—')}\n"
            f"Добавлен: {_fmt_time(rec['created_at'])}\n"
            f"Чат: {rec['chat_id'] or 'неизвестно'}" + (f", сообщение {rec['message_id']}" if rec['message_id'] else "") + "\n"
            f"Кем: {who}\n"
            f"Сведения: {escape(rec['details'] or '—')}")
    kb = _unblock_kb(target) if await sc.has_perm(uid, "spamblock.remove") else None
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(F.text.regexp(r"^!разбан(\s|$)", flags=re.I))
async def spamblock_unblock_cmd(message: Message):
    uid = message.from_user.id
    if not await sc.has_perm(uid, "spamblock.remove"):
        return
    target = _target(message, message.text[7:])
    if not target:
        await message.answer("Укажите Telegram ID или ответьте на сообщение пользователя.")
        return
    _ok, msg = await sc.spamblock_remove(uid, target)
    await message.answer(msg, parse_mode="HTML")


@router.message(F.text.regexp(r"^!спамблок(\s|$)", flags=re.I))
async def spamblock_add_cmd(message: Message):
    uid = message.from_user.id
    if not await sc.has_perm(uid, "spamblock.add"):
        return
    rest = message.text[9:].strip()
    target = _target(message, rest)
    if not target:
        await message.answer("Использование: !спамблок ID причина (или ответом на сообщение).")
        return
    reason = _ID_RE.sub("", rest, count=1).strip() if _ID_RE.match(rest) else rest
    reply = message.reply_to_message
    details = (reply.text or reply.caption or "")[:300] if reply else ""
    _ok, msg = await sc.spamblock_add(target, reason or "без причины", "staff", uid,
                                      message.chat.id, reply.message_id if reply else None, details)
    await message.answer(msg, parse_mode="HTML", reply_markup=_unblock_kb(target) if _ok else None)


@router.callback_query(F.data.startswith("sb:unblock:"))
async def spamblock_unblock_button(call: CallbackQuery):
    if not await sc.has_perm(call.from_user.id, "spamblock.remove"):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    try:
        target = int(call.data.split(":")[2])
    except (IndexError, ValueError):
        await call.answer("Некорректная кнопка.", show_alert=True)
        return
    ok, msg = await sc.spamblock_remove(call.from_user.id, target)
    await call.answer("Готово" if ok else "Не выполнено")
    if call.message:
        await call.message.answer(msg, parse_mode="HTML")


@router.message(F.text.regexp(r"^!(включить|выключить)\s+\S+", flags=re.I))
async def module_toggle(message: Message):
    uid = message.from_user.id
    if not sc.is_owner(uid):
        return
    cmd, word = message.text.split(maxsplit=1)
    key = sc.resolve_module(word)
    if not key:
        await message.answer("Неизвестный модуль. Список: !функции")
        return
    _ok, msg = await sc.set_module(uid, key, cmd.casefold() == "!включить")
    await message.answer(msg)


@router.message(F.text.casefold() == "!функции")
async def modules_list(message: Message):
    if not sc.is_owner(message.from_user.id):
        return
    state = await sc.modules_state()
    lines = ["🧩 <b>Глобальные модули</b>"]
    for key, enabled in state.items():
        lines.append(f"{'🟢' if enabled else '🔴'} {sc.MODULES[key][0]}")
    lines.append("\n!включить &lt;модуль&gt; / !выключить &lt;модуль&gt;")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(F.text.casefold().in_({"!обжаловать", "/appeal"}))
async def appeal(message: Message):
    rec = await sc.spamblock_get(message.from_user.id)
    if not rec:
        await message.answer("Вы не находитесь в спам-блоке.")
        return
    await sc.staff_log(message.from_user.id, "appeal", message.from_user.id, "запрос на снятие", message.chat.id)
    await message.answer("📨 Обжалование отправлено штату бота. Ожидайте решения.")
    owner = sc.config.OWNER_TELEGRAM_ID
    if owner > 0:
        try:
            await message.bot.send_message(
                owner, f"📨 Обжалование спам-блока от <code>{message.from_user.id}</code>\n"
                       f"Причина блока: {escape(rec['reason'] or '—')}",
                parse_mode="HTML", reply_markup=_unblock_kb(message.from_user.id))
        except Exception:
            pass
