from config import RARITY_BOOSTS, RARITY_RANKS
from datetime import datetime

async def calculate_personal_boost(db, user_id: int, team_name: str) -> float:
    """Вычисляет буст для конкретной команды на основе карт, выпавших сегодня."""
    
    # Получаем карты пользователя, полученные СЕГОДНЯ, которые принадлежат нужной команде
    query = """
        SELECT c.rarity 
        FROM inventory i 
        JOIN mifl_cards c ON i.card_id = c.card_id 
        WHERE i.user_id = $1 AND c.club = $2 AND DATE(i.obtained_at) = CURRENT_DATE
    """
    cards_today = await db.pool.fetch(query, user_id, team_name)
    
    if not cards_today:
        return 0.0

    # Группируем карты по редкости: {"Stock": 2, "One": 1}
    rarity_counts = {}
    for row in cards_today:
        rarity = row['rarity']
        rarity_counts[rarity] = rarity_counts.get(rarity, 0) + 1

    # Находим самую высокую редкость из выпавших
    highest_rarity = max(rarity_counts.keys(), key=lambda r: RARITY_RANKS[r])
    
    # Считаем сумму бустов только для самой высокой редкости
    boost_value = RARITY_BOOSTS[highest_rarity]
    total_boost = boost_value * rarity_counts[highest_rarity]
    
    return total_boost
