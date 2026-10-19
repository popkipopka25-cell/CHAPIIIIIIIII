
"""Роутеры обработчиков. Каждый модуль — отдельная функция бота."""
from aiogram import Router

from . import staff
from .gates import SpamBlockGate, ModuleGate
from . import (
    moderation,
    notes,
    antispam,
    autoreplies,
    cases,
    economy,
    premium,
    social,
    clans,
    admin_panel,
    settings,
    games,
    basic,
    command_rights,
)

router = Router()

# Общие проверки безопасности.
router.message.outer_middleware(SpamBlockGate())
router.callback_query.outer_middleware(SpamBlockGate())

# Штат бота.
router.include_router(staff.router)

# Проверка минимального ранга команды.
router.message.outer_middleware(
    command_rights.CommandRankMiddleware()
)

# Модули, которые могут отключаться в настройках группы.
_GATED = {
    "moderation": moderation,
    "notes": notes,
    "antispam": antispam,
    "autoreplies": autoreplies,
    "cases": cases,
    "economy": economy,
    "premium": premium,
    "social": social,
    "clans": clans,
    "games": games,
}

for _key, _mod in _GATED.items():
    _mod.router.message.outer_middleware(ModuleGate(_key))
    _mod.router.callback_query.outer_middleware(ModuleGate(_key))

# Основные обработчики.
router.include_router(moderation.router)
router.include_router(notes.router)
router.include_router(antispam.router)
router.include_router(autoreplies.router)
router.include_router(cases.router)
router.include_router(economy.router)
router.include_router(premium.router)
router.include_router(social.router)
router.include_router(clans.router)
router.include_router(admin_panel.router)
router.include_router(settings.router)
router.include_router(games.router)

# Команда .дк.
router.include_router(command_rights.router)

# Общие команды и счётчик сообщений подключаются последними.
router.include_router(basic.router)
