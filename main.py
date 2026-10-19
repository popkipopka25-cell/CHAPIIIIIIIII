
"""Точка входа: запуск Telegram-бота и веб-панели."""
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramUnauthorizedError, TelegramAPIError

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
_STATE = {"polling": False, "telegram_authorized": False}


def build_web_app() -> web.Application:
    """Создаёт HTTP-сервер для Render и подключает веб-панель."""

    async def root(request: web.Request) -> web.StreamResponse:
        """Главная страница веб-панели."""
        panel_path = Path(__file__).resolve().parent / "static" / "panel.html"

        if not panel_path.is_file():
            return web.Response(
                text="Файл веб-панели static/panel.html не найден.",
                status=404,
                content_type="text/plain",
            )

        return web.FileResponse(
            panel_path,
            headers={"Cache-Control": "no-store"},
        )

    async def health(request: web.Request) -> web.Response:
        """Проверка доступности HTTP-сервера."""
        return web.json_response({
            "ok": True,
            "polling": _STATE["polling"],
            "telegram_authorized": _STATE["telegram_authorized"],
        })

    async def status(request: web.Request) -> web.Response:
        """Статус сервиса."""
        if _STATE["polling"]:
            current_status = "running"
        elif _STATE["telegram_authorized"]:
            current_status = "starting"
        else:
            current_status = "starting"

        return web.json_response({
            "service": BOT_NAME,
            "status": current_status,
            "polling": _STATE["polling"],
            "telegram_authorized": _STATE["telegram_authorized"],
            "uptime_seconds": int(time.time() - _STARTED_AT),
        })

    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)

    # Подключаем существующие маршруты панели.
    from web_panel import add_routes
    add_routes(app)

    return app


async def start_web_server() -> web.AppRunner:
    """Запускает HTTP-сервер на порту Render."""
    raw_port = os.getenv("PORT", "10000")

    try:
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise RuntimeError("Переменная PORT должна содержать корректный порт.")

    runner = web.AppRunner(build_web_app())

    try:
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
    except Exception:
        await runner.cleanup()
        raise

    logger.info("HTTP-сервер слушает 0.0.0.0:%s (/ и /health)", port)
    return runner


async def _maintenance_loop() -> None:
    """Периодически обновляет состояние временных наказаний."""
    while True:
        try:
            expired = await db.expire_punishments()

            if expired:
                logger.info(
                    "Помечено истёкших наказаний: %s",
                    expired,
                )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("Ошибка фоновой очистки наказаний")

        await asyncio.sleep(60)


async def _check_telegram(bot: Bot) -> bool:
    """Проверяет токен и сообщает username без раскрытия секрета."""
    try:
        me = await bot.get_me()

        logger.info(
            "Авторизация Telegram успешна: bot_id=%s, username=@%s",
            me.id,
            me.username or "не указан",
        )

        _STATE["telegram_authorized"] = True
        return True

    except TelegramUnauthorizedError:
        logger.error(
            "Telegram отклонил токен (Unauthorized). "
            "Проверь переменную TELEGRAM_BOT_TOKEN в Render. "
            "Сам токен в логах не выводится."
        )
        return False

    except TelegramAPIError:
        logger.exception(
            "Telegram API вернул ошибку при проверке токена."
        )
        return False

    except Exception:
        logger.exception(
            "Не удалось проверить подключение к Telegram."
        )
        return False


async def main() -> None:
    """Основной запуск приложения."""
    problems = check_config()

    for problem in problems:
        logger.error("%s", problem)

    if problems:
        logger.error(
            "Запуск бота остановлен. Исправь конфигурацию в Render."
        )
        return

    if not BOT_TOKEN:
        logger.error(
            "Токен отсутствует. "
            "Задай TELEGRAM_BOT_TOKEN в переменных окружения."
        )
        return

    web_runner = None
    bot = None
    maintenance_task = None

    try:
        # Сначала запускаем веб-сервер, чтобы Render мог проверять сервис.
        web_runner = await start_web_server()

        # Подготавливаем базу данных.
        await db.init_db()
        logger.info("База данных готова.")

        # Создаём экземпляр Telegram-бота.
        bot = Bot(
            token=BOT_TOKEN,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
            ),
        )

        # Проверяем авторизацию до удаления webhook и polling.
        authorized = await _check_telegram(bot)

        if not authorized:
            logger.error(
                "Бот не запущен: проверка авторизации Telegram не пройдена. "
                "Веб-сервер продолжит отвечать, но команды бота работать не будут."
            )

            # Не завершаем процесс немедленно: HTTP-сервис остаётся доступен.
            # Для повторной попытки потребуется перезапуск сервиса.
            while True:
                await asyncio.sleep(300)

        # Настраиваем диспетчер команд.
        dp = Dispatcher()
        dp.include_router(router)

        # Удаляем webhook перед запуском polling.
        await bot.delete_webhook(drop_pending_updates=True)

        # Запускаем периодическую очистку наказаний.
        maintenance_task = asyncio.create_task(
            _maintenance_loop(),
            name="punishment-maintenance",
        )

        logger.info("%s запущен. Ожидаю сообщения...", BOT_NAME)
        _STATE["polling"] = True

        await dp.start_polling(
            bot,
            handle_signals=False,
            close_bot_session=False,
        )

    except asyncio.CancelledError:
        logger.info("Получен сигнал остановки.")
        raise

    except TelegramUnauthorizedError:
        _STATE["polling"] = False
        _STATE["telegram_authorized"] = False
        logger.exception(
            "Telegram отклонил авторизацию во время запуска или работы."
        )

    except TelegramAPIError:
        _STATE["polling"] = False
        logger.exception("Ошибка взаимодействия с Telegram API.")

    except Exception:
        _STATE["polling"] = False
        logger.exception("Критическая ошибка при запуске или работе бота.")

    finally:
        _STATE["polling"] = False

        if maintenance_task is not None:
            maintenance_task.cancel()
            try:
                await maintenance_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Ошибка остановки фоновой задачи.")

        if bot is not None:
            try:
                await bot.session.close()
            except Exception:
                logger.exception("Ошибка закрытия сессии Telegram.")

        if web_runner is not None:
            try:
                await web_runner.cleanup()
            except Exception:
                logger.exception("Ошибка остановки HTTP-сервера.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
    except SystemExit:
        raise
