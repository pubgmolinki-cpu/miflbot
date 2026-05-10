import asyncio
import asyncpg
import logging
from aiogram import Bot, Dispatcher
from config import TOKEN, DATABASE_URL

# Импорт роутеров из файлов
from handlers.user_profile import profile_router
from handlers.stake_menu import stake_router
from database import Database

logging.basicConfig(level=logging.INFO)

async def main():
    bot = Bot(token=TOKEN)
    dp = Dispatcher()

    # Подключение к БД
    pool = await asyncpg.create_pool(DATABASE_URL, ssl='require')
    db = Database(pool)
    await db.create_tables()
    
    # Прокидываем db во все хэндлеры через middleware (или просто передаем в kwargs)
    dp["db"] = db 

    # Подключение роутеров
    dp.include_router(profile_router)
    dp.include_router(stake_router)
    # dp.include_router(admin_mifl_router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

