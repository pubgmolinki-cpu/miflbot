import asyncio
import logging
import os
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

# Импортируем наши модули
from database import Database
from handlers.cards_menu import cards_router
from handlers.user_profile import profile_router
from handlers.admin_cards import admin_router

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 🌐 Веб-сервер для Render (чтобы сервис не "засыпал" и не выдавал ошибку портов)
async def handle_ping(request):
    return web.Response(text="MIFL CARDS Bot is active!")

async def main():
    # 1. Получаем токены из Environment Variables (настрой в панели Render!)
    TOKEN = os.getenv("BOT_TOKEN")
    DATABASE_URL = os.getenv("DATABASE_URL")

    if not TOKEN or not DATABASE_URL:
        logger.error("❌ ОШИБКА: BOT_TOKEN или DATABASE_URL не найдены в настройках!")
        return

    # 2. Инициализация бота и диспетчера
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
    dp = Dispatcher()

    # 3. Настройка базы данных
    try:
        # Обязательно ssl='require' для подключения к Neon
        pool = await asyncpg.create_pool(DATABASE_URL, ssl='require')
        db = Database(pool)
        await db.create_tables()
        logger.info("✅ База данных подключена, таблицы проверены.")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к БД: {e}")
        return

    # Прокидываем объект базы данных в контекст, чтобы он был доступен в хэндлерах как аргумент 'db'
    dp["db"] = db

    # 4. Регистрация роутеров (важен порядок: админские лучше ставить выше)
    dp.include_router(admin_router)
    dp.include_router(cards_router)
    dp.include_router(profile_router)

    # 5. Запуск веб-сервера для Render
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render выдает порт автоматически через переменную PORT
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    
    await site.start()
    logger.info(f"🌐 Мини-сервер запущен на порту {port}")

    # 6. Старт бота
    try:
        # Удаляем старые сообщения, которые пришли боту, пока он был выключен
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("🚀 MIFL CARDS запущен!")
        await dp.start_polling(bot)
    finally:
        await pool.close()
        logger.info("👋 Бот остановлен.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот выключен вручную.")
