from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from utils.states import UserBetting
from utils.boost_calc import calculate_personal_boost
import re

stake_router = Router()

@stake_router.message(F.text == "Матчи")
async def show_matches(message: types.Message, db):
    # Достаем все открытые матчи
    matches = await db.pool.fetch("SELECT * FROM matches WHERE status = 'open' ORDER BY match_date ASC")
    if not matches:
        return await message.answer("❌ На данный момент открытых матчей нет.")
    
    kb = []
    for m in matches:
        kb.append([InlineKeyboardButton(text=f"⚽ {m['team_a']} - {m['team_b']}", callback_data=f"match_{m['match_id']}")])
        
    await message.answer("🔥 <b>Линия MIFL STAKE</b>\nВыбери матч для ставки:", 
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

@stake_router.callback_query(F.data.startswith("match_"))
async def show_match_odds(call: types.CallbackQuery, db, state: FSMContext):
    match_id = int(call.data.split("_")[1])
    
    match = await db.pool.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
    odds = await db.pool.fetch("SELECT * FROM match_odds WHERE match_id = $1", match_id)
    
    if not odds:
        return await call.answer("Коэффициенты еще не загружены!", show_alert=True)

    # 1. Считаем персональные бусты от карт
    boost_a = await calculate_personal_boost(db, call.from_user.id, match['team_a'])
    boost_b = await calculate_personal_boost(db, call.from_user.id, match['team_b'])
    
    text = f"🏟 <b>{match['team_a']} vs {match['team_b']}</b>\n\n"
    if boost_a > 0 or boost_b > 0:
        text += "✨ <i>Действуют бусты от твоих карт!</i>\n\n"

    kb = []
    odds_dict = {o['outcome_type']: o['odd_value'] for o in odds}
    
    # Формируем кнопки с учетом бустов (П1 получает буст Команды А, П2 - Команды Б)
    def make_btn(text_label, o_type, team_boost=0.0):
        if o_type in odds_dict:
            final_odd = round(odds_dict[o_type] + team_boost, 2)
            btn_text = f"{text_label} ({final_odd})"
            return InlineKeyboardButton(text=btn_text, callback_data=f"bet_{match_id}_{o_type}_{final_odd}")
        return None

    # П1, Х, П2
    row1 = [
        make_btn("П1", "W1", boost_a),
        make_btn("Х", "X", 0.0), # Ничья без бустов
        make_btn("П2", "W2", boost_b)
    ]
    kb.append([b for b in row1 if b])
    
    # Тоталы и ОЗ
    row2 = [make_btn("ТБ 2.5", "TB2.5"), make_btn("ТМ 2.5", "TM2.5")]
    row3 = [make_btn("ОЗ: Да", "OZ_YES"), make_btn("ОЗ: Нет", "OZ_NO")]
    kb.append([b for b in row2 if b])
    kb.append([b for b in row3 if b])
    
    # Кнопка для точного счета
    kb.append([InlineKeyboardButton(text="🎯 Точный счет", callback_data=f"bet_exact_{match_id}")])

    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

@stake_router.callback_query(F.data.startswith("bet_"))
async def init_bet(call: types.CallbackQuery, state: FSMContext):
    data = call.data.split("_")
    
    if data[1] == "exact":
        match_id = int(data[2])
        await state.update_data(match_id=match_id, bet_type="single", outcome="EXACT")
        await call.message.answer("📝 Введи ожидаемый счет в формате <b>Число:Число</b> (например, 2:1):", parse_mode="HTML")
        await state.set_state(UserBetting.enter_exact_score)
    else:
        match_id, outcome, odd = int(data[1]), data[2], float(data[3])
        await state.update_data(match_id=match_id, bet_type="single", outcome=outcome, odd=odd)
        await call.message.answer(f"💰 Выбран исход <b>{outcome}</b> с кэфом <b>{odd}</b>.\nВведите сумму ставки (🌟):", parse_mode="HTML")
        await state.set_state(UserBetting.enter_bet_amount)

# Точный счет мы уже расписали в предыдущем сообщении!

@stake_router.message(UserBetting.enter_bet_amount)
async def place_bet(message: types.Message, state: FSMContext, db):
    if not message.text.isdigit():
        return await message.answer("❌ Сумма должна быть числом.")
        
    amount = int(message.text)
    user = await db.pool.fetchrow("SELECT stars FROM users WHERE user_id = $1", message.from_user.id)
    
    if amount < 100 or amount > user['stars']:
        return await message.answer("❌ Недостаточно средств или ставка меньше 100 🌟.")

    data = await state.get_data()
    
    # Если это точный счет, кэф ставим фиксированный или считаем отдельно
    odd = data.get('odd', 5.0) # Заглушка: на точный счет даем кэф 5.0
    exact_score = data.get('exact_score', None)

    # 1. Списываем деньги
    await db.pool.execute("UPDATE users SET stars = stars - $1 WHERE user_id = $2", amount, message.from_user.id)
    
    # 2. Создаем ставку
    bet_id = await db.pool.fetchval(
        "INSERT INTO bets (user_id, bet_type, amount, total_odd) VALUES ($1, 'single', $2, $3) RETURNING bet_id",
        message.from_user.id, amount, odd
    )
    
    # 3. Привязываем матч к ставке
    await db.pool.execute(
        "INSERT INTO bet_items (bet_id, match_id, selected_outcome, exact_score) VALUES ($1, $2, $3, $4)",
        bet_id, data['match_id'], data['outcome'], exact_score
    )

    await message.answer(f"✅ <b>Ставка принята!</b>\nСумма: {amount} 🌟\nКоэффициент: {odd}\nВозможный выигрыш: {int(amount * odd)} 🌟", parse_mode="HTML")
    await state.clear()
