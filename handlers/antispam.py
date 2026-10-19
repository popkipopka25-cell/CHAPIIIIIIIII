"""Антиспам: флуд, повторы, ссылки. Действие настраивается: warn/delete/mute/case."""
import time
import re
from collections import defaultdict, deque

from aiogram import Router, F
from aiogram.types import Message
from aiogram.exceptions import TelegramAPIError
from aiogram.dispatcher.event.bases import UNHANDLED

import database as db
import staff_core
from utils import format_duration, mention, has_at_least
from handlers.moderation import effective_role, do_mute, _add_punishment, count_warns, MAX_WARNS, AUTO_MUTE_SEC

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

# Память о последних сообщениях: (chat_id, user_id) -> deque[(timestamp, текст)]
_history: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=21))
_LAST_PRUNE = 0.0


def _prune_history(now: float, window: int) -> None:
    """Периодически удаляет устаревшие ключи, чтобы память не росла бесконечно."""
    global _LAST_PRUNE
    if now - _LAST_PRUNE < max(30, min(window, 300)):
        return
    cutoff = now - max(60, window * 3)
    stale = [key for key, hist in _history.items() if not hist or hist[-1][0] < cutoff]
    for key in stale:
        _history.pop(key, None)
    # Защита от длительного шквала уникальных аккаунтов: сохраняем только
    # самые недавно активные ключи, когда словарь превысил безопасный предел.
    if len(_history) > 20_000:
        keep = sorted(
            _history.items(),
            key=lambda item: item[1][-1][0] if item[1] else 0,
            reverse=True,
        )[:15_000]
        _history.clear()
        _history.update(keep)
    _LAST_PRUNE = now

LINK_RE = re.compile(r"(?i)(?:https?://|www\.|t\.me/|telegram\.me/|discord\.gg/|(?:[a-z0-9-]+\.)+(?:com|net|org|ru|ua|io|me|gg|xyz|site|online)\b)")


