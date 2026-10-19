"""Небольшие чат-игры без ставок и случайных денежных списаний."""
import secrets

from aiogram import Router, F
from aiogram.types import Message

import database as db

router = Router()
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


async def _games_enabled(message: Message) -> bool:
    return bool((await db.get_settings(message.chat.id)).get("games", True))


@router.message(F.text.regexp(r"(?i)^(орёл или решка|монетка)$"))
async def coin_flip(message: Message):
    if not await _games_enabled(message):
        await message.reply("🎮 Игры в этой группе отключены.")
        return
    result = secrets.choice(("Орёл", "Решка"))
    await message.answer(f"🪙 Выпало: <b>{result}</b>!")


@router.message(F.text.regexp(r"(?i)^(кости|бросить кости|кубик)$"))
async def roll_dice(message: Message):
    if not await _games_enabled(message):
        await message.reply("🎮 Игры в этой группе отключены.")
        return
    value = secrets.randbelow(6) + 1
    faces = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}
    player = message.from_user.first_name if message.from_user else "Участник"
    from html import escape
    await message.answer(f"🎲 {escape(player, quote=True)} бросает кубик: {faces[value]} <b>{value}</b>!")


@router.message(F.text.regexp(r"(?i)^случайное число(?:\s+(-?\d+)\s+(-?\d+))?$"))
async def random_number(message: Message):
    if not await _games_enabled(message):
        await message.reply("🎮 Игры в этой группе отключены.")
        return
    parts = (message.text or "").split()
    low, high = 1, 100
    if len(parts) == 4:
        try:
            low, high = int(parts[2]), int(parts[3])
        except ValueError:
            await message.reply("Формат: случайное число 1 100")
            return
    if low > high:
        await message.reply("Минимум не может быть больше максимума.")
        return
    if high - low > 1_000_000_000 or low < -1_000_000_000 or high > 1_000_000_000:
        await message.reply("Диапазон слишком большой. Используйте числа от −1 000 000 000 до 1 000 000 000.")
        return
    result = low + secrets.randbelow(high - low + 1)
    await message.answer(f"🎯 Случайное число: <b>{result}</b>")
