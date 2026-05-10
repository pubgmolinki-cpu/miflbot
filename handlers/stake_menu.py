from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from utils.states import UserBetting
from utils.boost_calc import calculate_personal_boost
import re

stake_router = Router()

@stake_router.message(F.text == "⚽ Матчи")
async def show_matches(message: types.Message, db):
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

    boost_a = await calculate_personal_boost(db, call.from_user.id, match['team_a'])
    boost_b = await calculate_personal_boost(db, call.from_user.id, match['team_b'])
    
    text = f"🏟 <b>{match['team_a']} vs {match['team_b']}</b>\n\n"
    if boost_a > 0 or boost_b > 0:
        text += f"✨ <b>Твои бусты сегодня:</b>\n"
        if boost_a > 0: text += f"— {match['team_a']}: +{boost_a}\n"
        if boost_b > 0: text += f"— {match['team_b']}: +{boost_b}\n\n"

    kb = []
    odds_dict = {o['outcome_type']: o['odd_value'] for o in odds}
    
    def make_btn(text_label, o_type, team_boost=0.0):
        if o_type in odds_dict:
            final_odd = round(odds_dict[o_type] + team_boost, 2)
            return InlineKeyboardButton(text=f"{text_label} ({final_odd})", callback_data=f"bet_{match_id}_{o_type}_{final_odd}")
        return None

    kb.append([b for b in [make_btn("П1", "W1", boost_a), make_btn("Х", "X"), make_btn("П2", "W2", boost_b)] if b])
    kb.append([b for b in [make_btn("ТБ 2.5", "TB2.5"), make_btn("ТМ 2.5", "TM2.5")] if b])
    kb.append([b for b in [make_btn("ОЗ: Да", "OZ_YES"), make_btn("ОЗ: Нет", "OZ_NO")] if b])
    kb.append([InlineKeyboardButton(text="🎯 Точный счет", callback_data=f"bet_exact_{match_id}")])

    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

@stake_router.callback_query(F.data.startswith("bet_"))
async def init_bet(call: types.CallbackQuery, state: FSMContext):
    data = call.data.split("_")
    if data[1] == "exact":
        await state.update_data(match_id=int(data[2]), outcome="EXACT", odd=10.0) # Кэф 10 на любой счет
        await call.message.answer("📝 Введи счет в формате <b>Число:Число</b> (например, 2:1):", parse_mode="HTML")
        await state.set_state(UserBetting.enter_exact_score)
    else:
        await state.update_data(match_id=int(data[1]), outcome=data[2], odd=float(data[3]))
        await call.message.answer(f"💰 Исход: <b>{data[2]}</b> (Кэф {data[3]})\nВведите сумму ставки (🌟):", parse_mode="HTML")
        await state.set_state(UserBetting.enter_bet_amount)

@stake_router.message(UserBetting.enter_exact_score)
async def process_exact_score(message: types.Message, state: FSMContext):
    if not re.match(r"^\d+:\d+$", message.text.replace(" ", "")):
        return await message.answer("⚠️ Ошибка! Формат должен быть <b>Число:Число</b>")
    await state.update_data(exact_score=message.text.replace(" ", ""))
    await message.answer("Счет принят. Введите сумму ставки (🌟):")
    await state.set_state(UserBetting.enter_bet_amount)

@stake_router.message(UserBetting.enter_bet_amount)
async def place_bet(message: types.Message, state: FSMContext, db):
    if not message.text.isdigit(): return
    amount = int(message.text)
    user = await db.pool.fetchrow("SELECT stars FROM users WHERE user_id = $1", message.from_user.id)
    if amount < 100 or amount > user['stars']:
        return await message.answer("❌ Недостаточно звезд или ставка меньше 100.")

    data = await state.get_data()
    await db.pool.execute("UPDATE users SET stars = stars - $1 WHERE user_id = $2", amount, message.from_user.id)
    bet_id = await db.pool.fetchval("INSERT INTO bets (user_id, bet_type, amount, total_odd) VALUES ($1, 'single', $2, $3) RETURNING bet_id",
                                     message.from_user.id, amount, data['odd'])
    await db.pool.execute("INSERT INTO bet_items (bet_id, match_id, selected_outcome, exact_score) VALUES ($1, $2, $3, $4)",
                           bet_id, data['match_id'], data['outcome'], data.get('exact_score'))

    await message.answer(f"✅ Ставка принята!\nВозможный выигрыш: {int(amount * data['odd'])} 🌟")
    await state.clear()

@stake_router.message(F.text == "📋 Мои Ставки")
async def my_bets(message: types.Message, db):
    bets = await db.pool.fetch("""
        SELECT b.amount, b.total_odd, b.status, bi.selected_outcome, m.team_a, m.team_b, m.score
        FROM bets b JOIN bet_items bi ON b.bet_id = bi.bet_id JOIN matches m ON bi.match_id = m.match_id
        WHERE b.user_id = $1 ORDER BY b.created_at DESC LIMIT 5
    """, message.from_user.id)
    if not bets: return await message.answer("У тебя пока нет ставок.")
    
    txt = "<b>📋 Твои ставки:</b>\n\n"
    for b in bets:
        sm = {"won": "✅", "lost": "❌", "active": "⏳"}.get(b['status'], "❓")
        score = f"({b['score']})" if b['score'] else ""
        txt += f"{sm} {b['team_a']} - {b['team_b']} {score}\n   {b['selected_outcome']} | {b['amount']}🌟 | Кэф {b['total_odd']}\n\n"
    await message.answer(txt, parse_mode="HTML")
