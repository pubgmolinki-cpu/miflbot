from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
import logging

profile_router = Router()

@profile_router.message(F.text == "👤 Профиль")
async def view_profile(message: types.Message, state: FSMContext, db):
    await state.clear()
    
    u = await db.pool.fetchrow("SELECT * FROM users WHERE user_id = $1", message.from_user.id)
    if not u: return
    
    cards_count = await db.pool.fetchval("SELECT COUNT(*) FROM inventory WHERE user_id = $1", message.from_user.id)
    
    # Расчет статистики ставок (MIFL STAKE)
    stats = await db.pool.fetchrow("""
        SELECT 
            COUNT(*) FILTER (WHERE status = 'won') as wins,
            COUNT(*) FILTER (WHERE status = 'lost') as losses,
            COUNT(*) FILTER (WHERE status = 'active') as active,
            SUM(amount) FILTER (WHERE status = 'won') as total_earned
        FROM bets 
        WHERE user_id = $1
    """, message.from_user.id)

    wins = stats['wins'] or 0
    losses = stats['losses'] or 0
    active = stats['active'] or 0
    total_settled = wins + losses
    
    # Автоматический расчет винрейта
    winrate = round((wins / total_settled) * 100, 1) if total_settled > 0 else 0.0

    caption = (
        f"👤 <b>Профиль: {u['username']}</b>\n"
        f"💰 Баланс: {u['stars']:,} 🌟\n"
        f"🎴 Карт: {cards_count}\n\n"
        f"📊 <b>Статистика MIFL STAKE:</b>\n"
        f"✅ Побед: {wins} | ❌ Поражений: {losses}\n"
        f"⏳ Активных ставок: {active}\n"
        f"📈 Винрейт: <b>{winrate}%</b>"
    )
    
    await message.answer(caption, parse_mode="HTML")
