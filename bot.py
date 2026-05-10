import asyncio
import logging
import os
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

# Импорты твоих модулей (убедись, что папки handlers и файлы в них есть!)
from database import Database
from handlers.user_profile import profile_router
from handlers.stake_menu import stake_router
from handlers.cards_menu import cards_router
from handlers.admin_mifl import admin_mifl_router

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 🌐 Веб-сервер для Render (чтобы не было ошибки "No open ports")
async def handle_ping(request):
    return web.Response(text="MIFL Bot is active!")

async def main():
    # 1. Берем данные из настроек Render (Environment Variables)
    TOKEN = os.getenv("BOT_TOKEN")
    DATABASE_URL = os.getenv("DATABASE_URL")
    ADMIN_IDS = os.getenv("ADMIN_IDS")

    if not TOKEN:
        logger.error("❌ ОШИБКА: Переменная BOT_TOKEN не найдена в настройках Render!")
        return
    if not DATABASE_URL:
        logger.error("❌ ОШИБКА: Переменная DATABASE_URL не найдена в настройках Render!")
        return

    # 2. Инициализация бота
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
    dp = Dispatcher()

    # 3. Подключение к базе данных Neon
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, ssl='require')
        db = Database(pool)
        await db.create_tables()
        logger.info("✅ База данных подключена успешно.")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к БД: {e}")
        return

    # Передаем БД в хэндлеры
    dp["db"] = db

    # 4. Регистрация всех роутеров
    dp.include_router(profile_router)
    dp.include_router(stake_router)
    dp.include_router(cards_router)
    dp.include_router(admin_mifl_router)

    # 5. Подготовка веб-сервера
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Порт Render выдает сам
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    
    # Запускаем всё вместе
    await site.start()
    logger.info(f"🌐 Порт {port} открыт для Render.")
    
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("🚀 Бот запущен и слушает сообщения!")
        await dp.start_polling(bot)
    finally:
        await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
