from aiogram.fsm.state import State, StatesGroup

class AdminStake(StatesGroup):
    create_match_teams = State()
    create_match_odds = State()
    set_score = State()
    ai_analyzer = State()

class UserBetting(StatesGroup):
    select_match = State()
    select_outcome = State()
    enter_exact_score = State() # Сюда попадает юзер, если выбрал исход "Точный счет"
    enter_bet_amount = State()
