import asyncio
import logging
import os
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

# Импорты твоих модулей
from database import Database
from handlers.user_profile import profile_router
from handlers.stake_menu import stake_router
from handlers.cards_menu import cards_router
from handlers.admin_mifl import admin_mifl_router

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 🌐 Мини веб-сервер для Render (затычка для порта)
async def handle_ping(request):
    return web.Response(text="MIFL Bot is alive!")

async def main():
    # 1. Загрузка конфигов из Environment Variables
    TOKEN = os.getenv("BOT_TOKEN")
    DATABASE_URL = os.getenv("DATABASE_URL")

    if not TOKEN or not DATABASE_URL:
        logger.error("❌ Ошибка: BOT_TOKEN или DATABASE_URL не заданы в настройках Render!")
        return

    # 2. Инициализация бота
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
    dp = Dispatcher()

    # 3. Подключение к базе данных Neon
    try:
        # Для Neon обязательно используем ssl='require'
        pool = await asyncpg.create_pool(DATABASE_URL, ssl='require')
        db = Database(pool)
        await db.create_tables()
        logger.info("✅ База данных подключена и таблицы созданы.")
    except Exception as e:
        logger.error(f"❌ Ошибка БД: {e}")
        return

    # Передаем базу данных во все хэндлеры через контекст
    dp["db"] = db

    # 4. Регистрация роутеров
    dp.include_router(profile_router)
    dp.include_router(stake_router)
    dp.include_router(cards_router)
    dp.include_router(admin_mifl_router)

    # 5. Очистка очереди обновлений
    await bot.delete_webhook(drop_pending_updates=True)

    # 6. Запуск веб-сервера (решаем проблему 'No open ports')
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render сам подставит нужный порт в переменную PORT
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    
    # Запускаем сервер и бота параллельно
    await site.start()
    logger.info(f"🌐 Веб-сервер запущен на порту {port}")
    
    try:
        logger.info("🚀 Бот запущен!")
        await dp.start_polling(bot)
    finally:
        await pool.close()
        logger.info("👋 Бот остановлен, соединение с БД закрыто.")

if __name__ == "__main__":
    asyncio.run(main())
