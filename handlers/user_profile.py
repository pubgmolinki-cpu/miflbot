from aiogram import Router, F, types
from aiogram.filters import Command

profile_router = Router()

@profile_router.message(Command("start"))
async def cmd_start(message: types.Message, db):
    await db.pool.execute(
        "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", 
        message.from_user.id
    )
    
    kb = [
        [types.KeyboardButton(text="🎴 Открыть пак")],
        [types.KeyboardButton(text="👤 Профиль"), types.KeyboardButton(text="🎒 Инвентарь")],
        [types.KeyboardButton(text="🎁 Бонус")]
    ]
    keyboard = types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Это <b>MIFL CARDS</b>. Собирай карточки медиафутболистов, "
        "выбивай редкие 'One' и создавай лучший состав!",
        reply_markup=keyboard
    )

@profile_router.message(F.text == "👤 Профиль")
async def show_profile(message: types.Message, db):
    user = await db.pool.fetchrow("SELECT * FROM users WHERE user_id = $1", message.from_user.id)
    count = await db.pool.fetchval("SELECT COUNT(*) FROM inventory WHERE user_id = $1", message.from_user.id)
    
    res = (
        f"👤 <b>Профиль: {message.from_user.full_name}</b>\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🌟 Звезд: {user['stars'] if user else 0}\n"
        f"🎴 Всего карточек: {count}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    await message.answer(res)
