import random
from datetime import datetime, timedelta
from aiogram import Router, F, types
from aiogram.filters import Command

cards_router = Router()

# Твои шансы выпадения
RARITY_CHANCES = {
    "Stock": 60, "Series": 25, "Drop": 10, "Chase": 4, "One": 1
}

@cards_router.message(Command("start"))
async def cmd_start(message: types.Message, db):
    await db.pool.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", message.from_user.id)
    kb = [
        [types.KeyboardButton(text="🎴 Открыть пак")],
        [types.KeyboardButton(text="🎒 Инвентарь"), types.KeyboardButton(text="🎁 Бонус")]
    ]
    keyboard = types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    await message.answer(f"Привет! Это MIFL CARDS. Собирай редкие карточки игроков!", reply_markup=keyboard)

@cards_router.message(F.text == "🎴 Открыть пак")
async def open_pack(message: types.Message, db):
    user = await db.pool.fetchrow("SELECT last_drop FROM users WHERE user_id = $1", message.from_user.id)
    
    if user['last_drop'] and datetime.now() < user['last_drop'] + timedelta(hours=4):
        diff = (user['last_drop'] + timedelta(hours=4)) - datetime.now()
        return await message.answer(f"⏳ Пак будет доступен через {diff.seconds // 60} мин.")

    rarity = random.choices(list(RARITY_CHANCES.keys()), weights=list(RARITY_CHANCES.values()))[0]
    card = await db.pool.fetchrow("SELECT * FROM mifl_cards WHERE rarity = $1 ORDER BY RANDOM() LIMIT 1", rarity)
    
    if not card:
        return await message.answer(f"📦 В категории {rarity} пока нет карт. Админ скоро добавит!")

    await db.pool.execute("INSERT INTO inventory (user_id, card_id) VALUES ($1, $2)", message.from_user.id, card['card_id'])
    await db.pool.execute("UPDATE users SET last_drop = CURRENT_TIMESTAMP WHERE user_id = $1", message.from_user.id)

    caption = (f"🎉 <b>Выпала карта!</b>\n\n👤 {card['name']}\n📊 Рейтинг: {card['rating']}\n"
               f"🛡 Клуб: {card['club']}\n💎 Редкость: {card['rarity']}")
    
    if card['photo_id']:
        await message.answer_photo(card['photo_id'], caption=caption)
    else:
        await message.answer(caption)

@cards_router.message(F.text == "🎒 Инвентарь")
async def show_inventory(message: types.Message, db):
    cards = await db.pool.fetch("""
        SELECT c.name, c.rarity, c.rating FROM inventory i 
        JOIN mifl_cards c ON i.card_id = c.card_id 
        WHERE i.user_id = $1 ORDER BY c.rating DESC
    """, message.from_user.id)
    
    if not cards: return await message.answer("У тебя нет карт.")
    
    text = "<b>🎒 Твоя коллекция:</b>\n\n" + "\n".join([f"• {c['name']} ({c['rarity']}) — {c['rating']}" for c in cards])
    await message.answer(text)
