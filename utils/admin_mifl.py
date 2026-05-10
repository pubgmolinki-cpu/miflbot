from utils.result_processor import get_winning_outcomes

@admin_mifl_router.message(Command("set_score"), F.func(is_admin))
async def cmd_set_score(message: types.Message, command: CommandObject, db):
    match_id, score = command.args.split()
    match_id = int(match_id)
    
    # 1. Получаем список победивших исходов
    winners = get_winning_outcomes(score)
    
    # 2. Обновляем статус матча
    await db.pool.execute("UPDATE matches SET status = 'finished', score = $1 WHERE match_id = $2", score, match_id)
    
    # 3. Находим все ОРДИНАРЫ на этот матч
    active_bets = await db.pool.fetch("""
        SELECT b.bet_id, b.user_id, b.amount, b.total_odd, bi.selected_outcome, bi.exact_score
        FROM bets b
        JOIN bet_items bi ON b.bet_id = bi.bet_id
        WHERE bi.match_id = $1 AND b.status = 'active' AND b.bet_type = 'single'
    """, match_id)

    for bet in active_bets:
        is_win = False
        # Проверка обычного исхода
        if bet['selected_outcome'] in winners:
            is_win = True
        # Проверка точного счета
        elif bet['selected_outcome'] == "EXACT" and bet['exact_score'] == score:
            is_win = True
            
        if is_win:
            win_amount = int(bet['amount'] * bet['total_odd'])
            await db.pool.execute("UPDATE users SET stars = stars + $1 WHERE user_id = $2", win_amount, bet['user_id'])
            await db.pool.execute("UPDATE bets SET status = 'won' WHERE bet_id = $1", bet['bet_id'])
            # Тут можно добавить уведомление юзеру через bot.send_message
        else:
            await db.pool.execute("UPDATE bets SET status = 'lost' WHERE bet_id = $1", bet['bet_id'])

    await message.answer(f"🏁 Матч {match_id} завершен! Ставки рассчитаны.")
