import asyncio
import asyncpg
import os
import random
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Awaitable

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from contextlib import asynccontextmanager
from aiohttp import web

# ================= НАСТРОЙКИ =================
TOKEN        = os.getenv("TOKEN", "YOUR_BOT_TOKEN_HERE")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID     = int(os.getenv("ADMIN_ID", 1866813859))

MAINTENANCE_MODE    = False
MAINTENANCE_MESSAGE = (
    "🔧 <b>Бот на техническом обслуживании</b>\n\n"
    "Мы обновляем базу данных и улучшаем работу бота.\n"
    "Пожалуйста, попробуй позже."
)

REQUIRED_CHANNELS = {
    "@miflcards": "https://t.me/miflcards", # Поменяй на свой канал!
}

REF_CHECK_DAYS      = 5
REF_BONUS_AMOUNT    = 5000
MODERATION_GROUP_ID = -1003951671147 # Поменяй на ID своей группы модерации!

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
dp  = Dispatcher()
db_pool = None

# ================= БАЗА ДАННЫХ =================
@asynccontextmanager
async def db():
    async with db_pool.acquire() as conn:
        yield conn

async def init_db():
    global db_pool
    if not DATABASE_URL:
        log.error("❌ DATABASE_URL не найден в переменных окружения!")
        return

    # Для Neon PostgreSQL требуется SSL
    db_pool = await asyncpg.create_pool(DATABASE_URL, ssl='require')

    async with db() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS all_cards (
                id          SERIAL PRIMARY KEY,
                name        TEXT,
                rating      INTEGER,
                position    TEXT,
                rarity      TEXT,
                rarity_type TEXT,
                club        TEXT,
                photo_id    TEXT
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id        BIGINT PRIMARY KEY,
                username       TEXT,
                balance        BIGINT DEFAULT 1000,
                last_open      TEXT,
                last_guess     TEXT,
                last_penalty   TEXT,
                vip_until      TEXT,
                referred_by    BIGINT,
                ref_bonus_paid INTEGER DEFAULT 0,
                is_banned      INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS user_cards (
                id      SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                card_id INTEGER NOT NULL REFERENCES all_cards(id)  ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS active_teams (
                user_id BIGINT PRIMARY KEY,
                gk_id   INTEGER,
                def_ids TEXT,
                mid_ids TEXT,
                fwd_ids TEXT
            );

            CREATE TABLE IF NOT EXISTS stars_log (
                id            SERIAL PRIMARY KEY,
                user_id       BIGINT NOT NULL,
                amount        BIGINT NOT NULL,
                balance_after BIGINT,
                reason        TEXT,
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP::text
            );

            CREATE TABLE IF NOT EXISTS referral_checks (
                id            SERIAL PRIMARY KEY,
                ref_user_id   BIGINT NOT NULL UNIQUE,
                inviter_id    BIGINT NOT NULL,
                joined_at     TEXT NOT NULL,
                subscribed_at TEXT,
                bonus_paid_at TEXT,
                revoked       INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS user_notifications (
                user_id       BIGINT PRIMARY KEY,
                pack_notif    INTEGER DEFAULT 0,
                guess_notif   INTEGER DEFAULT 0,
                penalty_notif INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS allowed_groups (
                chat_id   BIGINT PRIMARY KEY,
                added_by  BIGINT,
                added_at  TEXT DEFAULT CURRENT_TIMESTAMP::text
            );

            CREATE TABLE IF NOT EXISTS bot_chats (
                chat_id   BIGINT PRIMARY KEY,
                status    TEXT DEFAULT 'member',
                added_at  TEXT DEFAULT CURRENT_TIMESTAMP::text
            );

            CREATE TABLE IF NOT EXISTS special_packs (
                id            SERIAL PRIMARY KEY,
                title         TEXT NOT NULL,
                description   TEXT,
                channel       TEXT NOT NULL,
                channel_url   TEXT NOT NULL,
                days_active   INTEGER NOT NULL,
                cooldown_sec  INTEGER NOT NULL,
                unique_chance INTEGER NOT NULL DEFAULT 15,
                expires_at    TEXT NOT NULL,
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP::text,
                is_active     INTEGER DEFAULT 1,
                banner_photo  TEXT
            );

            CREATE TABLE IF NOT EXISTS special_pack_cards (
                id        SERIAL PRIMARY KEY,
                pack_id   INTEGER NOT NULL REFERENCES special_packs(id) ON DELETE CASCADE,
                card_id   INTEGER NOT NULL REFERENCES all_cards(id)     ON DELETE CASCADE,
                is_unique INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS user_pack_opens (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                pack_id    INTEGER NOT NULL,
                opened_at  TEXT NOT NULL,
                UNIQUE(user_id, pack_id)
            );

            CREATE INDEX IF NOT EXISTS idx_all_cards_rating   ON all_cards (rating);
            CREATE INDEX IF NOT EXISTS idx_all_cards_rarity   ON all_cards (rarity_type);
            CREATE INDEX IF NOT EXISTS idx_user_cards_user_id ON user_cards (user_id);
            CREATE INDEX IF NOT EXISTS idx_users_balance      ON users (balance DESC);
            CREATE INDEX IF NOT EXISTS idx_stars_log_user     ON stars_log (user_id);
            CREATE INDEX IF NOT EXISTS idx_refchecks_inviter  ON referral_checks (inviter_id);
            CREATE INDEX IF NOT EXISTS idx_pack_opens_user    ON user_pack_opens (user_id, pack_id);
        """)
    log.info("✅ База данных PostgreSQL инициализирована!")


guess_sessions    = {}
bet_waitlist      = {}
processing_users  = set()
penalty_sessions  = {}
penalty_team_sel  = {}
penalty_bet_wait  = {}
card_edit_sessions      = {}
player_suggest_sessions = {}
pack_create_sessions    = {}

TIER_SQL = {
    "bronze":    "rating BETWEEN 50 AND 74",
    "gold":      "rating BETWEEN 75 AND 89",
    "brilliant": "rating BETWEEN 90 AND 94",
    "ivents":    "rating BETWEEN 95 AND 98",
    "legend":    "rating >= 99",
}
TIER_REWARDS = {
    "bronze":    (250, 500),
    "gold":      (750, 1250),
    "brilliant": (2000, 3000),
    "ivents":    (4500, 5500),
    "legend":    (9000, 11000),
}
GUESS_REWARD = {
    "bronze":    {"max": 500,   "hint1": 350,  "hint2": 200},
    "gold":      {"max": 1250,  "hint1": 900,  "hint2": 500},
    "brilliant": {"max": 2500,  "hint1": 1750, "hint2": 1000},
    "ivents":    {"max": 5000,  "hint1": 3500, "hint2": 2000},
    "legend":    {"max": 10000, "hint1": 7000, "hint2": 4000},
}

CORNERS     = ["ЛВ 🔝", "ЛН ↙️", "Ц 🎯", "ПН ↘️", "ПВ 🔝"]
CORNER_KEYS = ["lv", "ln", "c", "pn", "pv"]

PENALTY_COOLDOWN      = 10800
PENALTY_WIN_BONUS_PCT = 0.90
UNDERDOG_BONUS_PCT    = 0.40

def determine_tier(rating: int) -> str:
    if rating >= 99: return "legend"
    if rating >= 95: return "ivents"
    if rating >= 90: return "brilliant"
    if rating >= 75: return "gold"
    return "bronze"

def parse_dt(value) -> datetime | None:
    if not value: return None
    try: return datetime.fromisoformat(value)
    except Exception: return None

def get_pack_remaining(last_open: str | None, vip: bool) -> int:
    delay = 3600 if vip else 7200
    dt = parse_dt(last_open)
    if not dt: return 0
    return max(0, int(delay - (datetime.now() - dt).total_seconds()))

def get_guess_remaining(last_guess: str | None) -> int:
    dt = parse_dt(last_guess)
    if not dt: return 0
    return max(0, int(18000 - (datetime.now() - dt).total_seconds()))

def get_penalty_remaining(last_penalty: str | None) -> int:
    dt = parse_dt(last_penalty)
    if not dt: return 0
    return max(0, int(PENALTY_COOLDOWN - (datetime.now() - dt).total_seconds()))

def avg_rating(cards: list[dict]) -> float:
    if not cards: return 70.0
    return sum(c["rating"] for c in cards) / len(cards)

def bot_saves_corners(keys: list[str]) -> list[str]:
    k = keys.copy()
    random.shuffle(k)
    return k[:2]

def _penalty_score_line(s: dict) -> str:
    r = max(s["round"], 1)
    u = "⚽" * s["user_score"] + "·" * (r - s["user_score"])
    b = "⚽" * s["bot_score"]  + "·" * (r - s["bot_score"])
    return f"👤 {u}  {s['user_score']}:{s['bot_score']}  {b} 🤖"

def _team_display(cards: list[dict]) -> str:
    return "\n".join(f"  {i+1}. <b>{c['name']}</b> — {c['rating']} ⭐" for i, c in enumerate(cards))

def _main_menu_markup():
    menu = ReplyKeyboardBuilder()
    menu.button(text="Получить Карту 🏆")
    menu.button(text="Мини-Игры ⚽")
    menu.button(text="Магазин 🛒")
    menu.button(text="Спец. Паки 🎁")
    menu.button(text="Профиль 👤")
    menu.button(text="Рефералка 👥")
    menu.button(text="ТОП-10 📊")
    menu.button(text="Поиск игрока 🔍")
    return menu.adjust(2).as_markup(resize_keyboard=True)

async def log_stars(conn, user_id: int, amount: int, reason: str):
    row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
    balance_after = row["balance"] if row else None
    await conn.execute(
        "INSERT INTO stars_log (user_id, amount, balance_after, reason) VALUES ($1, $2, $3, $4)",
        user_id, amount, balance_after, reason
    )

async def ensure_user(conn, user_id: int, username: str | None = None) -> bool:
    existing = await conn.fetchrow("SELECT user_id FROM users WHERE user_id = $1", user_id)
    if existing is None:
        await conn.execute("INSERT INTO users (user_id, username) VALUES ($1, $2)", user_id, username)
        return True
    if username:
        await conn.execute("UPDATE users SET username = $1 WHERE user_id = $2", username, user_id)
    return False

async def get_user_balance(conn, user_id: int) -> int | None:
    row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
    return row["balance"] if row else None

async def is_vip(user_id: int) -> bool:
    async with db() as conn:
        row = await conn.fetchrow("SELECT vip_until FROM users WHERE user_id = $1", user_id)
    if not row: return False
    dt = parse_dt(row["vip_until"])
    return bool(dt and dt > datetime.now())

async def get_subs_status(user_id: int) -> list[tuple[str, str]]:
    missing = []
    for tag, link in REQUIRED_CHANNELS.items():
        try:
            status = await bot.get_chat_member(tag, user_id)
            if status.status not in ("member", "administrator", "creator"):
                missing.append((tag, link))
        except Exception:
            missing.append((tag, link))
    return missing

async def check_user_subscribed(user_id: int) -> bool:
    return len(await get_subs_status(user_id)) == 0

def build_guess_keyboard(session: dict) -> types.InlineKeyboardMarkup:
    hints_used = session["hints_used"]
    kb = InlineKeyboardBuilder()
    for opt in session["options"]:
        kb.button(text=opt["name"], callback_data=f"guess_pick_{opt['id']}")
    kb.adjust(2)
    action_row = InlineKeyboardBuilder()
    if hints_used == 0:
        action_row.button(text="💡 Подсказка: рейтинг (−30%)", callback_data="guess_hint_1")
    elif hints_used == 1:
        action_row.button(text="💡 Подсказка: первая буква (−40%)", callback_data="guess_hint_2")
    action_row.button(text="🏳️ Сдаться", callback_data="guess_give_up")
    kb.attach(action_row)
    return kb.as_markup()

def build_guess_text(session: dict) -> str:
    hints_used = session["hints_used"]
    target     = session["target_info"]
    reward_key = ["max", "hint1", "hint2"][min(hints_used, 2)]
    reward     = GUESS_REWARD.get(session["card_rarity"], GUESS_REWARD["bronze"])[reward_key]
    lines = [
        "🧩 <b>Угадай игрока!</b>",
        f"🛡 Клуб: <b>{target['club']}</b>",
        f"📍 Позиция: <b>{target['position']}</b>",
    ]
    if hints_used >= 1: lines.append(f"📊 Рейтинг: <b>{target['rating']}</b>")
    if hints_used >= 2: lines.append(f"🔤 Имя начинается на: <b>{target['name'][0].upper()}...</b>")
    lines.append(f"\n💰 Награда за правильный ответ: <b>{reward:,} ⭐</b>")
    if hints_used == 0: lines.append("ℹ️ Можно взять подсказку, но награда уменьшится")
    elif hints_used == 1: lines.append("ℹ️ Осталась ещё одна подсказка")
    else: lines.append("ℹ️ Подсказок больше нет")
    return "\n".join(lines)

async def confirm_referral_subscription(ref_uid: int, inviter_id: int):
    async with db() as conn:
        ref_record = await conn.fetchrow(
            "SELECT id FROM referral_checks "
            "WHERE ref_user_id = $1 AND subscribed_at IS NULL AND revoked = 0",
            ref_uid
        )
        if not ref_record: return
        now = datetime.now()
        await conn.execute(
            "UPDATE referral_checks SET subscribed_at = $1 WHERE id = $2",
            now.isoformat(), ref_record["id"]
        )

    deadline_date = (now + timedelta(days=REF_CHECK_DAYS)).strftime("%d.%m.%Y")
    try:
        await bot.send_message(
            inviter_id,
            f"✅ <b>Реферал подтвердил подписки!</b>\n\n"
            f"⏳ <b>Бонус {REF_BONUS_AMOUNT:,} ⭐</b> будет начислен <b>{deadline_date}</b> "
            f"(через {REF_CHECK_DAYS} дней), если реферал останется подписан."
        )
    except Exception: pass
    try:
        await bot.send_message(
            ref_uid,
            f"✅ <b>Подписки подтверждены!</b>\n\n"
            f"⚠️ Если ты отпишешься от какого-либо канала в течение "
            f"<b>{REF_CHECK_DAYS} дней</b> (до {deadline_date}), "
            f"твоему другу <b>сгорит бонус {REF_BONUS_AMOUNT:,} ⭐</b>!\n\n"
            f"⚽️ <b>FTCL Cards приветствует тебя!</b>",
            reply_markup=_main_menu_markup()
        )
    except Exception: pass

async def _run_referral_checks():
    now      = datetime.now()
    deadline = (now - timedelta(days=REF_CHECK_DAYS)).isoformat()

    async with db() as conn:
        pending = await conn.fetch(
            "SELECT rc.id, rc.ref_user_id, rc.inviter_id, "
            "       u.username AS ref_username "
            "FROM referral_checks rc "
            "JOIN users u ON u.user_id = rc.ref_user_id "
            "WHERE rc.bonus_paid_at IS NULL "
            "  AND rc.revoked = 0 "
            "  AND rc.subscribed_at IS NOT NULL "
            "  AND rc.subscribed_at <= $1",
            deadline
        )

    for row in pending:
        rec_id    = row["id"]
        ref_uid   = row["ref_user_id"]
        inv_uid   = row["inviter_id"]
        ref_uname = row["ref_username"] or str(ref_uid)

        try: still_subbed = await check_user_subscribed(ref_uid)
        except Exception: continue

        if still_subbed:
            async with db() as conn:
                if not await conn.fetchrow("SELECT id FROM referral_checks WHERE id = $1 AND bonus_paid_at IS NULL", rec_id):
                    continue
                await conn.execute("UPDATE referral_checks SET bonus_paid_at = $1 WHERE id = $2", now.isoformat(), rec_id)
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", REF_BONUS_AMOUNT, inv_uid)
                await log_stars(conn, inv_uid, REF_BONUS_AMOUNT, f"Реферал подтверждён: @{ref_uname}")
            try:
                await bot.send_message(inv_uid, f"🎉 <b>Реферальный бонус начислен!</b>\n💰 Начислено: <b>+{REF_BONUS_AMOUNT:,} ⭐</b>")
            except Exception: pass
        else:
            async with db() as conn:
                await conn.execute("UPDATE referral_checks SET revoked = 1 WHERE id = $1", rec_id)
            try:
                await bot.send_message(inv_uid, f"🔥 <b>Реферальный бонус сгорел!</b>\nПользователь @{ref_uname} отписался от каналов.")
            except Exception: pass

async def _run_notification_checks():
    now = datetime.now()
    async with db() as conn:
        users = await conn.fetch(
            "SELECT u.user_id, u.last_open, u.last_guess, u.last_penalty, u.vip_until, "
            "       COALESCE(n.pack_notif,0) AS pack_notif, "
            "       COALESCE(n.guess_notif,0) AS guess_notif, "
            "       COALESCE(n.penalty_notif,0) AS penalty_notif "
            "FROM users u LEFT JOIN user_notifications n ON n.user_id = u.user_id "
            "WHERE u.is_banned = 0"
        )
    for u in users:
        uid = u["user_id"]
        vip = bool(u["vip_until"] and parse_dt(u["vip_until"]) and parse_dt(u["vip_until"]) > now)
        send_pack    = get_pack_remaining(u["last_open"], vip) == 0 and u["last_open"] and not u["pack_notif"]
        send_guess   = get_guess_remaining(u["last_guess"]) == 0 and u["last_guess"] and not u["guess_notif"]
        send_penalty = get_penalty_remaining(u["last_penalty"]) == 0 and u["last_penalty"] and not u["penalty_notif"]
        msgs = []
        if send_pack:    msgs.append("🏆 <b>Пак готов!</b> Открой новую карту — \"Получить Карту 🏆\"")
        if send_guess:   msgs.append("🧩 <b>Угадайка доступна!</b> Сыграй и выиграй ⭐ — \"Мини-Игры ⚽\"")
        if send_penalty: msgs.append("⚽ <b>Серия пенальти доступна!</b> Сыграй и выиграй ⭐ — \"Мини-Игры ⚽\"")
        if not msgs: continue
        try: await bot.send_message(uid, "\n\n".join(msgs))
        except Exception: pass
        async with db() as conn:
            await conn.execute(
                "INSERT INTO user_notifications (user_id, pack_notif, guess_notif, penalty_notif) VALUES ($1,$2,$3,$4) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "  pack_notif    = CASE WHEN EXCLUDED.pack_notif    = 1 THEN 1 ELSE user_notifications.pack_notif    END, "
                "  guess_notif   = CASE WHEN EXCLUDED.guess_notif   = 1 THEN 1 ELSE user_notifications.guess_notif   END, "
                "  penalty_notif = CASE WHEN EXCLUDED.penalty_notif = 1 THEN 1 ELSE user_notifications.penalty_notif END",
                uid, 1 if send_pack else 0, 1 if send_guess else 0, 1 if send_penalty else 0
            )

async def _reset_notif(uid: int, flag: str):
    async with db() as conn:
        await conn.execute(
            f"INSERT INTO user_notifications (user_id) VALUES ($1) "
            f"ON CONFLICT(user_id) DO UPDATE SET {flag} = 0", uid
        )

async def notification_loop():
    await asyncio.sleep(15)
    while True:
        try: await _run_notification_checks()
        except Exception as e: log.error("notif loop err: %s", e)
        await asyncio.sleep(60)

async def referral_checker_loop():
    await asyncio.sleep(10)
    while True:
        try: await _run_referral_checks()
        except Exception as e: log.error("ref loop err: %s", e)
        await asyncio.sleep(1800)

class MaintenanceBanMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[types.TelegramObject, dict[str, Any]], Awaitable[Any]], event: types.TelegramObject, data: dict[str, Any]) -> Any:
        if isinstance(event, types.Message):
            user_id     = event.from_user.id if event.from_user else None
            is_callback = False
            if event.text and event.text.startswith("/start"): return await handler(event, data)
        elif isinstance(event, types.CallbackQuery):
            user_id     = event.from_user.id if event.from_user else None
            is_callback = True
        else: return await handler(event, data)

        if user_id is None or user_id == ADMIN_ID: return await handler(event, data)

        if MAINTENANCE_MODE:
            if is_callback and event.data and event.data.startswith("confirm_ref_"): return await handler(event, data)
            if is_callback:
                try: await event.answer("🔧 Бот на техобслуживании. Попробуй позже.", show_alert=True)
                except TelegramBadRequest: pass
                return
            else:
                await event.answer(MAINTENANCE_MESSAGE)
                return

        async with db() as conn:
            row = await conn.fetchrow("SELECT is_banned FROM users WHERE user_id = $1", user_id)
        if row and row["is_banned"]:
            if is_callback:
                try: await event.answer("🚫 Ваш аккаунт заблокирован.", show_alert=True)
                except TelegramBadRequest: pass
            else: await event.answer("🚫 Ваш аккаунт заблокирован.")
            return

        return await handler(event, data)

@dp.my_chat_member()
async def on_my_chat_member(update: types.ChatMemberUpdated):
    chat = update.chat
    if chat.type not in ("group", "supergroup"): return
    new_status = update.new_chat_member.status
    old_status = update.old_chat_member.status

    async with db() as conn:
        await conn.execute(
            "INSERT INTO bot_chats (chat_id, status) VALUES ($1, $2) "
            "ON CONFLICT(chat_id) DO UPDATE SET status = $3",
            chat.id, new_status, new_status
        )
        if new_status == "member" and old_status in ("left", "kicked"):
            allowed = await conn.fetchrow("SELECT chat_id FROM allowed_groups WHERE chat_id = $1", chat.id)
            if not allowed:
                try: await bot.send_message(chat.id, "🚫 Бот покидает неразрешенную группу.")
                except Exception: pass
                try: await bot.leave_chat(chat.id)
                except Exception: pass
                await conn.execute("UPDATE bot_chats SET status='left' WHERE chat_id=$1", chat.id)

async def clean_groups():
    async with db() as conn:
        allowed_rows = await conn.fetch("SELECT chat_id FROM allowed_groups")
        allowed = {r["chat_id"] for r in allowed_rows}
        chats_rows = await conn.fetch("SELECT chat_id FROM bot_chats WHERE status='member'")
        chats = [r["chat_id"] for r in chats_rows]
    for chat_id in chats:
        if chat_id in allowed: continue
        try: await bot.leave_chat(chat_id)
        except Exception: pass
        async with db() as conn:
            await conn.execute("UPDATE bot_chats SET status='left' WHERE chat_id=$1", chat_id)

@dp.message(Command("allowgroup"), F.from_user.id == ADMIN_ID)
async def cmd_allowgroup(message: types.Message, command: CommandObject):
    if not command.args:
        async with db() as conn:
            rows = await conn.fetch("SELECT chat_id FROM allowed_groups")
        if not rows: return await message.answer("Разрешённых групп нет.")
        return await message.answer("Разрешённые группы:\n" + "\n".join(str(r["chat_id"]) for r in rows))
    try: chat_id = int(command.args.strip())
    except ValueError: return await message.answer("Неверный ID.")
    async with db() as conn:
        await conn.execute("INSERT INTO allowed_groups (chat_id, added_by) VALUES ($1, $2) ON CONFLICT (chat_id) DO NOTHING", chat_id, message.from_user.id)
    await message.answer(f"✅ Группа {chat_id} добавлена в белый список.")

@dp.message(Command("disallowgroup"), F.from_user.id == ADMIN_ID)
async def cmd_disallowgroup(message: types.Message, command: CommandObject):
    if not command.args: return await message.answer("Использование: /disallowgroup <chat_id>")
    try: chat_id = int(command.args.strip())
    except ValueError: return await message.answer("Неверный ID.")
    async with db() as conn:
        await conn.execute("DELETE FROM allowed_groups WHERE chat_id = $1", chat_id)
    await message.answer(f"❌ Группа {chat_id} удалена из списка.")

@dp.message(Command("allowthisgroup"), F.chat.type.in_({"group", "supergroup"}), F.from_user.id == ADMIN_ID)
async def cmd_allowthisgroup(message: types.Message):
    async with db() as conn:
        await conn.execute("INSERT INTO allowed_groups (chat_id, added_by) VALUES ($1, $2) ON CONFLICT (chat_id) DO NOTHING", message.chat.id, message.from_user.id)
    await message.answer("✅ Эта группа теперь разрешена для бота.")

@dp.message(Command("disallowthisgroup"), F.chat.type.in_({"group", "supergroup"}), F.from_user.id == ADMIN_ID)
async def cmd_disallowthisgroup(message: types.Message):
    async with db() as conn:
        await conn.execute("DELETE FROM allowed_groups WHERE chat_id = $1", message.chat.id)
    await message.answer("❌ Группа удалена.")

async def _claim_card_flow(message_or_call):
    is_call = isinstance(message_or_call, types.CallbackQuery)
    uid = message_or_call.from_user.id

    async def _send(text, **kwargs):
        if is_call: return await message_or_call.message.answer(text, **kwargs)
        return await message_or_call.answer(text, **kwargs)

    async def _send_photo(photo, **kwargs):
        if is_call: return await message_or_call.message.answer_photo(photo, **kwargs)
        return await message_or_call.answer_photo(photo, **kwargs)

    if uid in processing_users:
        return await _send("⏳ Подожди, твой запрос обрабатывается...")
    processing_users.add(uid)
    try:
        unsubbed = await get_subs_status(uid)
        if unsubbed:
            kb = InlineKeyboardBuilder()
            for i, (tag, link) in enumerate(unsubbed, 1):
                kb.button(text=f"Канал {i} 📢", url=link)
            kb.button(text="Я подписался ✅", callback_data="check_subs")
            return await _send("❌ <b>Подпишись на каналы!</b>", reply_markup=kb.adjust(1).as_markup())

        vip_status = await is_vip(uid)
        async with db() as conn:
            await ensure_user(conn, uid, message_or_call.from_user.username)
            user_row = await conn.fetchrow("SELECT last_open FROM users WHERE user_id = $1", uid)

        remaining = get_pack_remaining(user_row["last_open"] if user_row else None, vip_status)
        if remaining > 0:
            h, m = remaining // 3600, (remaining % 3600) // 60
            return await _send(f"⌛ <b>До следующего пака: {h}ч. {m}м.</b>")

        pool_tiers  = ["bronze", "gold", "brilliant", "ivents", "legend"]
        target_tier = random.choices(pool_tiers, weights=[70, 20, 5, 3, 2], k=1)[0]

        async with db() as conn:
            card = await conn.fetchrow(f"SELECT * FROM all_cards WHERE {TIER_SQL[target_tier]} ORDER BY RANDOM() LIMIT 1")
            if not card:
                card = await conn.fetchrow("SELECT * FROM all_cards ORDER BY RANDOM() LIMIT 1")

        if not card: return await _send("❌ Ошибка: карты не найдены.")

        rarity_key = card["rarity_type"] if card["rarity_type"] in TIER_REWARDS else determine_tier(card["rating"])
        lo, hi = TIER_REWARDS.get(rarity_key, (250, 500))
        bonus  = random.randint(lo, hi)

        async with db() as conn:
            now_iso = datetime.now().isoformat()
            await conn.execute("UPDATE users SET balance = balance + $1, last_open = $2 WHERE user_id = $3", bonus, now_iso, uid)
            await conn.execute("INSERT INTO user_cards (user_id, card_id) VALUES ($1, $2)", uid, card["id"])
            await log_stars(conn, uid, bonus, f"Пак: {card['name']} ({rarity_key})")
        await _reset_notif(uid, 'pack_notif')

        loader = await _send("Открываем пак... 💼")
        await asyncio.sleep(2)
        try: await loader.delete()
        except Exception: pass
        await _send_photo(
            card["photo_id"],
            caption=(
                f"👤 <b>{card['name']}</b>\n"
                f"📊 Рейтинг: {card['rating']}\n"
                f"🏷 Тип: {card['rarity']}\n"
                f"💰 Награда: +{bonus:,} ⭐"
            )
        )
    finally:
        processing_users.discard(uid)

@dp.message(Command("ftclcard"))
async def cmd_ftclcard(message: types.Message, command: CommandObject):
    if command.args:
        query = f"%{command.args.strip()}%"
        async with db() as conn:
            cards = await conn.fetch(
                "SELECT id, name, rating, club, position, rarity, photo_id FROM all_cards "
                "WHERE name ILIKE $1 ORDER BY rating DESC LIMIT 10", query
            )
        if not cards: return await message.answer(f"Игроки «{command.args.strip()}» не найдены.")
        if len(cards) == 1:
            card = cards[0]
            await message.answer_photo(card['photo_id'], caption=f"🃏 <b>{card['name']}</b>\n📊 Рейтинг: {card['rating']}\n🛡 Клуб: {card['club']}\n📍 Позиция: {card['position']}\n🏷 Тип: {card['rarity']}")
        else:
            kb = InlineKeyboardBuilder()
            for c in cards: kb.button(text=f"{c['name']} ({c['rating']})", callback_data=f"ftclcard_{c['id']}")
            await message.answer("🔍 Выберите:", reply_markup=kb.adjust(1).as_markup())
    else:
        await _claim_card_flow(message)

@dp.callback_query(F.data.startswith("ftclcard_"))
async def ftclcard_callback(call: types.CallbackQuery):
    card_id = int(call.data.split("_")[1])
    async with db() as conn:
        card = await conn.fetchrow("SELECT * FROM all_cards WHERE id=$1", card_id)
    if not card: return await call.answer("Не найдено", show_alert=True)
    await call.message.delete()
    await call.message.answer_photo(card['photo_id'], caption=f"🃏 <b>{card['name']}</b>\n📊 Рейтинг: {card['rating']}\n🛡 Клуб: {card['club']}\n📍 Позиция: {card['position']}\n🏷 Тип: {card['rarity']}")
    await call.answer()

@dp.message(Command("givevip"), F.from_user.id == ADMIN_ID)
async def cmd_givevip(message: types.Message, command: CommandObject):
    if not command.args: return await message.answer("Использование: /givevip @username [дней=30]")
    parts = command.args.split()
    username = parts[0].lstrip("@")
    days = int(parts[1]) if len(parts) > 1 else 30
    async with db() as conn:
        user = await conn.fetchrow("SELECT user_id, username FROM users WHERE username=$1", username)
        if not user: return await message.answer(f"@{username} не найден.")
        expiry = (datetime.now() + timedelta(days=days)).isoformat()
        await conn.execute("UPDATE users SET vip_until=$1 WHERE user_id=$2", expiry, user['user_id'])
    await message.answer(f"💎 VIP выдан @{username} на {days} дней.")
    try: await bot.send_message(user['user_id'], f"🎉 Администратор выдал вам VIP на {days} дней!")
    except Exception: pass

@dp.message(Command("maintenance"), F.from_user.id == ADMIN_ID)
async def toggle_maintenance(message: types.Message):
    global MAINTENANCE_MODE
    MAINTENANCE_MODE = not MAINTENANCE_MODE
    await message.answer(f"🔧 <b>Режим техобслуживания:</b> {'✅ ВКЛ' if MAINTENANCE_MODE else '❌ ВЫКЛ'}")

@dp.message(Command("add_player"), F.from_user.id == ADMIN_ID)
async def handle_add_player(message: types.Message, command: CommandObject):
    if not message.photo or not command.args:
        return await message.answer("Формат: фото + /add_player Имя | Рейтинг | Клуб | Позиция")
    try:
        name, rate, club, pos = [p.strip() for p in command.args.split("|")]
        rating, tier = int(rate), determine_tier(int(rate))
        async with db() as conn:
            await conn.execute(
                "INSERT INTO all_cards (name, rating, club, photo_id, position, rarity, rarity_type) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                name, rating, club, message.photo[-1].file_id, pos, tier, tier
            )
        await message.answer(f"✅ Карта <b>{name}</b> сохранена.")
    except Exception as err: await message.answer(f"❌ Ошибка: {err}")

@dp.message(Command("starslog"), F.from_user.id == ADMIN_ID)
async def admin_stars_log(message: types.Message, command: CommandObject):
    if not command.args: return await message.answer("ℹ️ Использование: /starslog @username [кол-во]")
    args = command.args.strip().split()
    raw_username = args[0].lstrip("@")
    limit = min(int(args[1]), 50) if len(args) > 1 and args[1].isdigit() else 15
    async with db() as conn:
        user_row = await conn.fetchrow("SELECT user_id, balance FROM users WHERE username = $1", raw_username)
        if not user_row: return await message.answer(f"❌ @{raw_username} не найден.")
        uid, current_balance = user_row["user_id"], user_row["balance"]
        logs = await conn.fetch("SELECT id, amount, balance_after, reason, created_at FROM stars_log WHERE user_id = $1 ORDER BY id DESC LIMIT $2", uid, limit)
        first = await conn.fetchrow("SELECT amount, balance_after FROM stars_log WHERE user_id = $1 ORDER BY id ASC LIMIT 1", uid)
        sum_row = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM stars_log WHERE user_id = $1", uid)
    total_logged = int(sum_row) if sum_row else 0
    initial_balance = (first["balance_after"] - first["amount"]) if first and first["balance_after"] is not None else 1000
    expected_balance = initial_balance + total_logged
    header = [f"📋 Лог: @{raw_username}", f"🏦 Нач: {initial_balance:,} | 💰 Тек: {current_balance:,}", f"📊 Сумма: {total_logged:+,} | 🧮 Ожидаемый: {expected_balance:,}"]
    if not logs: return await message.answer("\n".join(header) + "\n\n<i>Записей нет.</i>")
    entry_lines = [f"{'💚' if r['amount']>=0 else '🔴'} {'+' if r['amount']>=0 else ''}{r['amount']:,} ⭐\n   📌 {r['reason']}" for r in logs]
    await message.answer("\n".join(header) + "\n\n" + "\n\n".join(entry_lines)[:3500])

@dp.message(Command("refcheck"), F.from_user.id == ADMIN_ID)
async def admin_refcheck(message: types.Message, command: CommandObject):
    if not command.args: return await message.answer("Использование: /refcheck @username")
    raw = command.args.strip().lstrip("@")
    async with db() as conn:
        u = await conn.fetchrow("SELECT user_id FROM users WHERE username = $1", raw)
        if not u: return await message.answer("❌ Не найден.")
        rows = await conn.fetch("SELECT rc.id, u.username AS ref_uname, rc.joined_at, rc.subscribed_at, rc.bonus_paid_at, rc.revoked FROM referral_checks rc JOIN users u ON u.user_id = rc.ref_user_id WHERE rc.inviter_id = $1 ORDER BY rc.id DESC LIMIT 20", u["user_id"])
    if not rows: return await message.answer("Записей нет.")
    lines = [f"📋 Рефералы @{raw}:"]
    for r in rows:
        st = "🔥 Сгорел" if r["revoked"] else "✅ Выплачен" if r["bonus_paid_at"] else "⏳ Ожидание" if r["subscribed_at"] else "⚠️ Ждёт подписки"
        lines.append(f"• @{r['ref_uname'] or r['id']} | {st}")
    await message.answer("\n".join(lines))

@dp.message(Command("find_card"), F.from_user.id == ADMIN_ID)
async def find_card(message: types.Message, command: CommandObject):
    if not command.args: return await message.answer("ℹ️ Использование: /find_card Месси")
    query = f"%{command.args.strip()}%"
    async with db() as conn:
        cards = await conn.fetch("SELECT id, name, rating, club, position, rarity FROM all_cards WHERE name ILIKE $1 ORDER BY rating DESC LIMIT 20", query)
    if not cards: return await message.answer("❌ Карты не найдены.")
    if len(cards) == 1: return await message.answer(_card_info_text(cards[0]), reply_markup=_card_manage_kb(cards[0]["id"]))
    kb = InlineKeyboardBuilder()
    for c in cards: kb.button(text=f"{c['name']} | {c['rating']}", callback_data=f"cadmin_select_{c['id']}")
    await message.answer(f"🔍 Найдено {len(cards)}:", reply_markup=kb.adjust(1).as_markup())

def _card_info_text(card):
    return f"🃏 <b>{card['name']}</b>\n📊 Рейтинг: {card['rating']}\n🛡 Клуб: {card['club']}\n📍 Позиция: {card['position']}\n🏷 Тип: {card['rarity']}\n🆔 ID: <code>{card['id']}</code>"

def _card_manage_kb(card_id):
    kb = InlineKeyboardBuilder()
    for txt, cb in [("✏️ Имя", "name"), ("📊 Рейтинг", "rating"), ("🛡 Клуб", "club"), ("📍 Позиция", "position"), ("🖼 Фото", "photo")]:
        kb.button(text=txt, callback_data=f"cadmin_edit_{cb}_{card_id}")
    kb.button(text="🎁 Выдать", callback_data=f"cadmin_give_{card_id}")
    kb.button(text="🗑 Удалить", callback_data=f"cadmin_delete_{card_id}")
    return kb.adjust(2).as_markup()

@dp.callback_query(F.data.startswith("cadmin_select_"), F.from_user.id == ADMIN_ID)
async def cadmin_select(call: types.CallbackQuery):
    card_id = int(call.data.split("_")[2])
    async with db() as conn:
        card = await conn.fetchrow("SELECT * FROM all_cards WHERE id = $1", card_id)
    if not card: return await call.answer("❌ Не найдено", show_alert=True)
    await call.message.edit_text(_card_info_text(card), reply_markup=_card_manage_kb(card_id))

@dp.callback_query(F.data.startswith("cadmin_delete_"), F.from_user.id == ADMIN_ID)
async def cadmin_delete(call: types.CallbackQuery):
    card_id = int(call.data.split("_")[2])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"cadmin_confirm_delete_{card_id}")
    kb.button(text="❌ Отмена", callback_data=f"cadmin_select_{card_id}")
    await call.message.edit_text(f"⚠️ Удалить карту ID {card_id}?", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("cadmin_confirm_delete_"), F.from_user.id == ADMIN_ID)
async def cadmin_confirm_delete(call: types.CallbackQuery):
    card_id = int(call.data.split("_")[3])
    async with db() as conn: await conn.execute("DELETE FROM all_cards WHERE id = $1", card_id)
    await call.message.edit_text(f"🗑 Карта удалена.")

@dp.callback_query(F.data.startswith("cadmin_give_"), F.from_user.id == ADMIN_ID)
async def cadmin_give_prompt(call: types.CallbackQuery):
    card_id = int(call.data.split("_")[2])
    card_edit_sessions[call.from_user.id] = {"card_id": card_id, "step": "give_username"}
    await call.message.edit_text(f"🎁 Введи @username игрока для выдачи:")

@dp.callback_query(F.data.startswith("cadmin_edit_"), F.from_user.id == ADMIN_ID)
async def cadmin_edit_prompt(call: types.CallbackQuery):
    field, card_id = call.data.split("_")[2], int(call.data.split("_")[3])
    prompts = {"name": "Новое имя:", "rating": "Рейтинг (50-99):", "club": "Клуб:", "position": "Позиция:", "photo": "Новое фото:"}
    card_edit_sessions[call.from_user.id] = {"card_id": card_id, "step": f"edit_{field}"}
    await call.message.edit_text(f"✏️ {prompts[field]}")

@dp.message(lambda m: m.from_user.id in card_edit_sessions and m.from_user.id == ADMIN_ID)
async def cadmin_handle_input(message: types.Message):
    uid, session = message.from_user.id, card_edit_sessions.get(message.from_user.id)
    card_id, step = session["card_id"], session["step"]
    if step == "give_username":
        raw = message.text.strip().lstrip("@")
        async with db() as conn:
            user_row = await conn.fetchrow("SELECT user_id FROM users WHERE username = $1", raw)
            if not user_row: return await message.answer(f"❌ @{raw} не найден.")
            await conn.execute("INSERT INTO user_cards (user_id, card_id) VALUES ($1, $2)", user_row["user_id"], card_id)
        card_edit_sessions.pop(uid, None)
        return await message.answer(f"✅ Карта выдана @{raw}!")
    
    if step == "edit_photo" and message.photo: new_val, db_field = message.photo[-1].file_id, "photo_id"
    elif step == "edit_name": new_val, db_field = message.text.strip(), "name"
    elif step == "edit_club": new_val, db_field = message.text.strip(), "club"
    elif step == "edit_position": new_val, db_field = message.text.strip(), "position"
    elif step == "edit_rating":
        rating = int(message.text.strip())
        new_rarity = determine_tier(rating)
        async with db() as conn:
            await conn.execute("UPDATE all_cards SET rating = $1, rarity = $2, rarity_type = $3 WHERE id = $4", rating, new_rarity, new_rarity, card_id)
        card_edit_sessions.pop(uid, None)
        return await message.answer(f"✅ Рейтинг → {rating}")
    else: return
    async with db() as conn: await conn.execute(f"UPDATE all_cards SET {db_field} = $1 WHERE id = $2", new_val, card_id)
    card_edit_sessions.pop(uid, None)
    await message.answer("✅ Обновлено!")

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    uid, params = message.from_user.id, message.text.split()
    referrer = int(params[1]) if len(params) > 1 and params[1].isdigit() else None
    async with db() as conn:
        await ensure_user(conn, uid, message.from_user.username)
        if referrer and referrer != uid:
            ref_exists = await conn.fetchrow("SELECT user_id FROM users WHERE user_id = $1", referrer)
            if ref_exists:
                await conn.execute("INSERT INTO referral_checks (ref_user_id, inviter_id, joined_at) VALUES ($1, $2, $3) ON CONFLICT (ref_user_id) DO NOTHING", uid, referrer, datetime.now().isoformat())
                await conn.execute("UPDATE users SET referred_by = $1 WHERE user_id = $2", referrer, uid)

    if referrer and referrer != uid:
        unsubbed = await get_subs_status(uid)
        if unsubbed:
            kb = InlineKeyboardBuilder()
            for i, (tag, link) in enumerate(unsubbed, 1): kb.button(text=f"Канал {i} 📢", url=link)
            kb.button(text="✅ Я подписался", callback_data=f"confirm_ref_{referrer}")
            return await message.answer("👋 <b>Подпишись на все каналы для рефералки!</b>", reply_markup=kb.adjust(1).as_markup())
        else:
            await confirm_referral_subscription(uid, referrer)
    await message.answer("⚽️ <b>FTCL Cards приветствует тебя!</b>", reply_markup=_main_menu_markup())

@dp.callback_query(F.data.startswith("confirm_ref_"))
async def handle_confirm_referral(call: types.CallbackQuery):
    uid, inviter_id = call.from_user.id, int(call.data.split("_")[2])
    if await check_user_subscribed(uid):
        await call.message.delete()
        await confirm_referral_subscription(uid, inviter_id)
    else:
        await call.answer("❌ Не все каналы!", show_alert=True)

@dp.message(F.text == "Получить Карту 🏆")
async def claim_card(message: types.Message): await _claim_card_flow(message)

@dp.message(F.text == "Мини-Игры ⚽")
async def games_menu(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🧩 Угадай игрока", callback_data="game_guess")
    kb.button(text="⚽ Серия пенальти", callback_data="game_penalty")
    await message.answer("🎮 <b>Доступные режимы:</b>", reply_markup=kb.adjust(1).as_markup())

@dp.message(F.text == "Магазин 🛒")
async def open_shop(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Bronze (1200⭐)", callback_data="buy_bronze")
    kb.button(text="📦 Gold (3500⭐)", callback_data="buy_gold")
    kb.button(text="📦 Brilliant (6700⭐)", callback_data="buy_brilliant")
    kb.button(text="💎 VIP 30d (15000⭐)", callback_data="buy_vip")
    await message.answer("🛒 <b>Магазин FTCL</b>", reply_markup=kb.adjust(1).as_markup())

@dp.callback_query(F.data.startswith("buy_"))
async def handle_purchase(call: types.CallbackQuery):
    uid, sku = call.from_user.id, call.data.split("_", 1)[1]
    if uid in processing_users: return await call.answer("⏳ Подождите...")
    processing_users.add(uid)
    price_list = {"bronze": 1200, "gold": 3500, "brilliant": 6700, "vip": 15000}
    price = price_list[sku]
    try:
        async with db() as conn:
            balance = await get_user_balance(conn, uid)
            if balance is None or balance < price: return await call.answer("❌ Недостаточно ⭐!", show_alert=True)
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", price, uid)
            await log_stars(conn, uid, -price, f"Покупка: {sku}")
            if sku == "vip":
                expiry = (datetime.now() + timedelta(days=30)).isoformat()
                await conn.execute("UPDATE users SET vip_until = $1 WHERE user_id = $2", expiry, uid)
                await call.message.answer("💎 VIP активирован на 30 дней!")
            else:
                card = await conn.fetchrow(f"SELECT * FROM all_cards WHERE {TIER_SQL[sku]} ORDER BY RANDOM() LIMIT 1")
                if not card: return await call.message.answer("❌ Пусто.")
                grant = random.randint(*TIER_REWARDS.get(determine_tier(card["rating"]), (250,500)))
                await conn.execute("INSERT INTO user_cards (user_id, card_id) VALUES ($1, $2)", uid, card["id"])
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", grant, uid)
                await log_stars(conn, uid, grant, f"Магазин: {card['name']}")
                await call.message.answer_photo(card["photo_id"], caption=f"📦 Игрок: <b>{card['name']}</b>\n💰 Бонус: +{grant} ⭐")
    finally: processing_users.discard(uid)
    await call.answer()

@dp.callback_query(F.data == "game_guess")
async def init_guess_game(call: types.CallbackQuery):
    uid = call.from_user.id
    async with db() as conn:
        row = await conn.fetchrow("SELECT last_guess FROM users WHERE user_id = $1", uid)
    if get_guess_remaining(row["last_guess"] if row else None) > 0:
        return await call.answer(f"⏳ Перезарядка!", show_alert=True)
    bet_waitlist[uid] = "guess"
    await call.message.edit_text("💰 Укажите размер ставки (лимит 30 000 ⭐):")

@dp.message(lambda msg: msg.from_user.id in bet_waitlist)
async def process_bet(message: types.Message):
    uid, amount = message.from_user.id, int(message.text) if message.text.isdigit() else 0
    if not (0 < amount <= 30000): return await message.answer("❌ От 1 до 30 000 ⭐!")
    bet_waitlist.pop(uid, None)
    async with db() as conn:
        if (await get_user_balance(conn, uid) or 0) < amount: return await message.answer("❌ Нет денег.")
        options = await conn.fetch("SELECT id, name, club, position, rating, rarity_type FROM all_cards ORDER BY RANDOM() LIMIT 4")
    if len(options) < 2: return await message.answer("❌ Мало карт.")
    target = dict(options[0])
    options_list = [dict(r) for r in options]
    random.shuffle(options_list)
    guess_sessions[uid] = {"bet": amount, "target_id": target["id"], "target_info": target, "options": options_list, "hints_used": 0, "card_rarity": determine_tier(target["rating"])}
    await message.answer(build_guess_text(guess_sessions[uid]), reply_markup=build_guess_keyboard(guess_sessions[uid]))

@dp.callback_query(F.data.in_({"guess_hint_1", "guess_hint_2"}))
async def guess_hints(call: types.CallbackQuery):
    uid, s = call.from_user.id, guess_sessions.get(call.from_user.id)
    if not s: return await call.answer("Нет игры", show_alert=True)
    want_hint = 1 if "1" in call.data else 2
    if want_hint == 2 and s["hints_used"] < 1: return await call.answer("Сначала 1-ю", show_alert=True)
    s["hints_used"] = want_hint
    await call.message.edit_text(build_guess_text(s), reply_markup=build_guess_keyboard(s))

@dp.callback_query(F.data == "guess_give_up")
async def guess_give_up(call: types.CallbackQuery):
    uid, s = call.from_user.id, guess_sessions.pop(call.from_user.id, None)
    if not s: return await call.answer("Нет игры", show_alert=True)
    async with db() as conn:
        loss = min(s["bet"], await get_user_balance(conn, uid) or 0)
        await conn.execute("UPDATE users SET balance = GREATEST(balance - $1, 0), last_guess = $2 WHERE user_id = $3", loss, datetime.now().isoformat(), uid)
        await log_stars(conn, uid, -loss, "Сдался")
    await call.message.edit_text(f"🏳️ Сдался! Это {s['target_info']['name']}. Потеряно {loss} ⭐")

@dp.callback_query(F.data.startswith("guess_pick_"))
async def guess_pick(call: types.CallbackQuery):
    uid, pick = call.from_user.id, int(call.data.split("_")[2])
    s = guess_sessions.pop(uid, None)
    if not s: return await call.answer("Уже всё", show_alert=True)
    correct = (pick == s["target_id"])
    rew = GUESS_REWARD.get(s["card_rarity"], GUESS_REWARD["bronze"])[["max", "hint1", "hint2"][s["hints_used"]]]
    async with db() as conn:
        if correct:
            await conn.execute("UPDATE users SET balance = balance + $1, last_guess = $2 WHERE user_id = $3", rew, datetime.now().isoformat(), uid)
            await log_stars(conn, uid, rew, "Победа в угадайке")
            await call.message.edit_text(f"✅ <b>ВЕРНО!</b> Это {s['target_info']['name']}!\n💰 Выигрыш: {rew} ⭐")
        else:
            loss = min(s["bet"], await get_user_balance(conn, uid) or 0)
            await conn.execute("UPDATE users SET balance = GREATEST(balance - $1, 0), last_guess = $2 WHERE user_id = $3", loss, datetime.now().isoformat(), uid)
            await log_stars(conn, uid, -loss, "Проигрыш в угадайке")
            await call.message.edit_text(f"❌ <b>ОШИБКА!</b> Это {s['target_info']['name']}!\n💸 Потеряно: {loss} ⭐")

@dp.callback_query(F.data == "game_penalty")
async def init_penalty_game(call: types.CallbackQuery):
    uid = call.from_user.id
    async with db() as conn:
        row = await conn.fetchrow("SELECT last_penalty FROM users WHERE user_id = $1", uid)
        total = await conn.fetchval("SELECT COUNT(*) FROM user_cards WHERE user_id = $1", uid)
    if get_penalty_remaining(row["last_penalty"] if row else None) > 0: return await call.answer("⏳ Перезарядка!", show_alert=True)
    if total < 5: return await call.answer("❌ Нужно 5 карт!", show_alert=True)
    penalty_bet_wait[uid] = True
    await call.message.edit_text("⚽ Введи ставку на пенальти (до 30000):")

@dp.message(lambda m: m.from_user.id in penalty_bet_wait)
async def penalty_bet_input(message: types.Message):
    uid, amount = message.from_user.id, int(message.text.strip()) if message.text.isdigit() else 0
    if not (0 < amount <= 30000): return await message.answer("❌ Ставка 1-30000!")
    async with db() as conn:
        if (await get_user_balance(conn, uid) or 0) < amount: return await message.answer("❌ Мало ⭐!")
        cards = await conn.fetch("SELECT DISTINCT c.id, c.name, c.rating, c.club, c.position FROM user_cards uc JOIN all_cards c ON uc.card_id = c.id WHERE uc.user_id = $1 ORDER BY c.rating DESC LIMIT 20", uid)
    penalty_bet_wait.pop(uid, None)
    penalty_team_sel[uid] = {"bet": amount, "selected": [], "cards": [dict(c) for c in cards]}
    await _show_team_selector(message, uid)

async def _show_team_selector(msg, uid):
    data = penalty_team_sel[uid]
    kb = InlineKeyboardBuilder()
    for c in data["cards"]: kb.button(text=f"{'✅ ' if c['id'] in data['selected'] else ''}{c['name']}", callback_data=f"pen_pick_{c['id']}")
    if len(data['selected']) == 5: kb.button(text="✔️ Подтвердить", callback_data="pen_confirm")
    kb.button(text="🔄 Сброс", callback_data="pen_reset")
    txt = f"Выбери 5 игроков ({len(data['selected'])}/5):"
    if isinstance(msg, types.Message): await msg.answer(txt, reply_markup=kb.adjust(2).as_markup())
    else: await msg.message.edit_text(txt, reply_markup=kb.adjust(2).as_markup())

@dp.callback_query(F.data.startswith("pen_pick_"))
async def penalty_pick(call: types.CallbackQuery):
    uid, cid = call.from_user.id, int(call.data.split("_")[2])
    sel = penalty_team_sel[uid]["selected"]
    if cid in sel: sel.remove(cid)
    elif len(sel) < 5: sel.append(cid)
    await _show_team_selector(call, uid)

@dp.callback_query(F.data == "pen_reset")
async def penalty_reset(call: types.CallbackQuery):
    penalty_team_sel[call.from_user.id]["selected"] = []
    await _show_team_selector(call, call.from_user.id)

@dp.callback_query(F.data == "pen_confirm")
async def penalty_confirm(call: types.CallbackQuery):
    uid, data = call.from_user.id, penalty_team_sel.pop(call.from_user.id)
    uteam = [c for c in data["cards"] if c["id"] in data["selected"]]
    async with db() as conn:
        await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", data["bet"], uid)
        uids = ",".join(str(c["id"]) for c in uteam)
        bots = await conn.fetch(f"SELECT id, name, rating FROM all_cards WHERE id NOT IN ({uids}) ORDER BY RANDOM() LIMIT 5")
    bteam = [dict(c) for c in bots]
    penalty_sessions[uid] = {"bet": data["bet"], "user_team": uteam, "bot_team": bteam, "user_avg": avg_rating(uteam), "bot_avg": avg_rating(bteam), "underdog": (avg_rating(bteam)-avg_rating(uteam))>=5.0, "round": 0, "extra": False, "user_score": 0, "bot_score": 0, "phase": "user_kick"}
    await call.message.edit_text("⚽ <b>Пенальти начинается!</b>")
    await asyncio.sleep(2)
    await _send_penalty_kick(uid, call.message)

async def _send_penalty_kick(uid, msg):
    s = penalty_sessions[uid]
    kb = InlineKeyboardBuilder()
    for key, label in zip(CORNER_KEYS, CORNERS): kb.button(text=label, callback_data=f"pen_{'kick' if s['phase']=='user_kick' else 'save'}_{key}")
    await msg.edit_text(f"{_penalty_score_line(s)}\n\n{'🥅 Куда бьём?' if s['phase']=='user_kick' else '🧤 Куда прыгаем? (выбери 2)'}", reply_markup=kb.adjust(3,2).as_markup())
    if s["phase"] != "user_kick": s["save_picks"], s["bot_corner_current"] = [], random.choice(CORNER_KEYS)

@dp.callback_query(F.data.startswith("pen_kick_"))
async def pen_user_kick(call: types.CallbackQuery):
    uid, s, corner = call.from_user.id, penalty_sessions[uid], call.data.split("_")[2]
    saved = corner in bot_saves_corners(CORNER_KEYS)
    if not saved: s["user_score"] += 1
    await call.message.edit_text(f"{'🧤 СЕЙВ!' if saved else '⚽ ГОЛ!'}")
    await asyncio.sleep(1.5)
    s["phase"] = "bot_kick"
    await _send_penalty_kick(uid, call.message)

@dp.callback_query(F.data.startswith("pen_save_"))
async def pen_user_save(call: types.CallbackQuery):
    uid, s, corner = call.from_user.id, penalty_sessions[uid], call.data.split("_")[2]
    if corner not in s["save_picks"]: s["save_picks"].append(corner)
    if len(s["save_picks"]) < 2: return await call.message.edit_text("Выбери второй угол...", reply_markup=call.message.reply_markup)
    saved = s["bot_corner_current"] in s["save_picks"]
    if not saved: s["bot_score"] += 1
    await call.message.edit_text(f"{'🧤 СЕЙВ ТВОЙ!' if saved else '😬 ГОЛ БОТА!'}")
    await asyncio.sleep(1.5)
    s["round"] += 1
    s["phase"] = "user_kick"
    if s["round"] >= 5 and s["user_score"] != s["bot_score"]: return await _finish_penalty(uid, call.message)
    if s["round"] >= 5: s["extra"] = True
    await _send_penalty_kick(uid, call.message)

async def _finish_penalty(uid, msg):
    s = penalty_sessions.pop(uid)
    won = s["user_score"] > s["bot_score"]
    bonus = int(s["bet"] * PENALTY_WIN_BONUS_PCT) if won else 0
    underdog = int(s["bet"] * UNDERDOG_BONUS_PCT) if s["underdog"] else 0
    payout = (s["bet"] + bonus + underdog) if won else underdog
    async with db() as conn:
        await conn.execute("UPDATE users SET balance = balance + $1, last_penalty = $2 WHERE user_id = $3", payout, datetime.now().isoformat(), uid)
    await msg.edit_text(f"{'🏆 ПОБЕДА!' if won else '💔 ПРОИГРЫШ!'}\nИтог: {s['user_score']}:{s['bot_score']}\nВыплата: {payout} ⭐")

@dp.callback_query(F.data.startswith("vcoll_"))
async def show_collection(call: types.CallbackQuery):
    idx, uid = int(call.data.split("_")[1]), call.from_user.id
    async with db() as conn:
        items = await conn.fetch("SELECT c.name, c.rating FROM user_cards uc JOIN all_cards c ON uc.card_id = c.id WHERE uc.user_id = $1 ORDER BY c.rating DESC LIMIT 15 OFFSET $2", uid, idx*15)
        total = await conn.fetchval("SELECT COUNT(*) FROM user_cards WHERE user_id = $1", uid)
    if not items: return await call.message.edit_text("💼 Пусто.")
    out = "\n".join(f"▪️ {r['name']} — {r['rating']}" for r in items)
    nav = InlineKeyboardBuilder()
    if idx > 0: nav.button(text="⬅️ Назад", callback_data=f"vcoll_{idx-1}")
    if total > (idx+1)*15: nav.button(text="Вперед ➡️", callback_data=f"vcoll_{idx+1}")
    await call.message.edit_text(f"💼 Коллекция:\n\n{out}", reply_markup=nav.adjust(2).as_markup())

@dp.message(F.text == "Профиль 👤")
async def show_profile(message: types.Message):
    async with db() as conn: data = await conn.fetchrow("SELECT balance, username, vip_until, is_banned FROM users WHERE user_id = $1", message.from_user.id)
    if not data: return await message.answer("❌ Нет профиля.")
    vip = parse_dt(data["vip_until"])
    kb = InlineKeyboardBuilder().button(text="💼 Коллекция", callback_data="vcoll_0")
    await message.answer(f"👤 <b>Профиль @{data['username']}</b>\n💰 Баланс: {data['balance']:,} ⭐\n💎 VIP: {'Активен' if vip and vip>datetime.now() else 'Нет'}", reply_markup=kb.as_markup())

@dp.message(F.text == "Рефералка 👥")
async def invite_link(message: types.Message):
    async with db() as conn: stats = await conn.fetchrow("SELECT SUM(CASE WHEN revoked=0 AND bonus_paid_at IS NOT NULL THEN 1 ELSE 0 END) AS paid FROM referral_checks WHERE inviter_id = $1", message.from_user.id)
    me = await bot.get_me()
    await message.answer(f"👥 Рефералка\n\nТвоя ссылка: `t.me/{me.username}?start={message.from_user.id}`\n✅ Приглашено: {stats['paid'] if stats and stats['paid'] else 0}")

@dp.message(F.text == "ТОП-10 📊")
async def leaderboard(message: types.Message):
    async with db() as conn: rows = await conn.fetch("SELECT COALESCE(username, 'Игрок') as un, balance FROM users ORDER BY balance DESC LIMIT 10")
    await message.answer("🏆 ТОП-10:\n\n" + "\n".join(f"{i+1}. {r['un']} — {r['balance']:,} ⭐" for i, r in enumerate(rows)))

@dp.callback_query(F.data == "check_subs")
async def verify_subs(call: types.CallbackQuery):
    if not await get_subs_status(call.from_user.id):
        await call.message.delete()
        await _claim_card_flow(call)
    else: await call.answer("❌ Не все каналы!", show_alert=True)

@dp.message(Command("suggest"))
async def suggest_cmd(message: types.Message):
    player_suggest_sessions[message.from_user.id] = {"step": "suggest_name"}
    await message.answer("Шаг 1: Введи ИМЯ игрока")

@dp.message(F.text == "Поиск игрока 🔍")
async def psearch(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="По имени", callback_data="psearch_mode_name")
    await message.answer("Поиск:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("psearch_mode_"))
async def psearch_mode(call: types.CallbackQuery):
    player_suggest_sessions[call.from_user.id] = {"step": "search_name"}
    await call.message.edit_text("Введи имя:")

@dp.message(lambda m: m.from_user.id in player_suggest_sessions)
async def handle_suggest_search(message: types.Message):
    uid, sess = message.from_user.id, player_suggest_sessions.get(message.from_user.id)
    st = sess["step"]
    if st == "search_name":
        async with db() as conn: cards = await conn.fetch("SELECT id, name, rating FROM all_cards WHERE name ILIKE $1 LIMIT 10", f"%{message.text}%")
        player_suggest_sessions.pop(uid)
        if not cards: return await message.answer("Не найдено.")
        kb = InlineKeyboardBuilder()
        for c in cards: kb.button(text=f"{c['name']} ({c['rating']})", callback_data=f"ftclcard_{c['id']}")
        await message.answer("Найдено:", reply_markup=kb.adjust(1).as_markup())
    elif st == "suggest_name":
        sess["name"], sess["step"] = message.text, "suggest_rating"
        await message.answer("Введи рейтинг:")
    elif st == "suggest_rating":
        sess["rating"], sess["step"] = int(message.text), "suggest_club"
        await message.answer("Введи клуб:")
    elif st == "suggest_club":
        sess["club"], sess["step"] = message.text, "suggest_pos"
        await message.answer("Введи позицию:")
    elif st == "suggest_pos":
        sess["pos"], sess["step"] = message.text, "suggest_photo"
        await message.answer("Отправь ФАЙЛ (фото):")
    elif st == "suggest_photo" and message.document:
        add_cmd = f"/add_player {sess['name']} | {sess['rating']} | {sess['club']} | {sess['pos']}"
        await bot.send_document(MODERATION_GROUP_ID, message.document.file_id, caption=f"Заявка от {uid}\n\nКоманда:\n{add_cmd}")
        player_suggest_sessions.pop(uid)
        await message.answer("✅ Отправлено модераторам!")

@dp.message(F.text == "Спец. Паки 🎁")
async def sp_menu(message: types.Message):
    async with db() as conn: packs = await conn.fetch("SELECT id, title FROM special_packs WHERE is_active = 1")
    if not packs: return await message.answer("Паков нет.")
    kb = InlineKeyboardBuilder()
    for p in packs: kb.button(text=p['title'], callback_data=f"sp_view_{p['id']}")
    await message.answer("Спец Паки:", reply_markup=kb.adjust(1).as_markup())

@dp.callback_query(F.data.startswith("sp_view_"))
async def sp_view(call: types.CallbackQuery):
    pid = int(call.data.split("_")[2])
    async with db() as conn: pack = await conn.fetchrow("SELECT * FROM special_packs WHERE id = $1", pid)
    kb = InlineKeyboardBuilder().button(text="Открыть", callback_data=f"sp_open_{pid}")
    await call.message.edit_text(f"🎁 {pack['title']}\n\nНужна подписка: {pack['channel']}", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("sp_open_"))
async def sp_open(call: types.CallbackQuery):
    pid = int(call.data.split("_")[2])
    async with db() as conn:
        pack = await conn.fetchrow("SELECT * FROM special_packs WHERE id = $1", pid)
        cards = await conn.fetch("SELECT spc.card_id FROM special_pack_cards spc WHERE spc.pack_id = $1", pid)
        c = random.choice(cards)
        await conn.execute("INSERT INTO user_pack_opens (user_id, pack_id, opened_at) VALUES ($1, $2, $3)", call.from_user.id, pid, datetime.now().isoformat())
        await conn.execute("INSERT INTO user_cards (user_id, card_id) VALUES ($1, $2)", call.from_user.id, c["card_id"])
        c_full = await conn.fetchrow("SELECT * FROM all_cards WHERE id = $1", c["card_id"])
    await call.message.answer_photo(c_full["photo_id"], caption=f"🎁 Ты выбил {c_full['name']}!")

@dp.message(Command("createpack"), F.from_user.id == ADMIN_ID)
async def cp_start(message: types.Message):
    pack_create_sessions[message.from_user.id] = {"step": "title", "unique_cards": []}
    await message.answer("Создание пака. Введи название:")

@dp.message(lambda m: m.from_user.id in pack_create_sessions)
async def cp_input(message: types.Message):
    uid, sess = message.from_user.id, pack_create_sessions.get(message.from_user.id)
    st = sess["step"]
    if st == "title": sess["title"], sess["step"] = message.text, "chan"; await message.answer("Введи @канал:")
    elif st == "chan": sess["chan"], sess["step"] = message.text, "done"; await message.answer("Введи URL канала:")
    elif st == "done":
        async with db() as conn:
            pid = await conn.fetchval("INSERT INTO special_packs (title, channel, channel_url, days_active, cooldown_sec, expires_at) VALUES ($1, $2, $3, 30, 3600, $4) RETURNING id", sess["title"], sess["chan"], message.text, (datetime.now() + timedelta(days=30)).isoformat())
        pack_create_sessions.pop(uid)
        await message.answer(f"Пак создан! ID: {pid}")

# ================= ВЕБ-СЕРВЕР RENDER И ЗАПУСК =================
async def handle_ping(request):
    return web.Response(text="MIFL CARDS Bot is alive and using PostgreSQL!")

async def start_engine():
    await init_db()
    if MAINTENANCE_MODE: log.warning("⚠️ Бот запущен в РЕЖИМЕ ТЕХОБСЛУЖИВАНИЯ!")

    await clean_groups()

    dp.message.middleware(MaintenanceBanMiddleware())
    dp.callback_query.middleware(MaintenanceBanMiddleware())

    asyncio.create_task(referral_checker_loop())
    asyncio.create_task(notification_loop())

    # Веб-сервер для того, чтобы Render не выключал процесс
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"🌐 Мини-сервер запущен на порту {port}")

    try:
        await dp.start_polling(bot)
    finally:
        if db_pool:
            await db_pool.close()

if __name__ == "__main__":
    asyncio.run(start_engine())
