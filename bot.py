"""Точка входа Telegram-бота «Напоминалка».

Запуск одной командой:
    TELEGRAM_BOT_TOKEN=<токен> python bot.py

Помимо long polling поднимается маленький HTTP-сервер с endpoint /health —
он нужен для keep-alive на бесплатных хостингах (Render и т.п. засыпают
без входящих запросов; внешний монитор пингует /health раз в несколько минут).
"""
import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import db
import storage
from handlers import router
from scheduler import scheduler_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("reminder-bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "8080"))


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "reminder-bot"})


async def start_http() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("HTTP health-сервер слушает порт %s", PORT)
    return runner


async def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "Не задана переменная окружения TELEGRAM_BOT_TOKEN!\n"
            "Задай её так: TELEGRAM_BOT_TOKEN=<токен> python bot.py"
        )

    await asyncio.to_thread(storage.init_remote_db)
    await db.init_db()
    bot = Bot(token=TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    try:
        http_runner = await start_http()
    except OSError as exc:
        log.warning("HTTP health-сервер не запущен (%s) — бот работает без него",
                    exc)
        http_runner = None

    scheduler = asyncio.create_task(scheduler_loop(bot))

    log.info("Бот запущен, жду сообщения…")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.cancel()
        try:
            await asyncio.wait_for(asyncio.to_thread(storage.maybe_flush, True),
                                   timeout=15)
        except Exception:
            pass
        await bot.session.close()
        if http_runner is not None:
            await http_runner.cleanup()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
