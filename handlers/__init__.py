"""Роутеры обработчиков. Каждый модуль — отдельная функция бота.
Порядок важен: basic содержит общий счётчик сообщений, он подключается последним."""
from aiogram import Router

from . import staff
from .gates import SpamBlockGate, ModuleGate
from . import moderation, notes, antispam, autoreplies, cases, economy, premium, social, clans, admin_panel, settings, games, basic

router = Router()
router.message.outer_middleware(SpamBlockGate())
router.callback_query.outer_middleware(SpamBlockGate())
router.include_router(staff.router)

_GATED = {"moderation": moderation, "notes": notes, "antispam": antispam, "autoreplies": autoreplies,
          "cases": cases, "economy": economy, "premium": premium, "social": social, "clans": clans,
          "games": games}
for _key, _mod in _GATED.items():
    _mod.router.message.outer_middleware(ModuleGate(_key))
    _mod.router.callback_query.outer_middleware(ModuleGate(_key))
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
router.include_router(basic.router)
