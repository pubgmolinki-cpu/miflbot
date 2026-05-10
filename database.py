import asyncpg
import logging

class Database:
    def __init__(self, pool):
        self.pool = pool

    async def create_tables(self):
        async with self.pool.acquire() as conn:
            # Используем транзакцию, чтобы всё применилось разом
            async with conn.transaction():
                logging.info("🛠 Пересоздаю таблицы базы данных...")
                
                # ЛЕНИВЫЙ СПОСОБ: Если структура кривая, мы её сносим и ставим ровно.
                # ВНИМАНИЕ: Это удалит текущие данные о матчах и ставках! 
                # Если база пустая или тестовая — это лучший вариант.
                
                await conn.execute("""
                    -- Сначала удаляем таблицы в обратном порядке (из-за связей)
                    DROP TABLE IF EXISTS bet_items CASCADE;
                    DROP TABLE IF EXISTS match_odds CASCADE;
                    DROP TABLE IF EXISTS bets CASCADE;
                    DROP TABLE IF EXISTS matches CASCADE;
                    DROP TABLE IF EXISTS inventory CASCADE;
                    DROP TABLE IF EXISTS mifl_cards CASCADE;
                    DROP TABLE IF EXISTS users CASCADE;

                    -- 1. Таблица пользователей
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        stars INTEGER DEFAULT 1000,
                        last_drop TIMESTAMP,
                        last_bonus TIMESTAMP
                    );

                    -- 2. Таблица карточек
                    CREATE TABLE IF NOT EXISTS mifl_cards (
                        card_id SERIAL PRIMARY KEY,
                        name TEXT,
                        rarity TEXT,
                        rating REAL,
                        club TEXT,
                        photo_id TEXT
                    );

                    -- 3. Инвентарь
                    CREATE TABLE IF NOT EXISTS inventory (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(user_id),
                        card_id INTEGER REFERENCES mifl_cards(card_id)
                    );

                    -- 4. МАТЧИ (Тут была ошибка с match_id)
                    CREATE TABLE IF NOT EXISTS matches (
                        match_id SERIAL PRIMARY KEY, -- Вот он, главный ключ
                        team_a TEXT,
                        team_b TEXT,
                        match_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        status TEXT DEFAULT 'open', -- open, finished
                        score TEXT
                    );

                    -- 5. Коэффициенты (Ссылаются на match_id)
                    CREATE TABLE IF NOT EXISTS match_odds (
                        id SERIAL PRIMARY KEY,
                        match_id INTEGER REFERENCES matches(match_id) ON DELETE CASCADE,
                        outcome_type TEXT, -- W1, X, W2, TB2.5, etc.
                        odd_value REAL
                    );

                    -- 6. Ставки
                    CREATE TABLE IF NOT EXISTS bets (
                        bet_id SERIAL PRIMARY KEY,
                        user_id BIGINT REFERENCES users(user_id),
                        bet_type TEXT, -- single, express
                        amount INTEGER,
                        total_odd REAL,
                        status TEXT DEFAULT 'active', -- active, won, lost
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    -- 7. Детали ставки (Связь ставки и матча)
                    CREATE TABLE IF NOT EXISTS bet_items (
                        id SERIAL PRIMARY KEY,
                        bet_id INTEGER REFERENCES bets(bet_id) ON DELETE CASCADE,
                        match_id INTEGER REFERENCES matches(match_id) ON DELETE CASCADE,
                        selected_outcome TEXT,
                        exact_score TEXT
                    );
                """)
                logging.info("✅ Все таблицы успешно пересозданы.")
