from aiogram import Router, types
from aiogram.filters import Command
import logging

admin_router = Router()
ADMIN_ID = 1866813859 # Твой ID

def get_rarity_by_rating(rating: float) -> str:
    """Твоя персональная шкала редкостей"""
    if 0.5 <= rating <= 1.5: return "Stock"
    if 2.0 <= rating <= 2.5: return "Series"
    if 3.0 <= rating <= 3.5: return "Drop"
    if 4.0 <= rating <= 4.5: return "Chase"
    if rating == 5.0: return "One"
    return "Stock"

@admin_router.message(Command("add_player"))
async def add_player(message: types.Message, db):
    if message.from_user.id != ADMIN_ID:
        return

    # Классический шаблон: /add_player Имя Позиция Рейтинг Клуб
    # (Мы убрали разделители |, так как в старой версии ты просто писал через пробел)
    try:
        args = message.text.split(maxsplit=4)
        
        name = args[1]
        position = args[2].upper()
        rating = float(args[3])
        club = args[4]

        rarity = get_rarity_by_rating(rating)

        card_id = await db.pool.fetchval(
            """INSERT INTO mifl_cards (name, position, rarity, rating, club) 
               VALUES ($1, $2, $3, $4, $5) RETURNING card_id""",
            name, position, rarity, rating, club
        )
        
        # Тот самый стиль вывода из старых логов
        response = (
            f"📥 <b>Игрок успешно внесен в базу!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>ID:</b> <code>{card_id}</code>\n"
            f"👤 <b>Имя:</b> {name}\n"
            f"⚽ <b>Позиция:</b> {position}\n"
            f"📊 <b>Рейтинг:</b> {rating}\n"
            f"💎 <b>Редкость:</b> {rarity}\n"
            f"🛡 <b>Клуб:</b> {club}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Для привязки фото ответьте на изображение командой:</i>\n"
            f"<code>/set_photo {card_id}</code>"
        )
        
        await message.answer(response)

    except (IndexError, ValueError):
        await message.answer(
            "❌ <b>Ошибка синтаксиса!</b>\n\n"
            "Используй старый шаблон:\n"
            "<code>/add_player Имя Позиция Рейтинг Клуб</code>\n\n"
            "<i>Пример: /add_player Прокоп MID 5.0 Амкал</i>"
        )

@admin_router.message(Command("set_photo"))
async def set_photo(message: types.Message, db):
    if message.from_user.id != ADMIN_ID:
        return
        
    if not message.reply_to_message or not message.reply_to_message.photo:
        return await message.answer("⚠️ Ответь командой на фото игрока!")

    try:
        card_id = int(message.text.split()[1])
        photo_id = message.reply_to_message.photo[-1].file_id
        
        await db.pool.execute("UPDATE mifl_cards SET photo_id = $1 WHERE card_id = $2", photo_id, card_id)
        await message.answer(f"📸 Фото привязано к карточке #{card_id}")
    except:
        await message.answer("Ошибка! Введи: <code>/set_photo ID</code>")
