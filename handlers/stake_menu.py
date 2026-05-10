from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from utils.states import UserBetting
import re

stake_router = Router()

@stake_router.message(UserBetting.enter_exact_score)
async def process_exact_score(message: types.Message, state: FSMContext, db):
    score_text = message.text.strip()
    
    # Регулярное выражение: строго "Число:Число" (без пробелов или с ними, но знак только двоеточие)
    if not re.match(r"^\d+\s*:\s*\d+$", score_text):
        return await message.answer(
            "⚠️ Недействительный шаблон.\n"
            "Запишите пожалуйста так: <b>Число:Число</b> (например, 2:1)",
            parse_mode="HTML"
        )
    
    # Очищаем от пробелов, если юзер ввел "2 : 1", делаем "2:1"
    clean_score = score_text.replace(" ", "")
    
    await state.update_data(exact_score=clean_score)
    await message.answer(f"Счет {clean_score} принят. Введите сумму ставки (🌟):")
    await state.set_state(UserBetting.enter_bet_amount)

