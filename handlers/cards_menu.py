import random
from datetime import datetime, timedelta
from aiogram import Router, F, types
from aiogram.filters import Command

cards_router = Router()

# Твои шансы выпадения (в процентах)
RARITY_CHANCES = {
    "Stock": 60, "Series": 25, "Drop": 10, "Chase": 4, "One": 1
}

@cards_router.message(F.text == "🎴 Открыть пак")
async def open_pack(message: types.Message, db):
    user = await db.pool.fetchrow("SELECT last_drop FROM users WHERE user_id = $1", message.from_user.id)
    
    # Кулдаун 4 часа
    if user and user['last_drop'] and datetime.now() < user['last_drop'] + timedelta(hours=4):
        diff = (user['last_drop'] + timedelta(hours=4)) - datetime.now()
        mins = diff.seconds // 60
        return await message.answer(f"⏳ Следующий пак можно открыть через {mins} мин.")

    # Выбираем редкость по шансам
    rarity = random.choices(list(RARITY_CHANCES.keys()), weights=list(RARITY_CHANCES.values()))[0]
    
    # Ищем случайную карту этой редкости
    card = await db.pool.fetchrow(
        "SELECT * FROM mifl_cards WHERE rarity = $1 ORDER BY RANDOM() LIMIT 1", 
        rarity
    )
    
    if not card:
        return await message.answer(f"📦 В категории {rarity} пока нет игроков. Напиши админу!")

    # Добавляем в инвентарь
    await db.pool.execute(
        "INSERT INTO inventory (user_id, card_id) VALUES ($1, $2)", 
        message.from_user.id, card['card_id']
    )
    await db.pool.execute(
        "UPDATE users SET last_drop = CURRENT_TIMESTAMP WHERE user_id = $1", 
        message.from_user.id
    )

    caption = (
        f"🎉 <b>Тебе выпал новый игрок!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>{card['name']}</b> [<i>{card['position']}</i>]\n"
        f"📊 Рейтинг: {card['rating']}\n"
        f"🛡 Клуб: {card['club']}\n"
        f"💎 Редкость: <b>{card['rarity']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    
    if card['photo_id']:
        await message.answer_photo(card['photo_id'], caption=caption)
    else:
        await message.answer(caption)

@cards_router.message(F.text == "🎒 Инвентарь")
async def show_inventory(message: types.Message, db):
    cards = await db.pool.fetch("""
        SELECT c.name, c.position, c.rarity, c.rating 
        FROM inventory i 
        JOIN mifl_cards c ON i.card_id = c.card_id 
        WHERE i.user_id = $1 
        ORDER BY c.rating DESC
    """, message.from_user.id)
    
    if not cards:
        return await message.answer("🎒 Твой инвентарь пока пуст. Открой свой первый пак!")
    
    text = "<b>🎒 Твоя коллекция игроков:</b>\n\n"
    for c in cards:
        text += f"• {c['name']} [{c['position']}] — <b>{c['rating']}</b> ({c['rarity']})\n"
    
    await message.answer(text)
