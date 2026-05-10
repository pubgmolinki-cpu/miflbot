def get_winning_outcomes(score_text: str):
    # Парсим счет "2:1" -> home=2, away=1
    home, away = map(int, score_text.split(':'))
    total = home + away
    
    outcomes = []
    
    # П1, Х, П2
    if home > away: outcomes.append("W1")
    elif away > home: outcomes.append("W2")
    else: outcomes.append("X")
    
    # Тоталы
    if total > 2.5: outcomes.append("TB2.5")
    else: outcomes.append("TM2.5")
    
    # Обе забьют
    if home > 0 and away > 0: outcomes.append("OZ_YES")
    else: outcomes.append("OZ_NO")
    
    return outcomes
