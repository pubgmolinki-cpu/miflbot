from aiogram import Router, F, types
from aiogram.filters import Command
import logging

profile_router = Router()

@profile_router.message(Command("start"))
async def cmd_start(message: types.Message, db):
    user_id = message.from_user.id
    
    # Пытаемся добавить юзера, если его еще нет (ON CONFLICT DO NOTHING)
    await db.pool.execute(
        "INSERT INTO users (user_id, stars) VALUES ($1, 1000) ON CONFLICT (user_id) DO NOTHING",
        user_id
    )
    
    # Главное меню с кнопками
    kb = [
        [types.KeyboardButton(text="👤 Профиль"), types.KeyboardButton(text="⚽ Матчи")],
        [types.KeyboardButton(text="🎴 Открыть пак"), types.KeyboardButton(text="🎒 Инвентарь")],
        [types.KeyboardButton(text="📋 Мои Ставки"), types.KeyboardButton(text="🎁 Бонус")]
    ]
    keyboard = types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Добро пожаловать в <b>MIFL STAKE</b> — симулятор ставок на медиафутбол.\n"
        "Тебе начислено 1000 🌟 для старта!",
        reply_markup=keyboard
    )

@profile_router.message(F.text == "👤 Профиль")
async def show_profile(message: types.Message, db):
    user = await db.pool.fetchrow("SELECT * FROM users WHERE user_id = $1", message.from_user.id)
    
    if not user:
        return await message.answer("Сначала напиши /start")

    # Считаем количество карт
    cards_count = await db.pool.fetchval("SELECT COUNT(*) FROM inventory WHERE user_id = $1", message.from_user.id)

    res = (
        f"👤 <b>Профиль: {message.from_user.first_name}</b>\n\n"
        f"💰 Баланс: {user['stars']} 🌟\n"
        f"🎴 Карточек в коллекции: {cards_count}\n"
    )
    await message.answer(res)
