import random
from datetime import datetime, timedelta
from aiogram import Router, F, types

cards_router = Router()

# Шансы выпадения редкостей
RARITY_CHANCES = {
    "Stock": 60,   # 60%
    "Series": 25,  # 25%
    "Drop": 10,    # 10%
    "Chase": 4,    # 4%
    "One": 1       # 1%
}

@cards_router.message(F.text == "🎴 Открыть пак")
async def cmd_drop(message: types.Message, db):
    user = await db.pool.fetchrow("SELECT last_drop FROM users WHERE user_id = $1", message.from_user.id)
    
    # Проверка Кулдауна (4 часа)
    if user['last_drop'] and datetime.now() < user['last_drop'] + timedelta(hours=4):
        diff = (user['last_drop'] + timedelta(hours=4)) - datetime.now()
        return await message.answer(f"⏳ Подожди еще {diff.seconds // 60} мин.")

    # Выбор редкости по шансам
    rarity = random.choices(list(RARITY_CHANCES.keys()), weights=list(RARITY_CHANCES.values()))[0]
    
    # Берем рандомную карту этой редкости
    card = await db.pool.fetchrow("SELECT * FROM mifl_cards WHERE rarity = $1 ORDER BY RANDOM() LIMIT 1", rarity)
    
    if not card:
        return await message.answer("❌ В этой категории пока нет карт.")

    # Добавляем в инвентарь и обновляем время
    await db.pool.execute("INSERT INTO inventory (user_id, card_id) VALUES ($1, $2)", message.from_user.id, card['card_id'])
    await db.pool.execute("UPDATE users SET last_drop = CURRENT_TIMESTAMP WHERE user_id = $1", message.from_user.id)

    caption = (
        f"🎉 <b>Тебе выпала карта!</b>\n\n"
        f"👤 Игрок: {card['name']}\n"
        f"📊 Рейтинг: {card['rating']}\n"
        f"🛡 Клуб: {card['club']}\n"
        f"💎 Редкость: <b>{card['rarity']}</b>"
    )
    
    if card['photo_id']:
        await message.answer_photo(card['photo_id'], caption=caption, parse_mode="HTML")
    else:
        await message.answer(caption, parse_mode="HTML")

@cards_router.message(F.text == "🎒 Инвентарь")
async def cmd_inventory(message: types.Message, db):
    cards = await db.pool.fetch("""
        SELECT c.name, c.rarity, c.rating 
        FROM inventory i JOIN mifl_cards c ON i.card_id = c.card_id 
        WHERE i.user_id = $1 ORDER BY c.rating DESC
    """, message.from_user.id)
    
    if not cards:
        return await message.answer("Твой инвентарь пуст. Открой свой первый пак!")

    res = f"<b>🎒 Твои карты ({len(cards)} шт.):</b>\n\n"
    for c in cards[:20]: # Показываем только первые 20 для краткости
        res += f"• {c['name']} ({c['rarity']}) — {c['rating']}\n"
    
    await message.answer(res, parse_mode="HTML")

@cards_router.message(F.text == "🎁 Бонус")
async def cmd_bonus(message: types.Message, db):
    user = await db.pool.fetchrow("SELECT last_bonus FROM users WHERE user_id = $1", message.from_user.id)
    
    if user['last_bonus'] and datetime.now() < user['last_bonus'] + timedelta(days=1):
        return await message.answer("❌ Ты уже забирал бонус сегодня!")

    amount = random.randint(100, 500)
    await db.pool.execute("UPDATE users SET stars = stars + $1, last_bonus = CURRENT_TIMESTAMP WHERE user_id = $2", 
                           amount, message.from_user.id)
    
    await message.answer(f"🎁 Ты получил <b>{amount} 🌟</b>! Приходи завтра.")