def _bounded_int(value, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(low, min(high, parsed))


def _check_spam(key, text: str, settings: dict) -> str | None:
    """Возвращает причину нарушения или None."""
    now = time.time()
    configured_window = _bounded_int(settings.get("antispam_window_sec", 10), 10, 1, 3600)
    _prune_history(now, configured_window)
    hist = _history[key]
    hist.append((now, text))

    recent = [h for h in hist if now - h[0] <= configured_window]

    max_messages = _bounded_int(settings.get("antispam_max_messages", 5), 5, 1, 20)
    max_duplicates = _bounded_int(settings.get("antispam_max_duplicates", 3), 3, 2, 20)
    max_links = _bounded_int(settings.get("antispam_max_links", 3), 3, 0, 20)
    if len(recent) > max_messages:
        return "флуд (слишком много сообщений)"

    dupes = [h for h in recent if h[1] == text]
    if len(dupes) >= max_duplicates:
        return "повторяющиеся сообщения"

    links = sum(1 for h in recent if LINK_RE.search(h[1]))
    if links > max_links:
        return "спам ссылками"

    return None


def _message_content(message: Message) -> tuple[str, str] | None:
    """Возвращает fingerprint и читаемую подпись для текста/медиа."""
    if message.text is not None:
        return message.text, message.text
    if message.caption is not None:
        return message.caption, message.caption
    media_fields = (
        ("sticker", "стикер"), ("animation", "GIF"), ("photo", "фото"),
        ("video", "видео"), ("video_note", "видеосообщение"),
        ("voice", "голосовое сообщение"), ("audio", "аудио"),
        ("document", "документ"),
    )
    for attribute, label in media_fields:
        media = getattr(message, attribute, None)
        if media:
            if attribute == "photo":
                media = media[-1] if isinstance(media, list) else media
            unique_id = getattr(media, "file_unique_id", None) or label
            return f"{attribute}:{unique_id}", f"[{label}]"
    if message.dice:
        return f"dice:{message.dice.emoji}", f"[кубик {message.dice.emoji}]"
    return None


async def antispam_check(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return UNHANDLED
    settings = await db.get_settings(message.chat.id)
    if not settings.get("antispam", True):
        return UNHANDLED

    role = await effective_role(message.bot, message.chat.id, message.from_user.id)
    if role in settings.get("exempt_roles", []):
        return UNHANDLED
    # Не блокируем ручную передачу сообщения на проверку агентом/модератором.
    if (message.text or "").strip().casefold() in {"спам", "пожаловаться на спам"} and has_at_least(role, "agent"):
        return UNHANDLED

    message_content = _message_content(message)
    if not message_content:
        return UNHANDLED
    fingerprint, display_content = message_content
    reason = _check_spam((message.chat.id, message.from_user.id), fingerprint, settings)
    if not reason:
        # Важно: передаём обычное сообщение следующим роутерам (экономика, профиль и т. п.).
        return UNHANDLED

    action = settings.get("antispam_action", "warn")
    if await staff_core.register_violation(message.from_user.id, reason, message.chat.id, message.message_id):
        await message.answer("⛔ Нарушитель автоматически добавлен в глобальный спам-блок. Обжалование: !обжаловать")
    if action not in {"warn", "delete", "mute", "case"}:
        action = "warn"
    uid = message.from_user.id
    name = mention(uid, message.from_user.first_name)

    # сообщение-нарушение удаляем при любом действии
    try:
        await message.delete()
    except TelegramAPIError:
        # У бота может не быть права удаления; само нарушение всё равно обрабатываем.
        pass

    if action == "delete":
        await db.log_action(message.chat.id, message.bot.id, "antispam_delete", uid, reason)
        return

    if action == "warn":
        await _add_punishment(message.chat.id, uid, "warn", f"антиспам: {reason}", message.bot.id)
        count = await count_warns(message.chat.id, uid)
        text = f"⚠️ {name}, антиспам: {reason}. Варн ({count}/{MAX_WARNS})."
        if count >= MAX_WARNS:
            try:
                await do_mute(message.bot, message.chat.id, uid, AUTO_MUTE_SEC)
                conn = await db.get_db()
                await conn.execute(
                    "UPDATE punishments SET active=0 WHERE chat_id=? AND user_id=? AND type='warn' AND active=1",
                    (message.chat.id, uid),
                )
                await conn.commit()
                await _add_punishment(
                    message.chat.id, uid, "mute", "автоматический мут за лимит варнов",
                    message.bot.id, time.time() + AUTO_MUTE_SEC,
                )
                await db.log_action(
                    message.chat.id, message.bot.id, "antispam_auto_mute", uid,
                    f"{MAX_WARNS} варна; срок {AUTO_MUTE_SEC}с",
                )
                text += f"\n🔇 Лимит варнов — мут на {format_duration(AUTO_MUTE_SEC)}."
            except TelegramAPIError:
                text += "\n⚠️ Не удалось применить автомут: проверьте права бота."
        await message.answer(text, parse_mode="HTML")

    elif action == "mute":
        try:
            sec = int(settings.get("mute_duration_sec", 3600))
        except (TypeError, ValueError):
            sec = 3600
        sec = max(60, min(365 * 86400, sec))
        try:
            await do_mute(message.bot, message.chat.id, uid, sec)
            await _add_punishment(message.chat.id, uid, "mute", f"антиспам: {reason}",
                                  message.bot.id, time.time() + sec)
            await message.answer(f"🔇 {name} замучен на {format_duration(sec)}. Антиспам: {reason}.", parse_mode="HTML")
        except TelegramAPIError:
            await message.answer("⚠️ Не удалось применить мут. Проверьте права бота и повторите попытку.")
            return

    elif action == "case":
        conn = await db.get_db()
        await conn.execute(
            "INSERT INTO cases (chat_id, user_id, reason, message_text, created_at) VALUES (?,?,?,?,?)",
            (message.chat.id, uid, f"антиспам: {reason}", display_content[:500], time.time()),
        )
        await conn.commit()
        await message.answer(
            f"📁 Создано дело против {name}: {reason}. Модераторы решат, что делать.", parse_mode="HTML"
        )

    await db.log_action(message.chat.id, message.bot.id, f"antispam_{action}", uid, reason)


@router.message(F.text.regexp(r"(?i)^(спам|пожаловаться на спам)$"))
async def report_spam(message: Message):
    """Агент или модератор может отправить сообщение на ручную проверку."""
    if not message.from_user:
        return
    actor_role = await effective_role(message.bot, message.chat.id, message.from_user.id)
    if not has_at_least(actor_role, "agent"):
        await message.reply("⛔ Отправлять сообщения на проверку могут агенты и модераторы.")
        return
    source = message.reply_to_message
    if not source or not source.from_user:
        await message.reply("Ответьте командой «спам» на сообщение, которое нужно проверить.")
        return
    target = source.from_user
    if target.is_bot or target.id == message.from_user.id:
        await message.reply("Это сообщение нельзя отправить на проверку.")
        return
    content = (source.text or source.caption or "[сообщение без текста]")[:500]
    conn = await db.get_db()
    await conn.execute(
        "INSERT INTO cases (chat_id,user_id,reason,message_text,created_at) VALUES (?,?,?,?,?)",
        (message.chat.id, target.id, "ручная жалоба агента на спам", content, time.time()),
    )
    await conn.commit()
    await db.log_action(message.chat.id, message.from_user.id, "spam_report", target.id, "ручная жалоба агента")
    await message.answer(
        f"📁 Жалоба отправлена на проверку. Пользователь: {mention(target.id, target.first_name)}.",
        parse_mode="HTML",
    )


# Регистрируем общий фильтр ПОСЛЕДНИМ: aiogram не передаёт сообщение следующим
# обработчикам того же роутера, если подошедший обработчик вернул UNHANDLED.
# Иначе команда «спам» никогда не срабатывала.
router.message.register(antispam_check)
