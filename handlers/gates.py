"""Мидлвари: глобальный спам-блок и глобальное отключение модулей."""
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import CallbackQuery, Message, TelegramObject

import staff_core as sc

# Команды, доступные даже в спам-блоке (обжалование).
APPEAL_COMMANDS = {"!обжаловать", "/appeal"}


class SpamBlockGate(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        user = getattr(event, "from_user", None)
        if user and await sc.is_spamblocked(user.id):
            text = (getattr(event, "text", None) or "").strip().casefold()
            if isinstance(event, Message) and text in APPEAL_COMMANDS:
                return await handler(event, data)
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ Вы в глобальном спам-блоке. Напишите !обжаловать", show_alert=True)
            return None  # бот игнорирует пользователя
        return await handler(event, data)


class ModuleGate(BaseMiddleware):
    def __init__(self, key: str):
        self.key = key

    async def __call__(self, handler, event, data):
        if not await sc.module_enabled(self.key):
            return UNHANDLED  # пропускаем модуль, остальные роутеры продолжают работу
        return await handler(event, data)
