import asyncio
import logging
import os
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

# Импорт конфигурации
from config import TOKEN, DATABASE_URL

# Импорт базы данных
from database import Database

# Импорт всех наших роутеров (модулей)
from handlers.user_profile import profile_router
from handlers.stake_menu import stake_router
from handlers.cards_menu import cards_router
from handlers.admin_mifl import admin_mifl_router

# Настройка логирования, чтобы видеть ошибки в консоли
logging.basicConfig(level=logging.INFO)

# 🌐 Простой обработчик для веб-сервера (чтобы хостинг не ругался)
async def handle_ping(request):
    return web.Response(text="MIFL Bot is running and feeling good!")

async def main():
    # 1. Инициализация бота и диспетчера
    # Указываем parse_mode="HTML" по умолчанию, чтобы не писать это в каждом сообщении
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
    dp = Dispatcher()

    # 2. Подключение к базе данных PostgreSQL
    try:
        # ssl='require' часто нужен для внешних БД
        pool = await asyncpg.create_pool(DATABASE_URL, ssl='require')
        db = Database(pool)
        await db.create_tables()
        logging.info("✅ База данных подключена, таблицы проверены.")
    except Exception as e:
        logging.error(f"❌ Ошибка подключения к БД: {e}")
        return

    # 3. Прокидываем объект db во все хэндлеры
    # Теперь любой хэндлер может принимать аргумент `db`
    dp["db"] = db

    # 4. Регистрация всех роутеров
    dp.include_router(profile_router)
    dp.include_router(stake_router)
    dp.include_router(cards_router)
    dp.include_router(admin_mifl_router)

    # 5. Очистка старых обновлений (чтобы бот не спамил старыми ответами при запуске)
    await bot.delete_webhook(drop_pending_updates=True)
    
    # 6. Запуск веб-сервера aiohttp (критически важно для деплоя на облаке)
    app = web.Application()
    app.router.add_get('/', handle_ping)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Получаем порт от системы или используем 8080 по умолчанию
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"🌐 Веб-сервер запущен на порту {port}")

    # 7. Запуск самого бота в режиме polling
    logging.info("🚀 MIFL Bot успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    # Запускаем асинхронную функцию main
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен вручную.")
