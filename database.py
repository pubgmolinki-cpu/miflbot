import asyncpg
import logging

class Database:
    def __init__(self, pool):
        self.pool = pool

    async def create_tables(self):
        async with self.pool.acquire() as conn:
            logging.info("🛠 Проверка и создание таблиц...")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    stars INTEGER DEFAULT 1000,
                    last_drop TIMESTAMP,
                    last_bonus TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS mifl_cards (
                    card_id SERIAL PRIMARY KEY,
                    name TEXT,
                    position TEXT,
                    rarity TEXT,
                    rating REAL,
                    club TEXT,
                    photo_id TEXT
                );

                CREATE TABLE IF NOT EXISTS inventory (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    card_id INTEGER REFERENCES mifl_cards(card_id) ON DELETE CASCADE
                );
            """)
            logging.info("✅ База данных готова.")
