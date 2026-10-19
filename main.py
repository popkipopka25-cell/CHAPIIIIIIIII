"""Точка входа: запуск Telegram-бота."""
import asyncio
import logging
import os
import sys
import time

from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import BOT_TOKEN, BOT_NAME, check_config
import database as db
from handlers import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("bot")

_STARTED_AT = time.time()
_STATE = {"polling": False}


def build_web_app() -> web.Application:
    """HTTP-сервер для Render Web Service: / и /health."""
    async def root(_request):
        # Основной URL сразу открывает веб-панель; /health оставлен для Render.
        from pathlib import Path
        return web.FileResponse(Path(__file__).resolve().parent / "static" / "panel.html",
                                headers={"Cache-Control": "no-store"})

    async def health(_request):
        return web.json_response({"ok": True, "polling": _STATE["polling"]})

    async def status(_request):
        return web.json_response({
            "service": BOT_NAME,
            "status": "running" if _STATE["polling"] else "starting",
            "uptime_seconds": int(time.time() - _STARTED_AT),
        })

    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    from web_panel import add_routes
    add_routes(app)
    return app


async def start_web_server() -> web.AppRunner:
    port = int(os.getenv("PORT", "10000") or 10000)
    runner = web.AppRunner(build_web_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("HTTP-сервер слушает 0.0.0.0:%s (/ и /health)", port)
    return runner


async def _maintenance_loop():
    """Периодически синхронизирует состояние временных наказаний в базе."""
    while True:
        try:
            expired = await db.expire_punishments()
            if expired:
                logger.info("Помечено истёкших наказаний: %s", expired)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка фоновой очистки наказаний")
        await asyncio.sleep(60)


async def main():
    problems = check_config()
    for problem in problems:
        logger.error(problem)
    # Не запускаем бота с неполной/небезопасной конфигурацией.
    if problems:
        logger.error("Запуск остановлен: исправьте конфигурацию в .env или Secrets хостинга.")
        sys.exit(1)
    if not BOT_TOKEN:  # дополнительная защита на случай изменения check_config()
        logger.error("Запуск невозможен без TELEGRAM_BOT_TOKEN. См. README.md.")
        sys.exit(1)

    web_runner = await start_web_server()
    await db.init_db()
    logger.info("База данных готова.")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    maintenance_task = asyncio.create_task(_maintenance_loop(), name="punishment-maintenance")
    logger.info("%s запущен. Ожидаю сообщения...", BOT_NAME)
    _STATE["polling"] = True
    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        maintenance_task.cancel()
        try:
            await maintenance_task
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await db.close_db()
            finally:
                try:
                    await bot.session.close()
                finally:
                    await web_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
