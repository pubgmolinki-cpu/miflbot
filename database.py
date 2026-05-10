import asyncpg

class Database:
    def __init__(self, pool):
        self.pool = pool

    async def create_tables(self):
        async with self.pool.acquire() as conn:
            # Твои старые таблицы...
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, stars INT DEFAULT 0, vip_until TIMESTAMP, last_drop TIMESTAMP, last_bonus TIMESTAMP);
                CREATE TABLE IF NOT EXISTS mifl_cards (card_id SERIAL PRIMARY KEY, name TEXT, rating FLOAT, club TEXT, position TEXT, rarity TEXT, photo_id TEXT);
                CREATE TABLE IF NOT EXISTS promocodes (code TEXT PRIMARY KEY, stars INT, max_uses INT, current_uses INT DEFAULT 0);
                CREATE TABLE IF NOT EXISTS used_promos (user_id BIGINT, code TEXT);
                CREATE TABLE IF NOT EXISTS active_trades (code TEXT PRIMARY KEY, user_a BIGINT, card_a INT);
            """)
            
            # ИНВЕНТАРЬ (добавлено время получения для буста)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS inventory (
                    user_id BIGINT, 
                    card_id INT, 
                    obtained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # НОВЫЕ ТАБЛИЦЫ MIFL STAKE
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS matches (
                    match_id SERIAL PRIMARY KEY,
                    team_a TEXT,
                    team_b TEXT,
                    match_date TIMESTAMP,
                    status TEXT DEFAULT 'open', -- 'open', 'closed', 'finished'
                    score TEXT DEFAULT NULL
                );

                CREATE TABLE IF NOT EXISTS match_odds (
                    match_id INT REFERENCES matches(match_id),
                    outcome_type TEXT, -- 'W1', 'W2', 'X', 'TB2.5', 'TM2.5', 'OZ_YES', 'OZ_NO'
                    odd_value FLOAT,
                    PRIMARY KEY(match_id, outcome_type)
                );

                CREATE TABLE IF NOT EXISTS bets (
                    bet_id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    bet_type TEXT, -- 'single' или 'express'
                    amount INT,
                    total_odd FLOAT,
                    status TEXT DEFAULT 'active', -- 'active', 'won', 'lost', 'refund'
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS bet_items (
                    bet_id INT REFERENCES bets(bet_id),
                    match_id INT REFERENCES matches(match_id),
                    selected_outcome TEXT,
                    exact_score TEXT DEFAULT NULL, -- заполняется, если ставка на точный счет
                    applied_boost FLOAT DEFAULT 0.0, -- сохраненный буст от карты на момент ставки
                    status TEXT DEFAULT 'pending' -- 'pending', 'won', 'lost'
                );
            """)

    # Остальные функции (get_user, update_stars) остаются без изменений
