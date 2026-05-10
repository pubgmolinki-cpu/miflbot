import os
import json
from aiogram import Router, F, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from config import ADMIN_IDS

admin_mifl_router = Router()

# Фильтр для админов
def is_admin(message: types.Message):
    return message.from_user.id in ADMIN_IDS

@admin_mifl_router.message(Command("create_match"), F.func(is_admin))
async def cmd_create_match(message: types.Message, command: CommandObject, db):
    try:
        team_a, team_b = command.args.split("-")
        team_a, team_b = team_a.strip(), team_b.strip()
        
        match_id = await db.pool.fetchval(
            "INSERT INTO matches (team_a, team_b, status) VALUES ($1, $2, 'open') RETURNING match_id",
            team_a, team_b
        )
        await message.answer(f"✅ Матч создан!\nID: <code>{match_id}</code>\n{team_a} vs {team_b}", parse_mode="HTML")
    except:
        await message.answer("❌ Формат: /create_match Команда А - Команда Б")

@admin_mifl_router.message(Command("set_odds"), F.func(is_admin))
async def cmd_set_odds(message: types.Message, command: CommandObject, db):
    # Пример формата: /set_odds 1 W1:2.1 X:3.4 W2:2.8
    args = command.args.split()
    match_id = int(args[0])
    
    odds_data = []
    for odd in args[1:]:
        otype, ovalue = odd.split(":")
        odds_data.append((match_id, otype.upper(), float(ovalue)))
    
    await db.pool.executemany(
        "INSERT INTO match_odds (match_id, outcome_type, odd_value) VALUES ($1, $2, $3) ON CONFLICT (match_id, outcome_type) DO UPDATE SET odd_value = EXCLUDED.odd_value",
        odds_data
    )
    await message.answer(f"✅ Кэфы для матча {match_id} загружены!")

@admin_mifl_router.message(Command("set_score"), F.func(is_admin))
async def cmd_set_score(message: types.Message, command: CommandObject, db):
    # Формат: /set_score 1 2:1
    match_id, score = command.args.split()
    
    # 1. Закрываем матч и ставим счет
    await db.pool.execute("UPDATE matches SET status = 'finished', score = $1 WHERE match_id = $2", score, int(match_id))
    await message.answer(f"✅ Счет {score} установлен для матча {match_id}. Запускаю расчет ставок...")
    
    # ПРИМЕЧАНИЕ: Здесь нужно будет дописать логику расчета (кто выиграл). 
    # Бот должен распарсить 2:1 -> П1 (выигрыш), ТБ 2.5 (выигрыш), ОЗ (Да) (выигрыш).
    # И обновить статусы в таблице bet_items.

@admin_mifl_router.message(Command("ai_analyze"), F.func(is_admin))
async def cmd_ai_analyze(message: types.Message, command: CommandObject):
    context = command.args
    if not context:
        return await message.answer("❌ Напиши данные. Пример: /ai_analyze Матч МФК Риск - Броуки, Риск фаворит, у броуков нет вратаря.")
    
    await message.answer("🤖 ИИ анализирует матч, ожидайте...")
    
    # ПРОМПТ ДЛЯ ИИ (Ты можешь использовать библиотеку openai или google-generativeai)
    prompt = f"""
    Ты — букмекер медийной футбольной лиги. Рассчитай коэффициенты на матч на основе этих данных: {context}.
    Выдай ответ ТОЛЬКО в формате JSON:
    {{"W1": 0.0, "X": 0.0, "W2": 0.0, "TB2.5": 0.0, "TM2.5": 0.0, "OZ_YES": 0.0, "OZ_NO": 0.0}}
    Учти маржу букмекера 10%.
    """
    
    # Здесь должен быть API-запрос к нейросети. Пока ставим заглушку:
    dummy_response = '{"W1": 1.8, "X": 3.6, "W2": 4.1, "TB2.5": 1.6, "TM2.5": 2.2, "OZ_YES": 1.5, "OZ_NO": 2.4}'
    
    try:
        odds = json.loads(dummy_response)
        text = f"📊 <b>Анализ завершен!</b>\n\nСкопируй команду ниже, чтобы применить кэфы (замени ID):\n"
        cmd = f"/set_odds [ID]"
        for k, v in odds.items(): cmd += f" {k}:{v}"
        
        await message.answer(text + f"<code>{cmd}</code>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка ИИ: {e}")
