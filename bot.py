import asyncio
import aiosqlite
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

TOKEN    = os.getenv("TOKEN", "YOUR_BOT_TOKEN_HERE")
DB_PATH  = os.getenv("DATABASE_URL", "miflcards.db")
ADMIN_ID = int(os.getenv("ADMIN_ID", 1866813859))

MAINTENANCE_MODE    = True
MAINTENANCE_MESSAGE = (
    "🔧 <b>Бот на техническом обслуживании</b>\n\n"
    "Мы обновляем базу данных и улучшаем работу бота.\n"
    "Пожалуйста, попробуй позже. Вся информация тут - https://t.me/ftclgames"
)

REQUIRED_CHANNELS = {
    "@miflcards":   "https://t.me/miflcards",
}

REF_CHECK_DAYS      = 5
REF_BONUS_AMOUNT    = 5000
MODERATION_GROUP_ID = -1003951671147

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
dp  = Dispatcher()

@asynccontextmanager
async def db():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=10000")
        yield conn

async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS all_cards (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT,
                rating      INTEGER,
                position    TEXT,
                rarity      TEXT,
                rarity_type TEXT,
                club        TEXT,
                photo_id    TEXT
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id        INTEGER PRIMARY KEY,
                username       TEXT,
                balance        INTEGER DEFAULT 1000,
                last_open      TEXT,
                last_guess     TEXT,
                last_penalty   TEXT,
                vip_until      TEXT,
                referred_by    INTEGER,
                ref_bonus_paid INTEGER DEFAULT 0,
                is_banned      INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS user_cards (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                card_id INTEGER NOT NULL REFERENCES all_cards(id)  ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS active_teams (
                user_id INTEGER PRIMARY KEY,
                gk_id   INTEGER,
                def_ids TEXT,
                mid_ids TEXT,
                fwd_ids TEXT
            );

            CREATE TABLE IF NOT EXISTS stars_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                amount        INTEGER NOT NULL,
                balance_after INTEGER,
                reason        TEXT,
                created_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS referral_checks (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ref_user_id   INTEGER NOT NULL UNIQUE,
                inviter_id    INTEGER NOT NULL,
                joined_at     TEXT    NOT NULL,
                subscribed_at TEXT,
                bonus_paid_at TEXT,
                revoked       INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS user_notifications (
                user_id       INTEGER PRIMARY KEY,
                pack_notif    INTEGER DEFAULT 0,
                guess_notif   INTEGER DEFAULT 0,
                penalty_notif INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS allowed_groups (
                chat_id   INTEGER PRIMARY KEY,
                added_by  INTEGER,
                added_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS bot_chats (
                chat_id   INTEGER PRIMARY KEY,
                status    TEXT DEFAULT 'member',
                added_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS special_packs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                title         TEXT    NOT NULL,
                description   TEXT,
                channel       TEXT    NOT NULL,
                channel_url   TEXT    NOT NULL,
                days_active   INTEGER NOT NULL,
                cooldown_sec  INTEGER NOT NULL,
                unique_chance INTEGER NOT NULL DEFAULT 15,
                expires_at    TEXT    NOT NULL,
                created_at    TEXT    DEFAULT (datetime('now')),
                is_active     INTEGER DEFAULT 1,
                banner_photo  TEXT
            );

            CREATE TABLE IF NOT EXISTS special_pack_cards (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                pack_id   INTEGER NOT NULL REFERENCES special_packs(id) ON DELETE CASCADE,
                card_id   INTEGER NOT NULL REFERENCES all_cards(id)     ON DELETE CASCADE,
                is_unique INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS user_pack_opens (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                pack_id    INTEGER NOT NULL,
                opened_at  TEXT    NOT NULL,
                UNIQUE(user_id, pack_id)
            );

            CREATE INDEX IF NOT EXISTS idx_all_cards_rating   ON all_cards (rating);
            CREATE INDEX IF NOT EXISTS idx_all_cards_rarity   ON all_cards (rarity_type);
            CREATE INDEX IF NOT EXISTS idx_user_cards_user_id ON user_cards (user_id);
            CREATE INDEX IF NOT EXISTS idx_users_balance      ON users (balance DESC);
            CREATE INDEX IF NOT EXISTS idx_stars_log_user     ON stars_log (user_id);
            CREATE INDEX IF NOT EXISTS idx_refchecks_inviter  ON referral_checks (inviter_id);
        """)

        async with conn.execute("PRAGMA table_info(users)") as cur:
            cols = [r[1] async for r in cur]
        for col, definition in [
            ("last_guess",   "TEXT"),
            ("last_penalty", "TEXT"),
        ]:
            if col not in cols:
                await conn.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")

        async with conn.execute("PRAGMA table_info(stars_log)") as cur:
            log_cols = [r[1] async for r in cur]
        if "balance_after" not in log_cols:
            await conn.execute("ALTER TABLE stars_log ADD COLUMN balance_after INTEGER")

        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pack_opens_user ON user_pack_opens (user_id, pack_id)"
        )

        await conn.commit()
    log.info("База данных инициализирована: %s", DB_PATH)

guess_sessions    = {}
bet_waitlist      = {}
processing_users  = set()
penalty_sessions  = {}
penalty_team_sel  = {}
penalty_bet_wait  = {}
card_edit_sessions      = {}
player_suggest_sessions = {}
pack_create_sessions    = {}   # uid -> dict

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
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None

def get_pack_remaining(last_open: str | None, vip: bool) -> int:
    delay = 3600 if vip else 7200
    dt = parse_dt(last_open)
    if not dt:
        return 0
    return max(0, int(delay - (datetime.now() - dt).total_seconds()))

def get_guess_remaining(last_guess: str | None) -> int:
    dt = parse_dt(last_guess)
    if not dt:
        return 0
    return max(0, int(18000 - (datetime.now() - dt).total_seconds()))

def get_penalty_remaining(last_penalty: str | None) -> int:
    dt = parse_dt(last_penalty)
    if not dt:
        return 0
    return max(0, int(PENALTY_COOLDOWN - (datetime.now() - dt).total_seconds()))

def avg_rating(cards: list[dict]) -> float:
    if not cards:
        return 70.0
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
    async with conn.execute(
        "SELECT balance FROM users WHERE user_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    balance_after = row["balance"] if row else None
    await conn.execute(
        "INSERT INTO stars_log (user_id, amount, balance_after, reason) VALUES (?, ?, ?, ?)",
        (user_id, amount, balance_after, reason)
    )

async def ensure_user(conn, user_id: int, username: str | None = None) -> bool:
    async with conn.execute(
        "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
    ) as cur:
        existing = await cur.fetchone()
    if existing is None:
        await conn.execute(
            "INSERT INTO users (user_id, username) VALUES (?, ?)",
            (user_id, username)
        )
        return True
    if username:
        await conn.execute(
            "UPDATE users SET username = ? WHERE user_id = ?",
            (username, user_id)
        )
    return False

async def get_user_balance(conn, user_id: int) -> int | None:
    async with conn.execute(
        "SELECT balance FROM users WHERE user_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    return row["balance"] if row else None

async def is_vip(user_id: int) -> bool:
    async with db() as conn:
        async with conn.execute(
            "SELECT vip_until FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return False
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
    if hints_used >= 1:
        lines.append(f"📊 Рейтинг: <b>{target['rating']}</b>")
    if hints_used >= 2:
        lines.append(f"🔤 Имя начинается на: <b>{target['name'][0].upper()}...</b>")
    lines.append(f"\n💰 Награда за правильный ответ: <b>{reward:,} ⭐</b>")
    if hints_used == 0:
        lines.append("ℹ️ Можно взять подсказку, но награда уменьшится")
    elif hints_used == 1:
        lines.append("ℹ️ Осталась ещё одна подсказка")
    else:
        lines.append("ℹ️ Подсказок больше нет")
    return "\n".join(lines)

async def confirm_referral_subscription(ref_uid: int, inviter_id: int):
    async with db() as conn:
        async with conn.execute(
            "SELECT id FROM referral_checks "
            "WHERE ref_user_id = ? AND subscribed_at IS NULL AND revoked = 0",
            (ref_uid,)
        ) as cur:
            ref_record = await cur.fetchone()
        if not ref_record:
            return
        now = datetime.now()
        await conn.execute(
            "UPDATE referral_checks SET subscribed_at = ? WHERE id = ?",
            (now.isoformat(), ref_record["id"])
        )
        await conn.commit()

    deadline_date = (now + timedelta(days=REF_CHECK_DAYS)).strftime("%d.%m.%Y")
    try:
        await bot.send_message(
            inviter_id,
            f"✅ <b>Реферал подтвердил подписки!</b>\n\n"
            f"Пользователь подписался на все каналы.\n"
            f"⏳ <b>Бонус {REF_BONUS_AMOUNT:,} ⭐</b> будет начислен <b>{deadline_date}</b> "
            f"(через {REF_CHECK_DAYS} дней), если реферал останется подписан.\n\n"
            f"❌ Если отпишется — бонус сгорит."
        )
    except Exception:
        pass
    try:
        await bot.send_message(
            ref_uid,
            f"✅ <b>Подписки подтверждены!</b>\n\n"
            f"Реферальная система активирована.\n"
            f"⚠️ Если ты отпишешься от какого-либо канала в течение "
            f"<b>{REF_CHECK_DAYS} дней</b> (до {deadline_date}), "
            f"твой пригласитель <b>потеряет бонус {REF_BONUS_AMOUNT:,} ⭐</b>!\n\n"
            f"⚽️ <b>FTCL Cards приветствует тебя!</b>",
            reply_markup=_main_menu_markup()
        )
    except Exception:
        pass


async def _run_referral_checks():
    now      = datetime.now()
    deadline = (now - timedelta(days=REF_CHECK_DAYS)).isoformat()

    async with db() as conn:
        async with conn.execute(
            "SELECT rc.id, rc.ref_user_id, rc.inviter_id, "
            "       u.username AS ref_username "
            "FROM referral_checks rc "
            "JOIN users u ON u.user_id = rc.ref_user_id "
            "WHERE rc.bonus_paid_at IS NULL "
            "  AND rc.revoked = 0 "
            "  AND rc.subscribed_at IS NOT NULL "
            "  AND rc.subscribed_at <= ?",
            (deadline,)
        ) as cur:
            pending = await cur.fetchall()

    for row in pending:
        rec_id    = row["id"]
        ref_uid   = row["ref_user_id"]
        inv_uid   = row["inviter_id"]
        ref_uname = row["ref_username"] or str(ref_uid)

        try:
            still_subbed = await check_user_subscribed(ref_uid)
        except Exception as e:
            log.warning("Не удалось проверить подписку реферала %s: %s", ref_uid, e)
            continue

        if still_subbed:
            async with db() as conn:
                async with conn.execute(
                    "SELECT id FROM referral_checks WHERE id = ? AND bonus_paid_at IS NULL", (rec_id,)
                ) as cur:
                    if not await cur.fetchone():
                        continue
                await conn.execute(
                    "UPDATE referral_checks SET bonus_paid_at = ? WHERE id = ?",
                    (now.isoformat(), rec_id)
                )
                await conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (REF_BONUS_AMOUNT, inv_uid)
                )
                await log_stars(conn, inv_uid, REF_BONUS_AMOUNT,
                                f"Реферал подтверждён: @{ref_uname} (подписан {REF_CHECK_DAYS} дней)")
                await conn.commit()

            log.info("✅ Реферал одобрен: ref=%s inviter=%s", ref_uid, inv_uid)
            try:
                await bot.send_message(
                    inv_uid,
                    f"🎉 <b>Реферальный бонус начислен!</b>\n\n"
                    f"Пользователь @{ref_uname} был подписан {REF_CHECK_DAYS} дней.\n"
                    f"💰 Начислено: <b>+{REF_BONUS_AMOUNT:,} ⭐</b>"
                )
            except Exception:
                pass
        else:
            async with db() as conn:
                await conn.execute(
                    "UPDATE referral_checks SET revoked = 1 WHERE id = ?", (rec_id,)
                )
                await conn.commit()

            log.info("🔥 Реферал отписался: ref=%s inviter=%s", ref_uid, inv_uid)
            try:
                await bot.send_message(
                    inv_uid,
                    f"🔥 <b>Реферальный бонус сгорел!</b>\n\n"
                    f"Пользователь @{ref_uname} отписался от каналов "
                    f"в течение {REF_CHECK_DAYS}-дневного периода.\n"
                    f"💸 Бонус {REF_BONUS_AMOUNT:,} ⭐ не будет начислен."
                )
            except Exception:
                pass
            try:
                await bot.send_message(
                    ref_uid,
                    f"⚠️ Ты отписался от обязательных каналов, "
                    f"поэтому твой пригласитель <b>потерял бонус {REF_BONUS_AMOUNT:,} ⭐</b>.\n\n"
                    f"Подпишись снова, чтобы использовать бота."
                )
            except Exception:
                pass


async def _run_notification_checks():
    now = datetime.now()
    async with db() as conn:
        async with conn.execute(
            "SELECT u.user_id, u.last_open, u.last_guess, u.last_penalty, u.vip_until, "
            "       COALESCE(n.pack_notif,0) AS pack_notif, "
            "       COALESCE(n.guess_notif,0) AS guess_notif, "
            "       COALESCE(n.penalty_notif,0) AS penalty_notif "
            "FROM users u LEFT JOIN user_notifications n ON n.user_id = u.user_id "
            "WHERE u.is_banned = 0"
        ) as cur:
            users = await cur.fetchall()
    for u in users:
        uid = u["user_id"]
        vip = bool(u["vip_until"] and parse_dt(u["vip_until"]) and parse_dt(u["vip_until"]) > now)
        send_pack    = get_pack_remaining(u["last_open"], vip) == 0 and u["last_open"] and not u["pack_notif"]
        send_guess   = get_guess_remaining(u["last_guess"]) == 0 and u["last_guess"] and not u["guess_notif"]
        send_penalty = get_penalty_remaining(u["last_penalty"]) == 0 and u["last_penalty"] and not u["penalty_notif"]
        msgs = []
        if send_pack:    msgs.append("🏆 <b>Пак готов!</b> Открой новую карту — \"Получить Карту 🏆\"")
        if send_guess:   msgs.append("🧩 <b>Угадай игрока доступна!</b> Сыграй и выиграй ⭐ — \"Мини-Игры ⚽\"")
        if send_penalty: msgs.append("⚽ <b>Серия пенальти доступна!</b> Сыграй и выиграй ⭐ — \"Мини-Игры ⚽\"")
        if not msgs:
            continue
        try:
            await bot.send_message(uid, "\n\n".join(msgs))
        except Exception:
            pass
        async with db() as conn:
            await conn.execute(
                "INSERT INTO user_notifications (user_id, pack_notif, guess_notif, penalty_notif) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "  pack_notif    = CASE WHEN excluded.pack_notif    = 1 THEN 1 ELSE pack_notif    END, "
                "  guess_notif   = CASE WHEN excluded.guess_notif   = 1 THEN 1 ELSE guess_notif   END, "
                "  penalty_notif = CASE WHEN excluded.penalty_notif = 1 THEN 1 ELSE penalty_notif END",
                (uid, 1 if send_pack else 0, 1 if send_guess else 0, 1 if send_penalty else 0)
            )
            await conn.commit()


async def _reset_notif(uid: int, flag: str):
    async with db() as conn:
        await conn.execute(
            f"INSERT INTO user_notifications (user_id) VALUES (?) "
            f"ON CONFLICT(user_id) DO UPDATE SET {flag} = 0", (uid,)
        )
        await conn.commit()



async def notification_loop():
    await asyncio.sleep(15)
    while True:
        try:
            await _run_notification_checks()
        except Exception as e:
            log.error("notification_loop error: %s", e)
        await asyncio.sleep(60)


async def referral_checker_loop():
    await asyncio.sleep(10)
    while True:
        try:
            await _run_referral_checks()
        except Exception as e:
            log.error("referral_checker_loop error: %s", e)
        await asyncio.sleep(1800)

class MaintenanceBanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, types.Message):
            user_id     = event.from_user.id if event.from_user else None
            is_callback = False
            if event.text and event.text.startswith("/start"):
                return await handler(event, data)
        elif isinstance(event, types.CallbackQuery):
            user_id     = event.from_user.id if event.from_user else None
            is_callback = True
        else:
            return await handler(event, data)

        if user_id is None or user_id == ADMIN_ID:
            return await handler(event, data)

        if MAINTENANCE_MODE:
            if is_callback and event.data and event.data.startswith("confirm_ref_"):
                return await handler(event, data)
            if is_callback:
                try:
                    await event.answer("🔧 Бот на техобслуживании. Попробуй позже.", show_alert=True)
                except TelegramBadRequest:
                    pass
                return
            else:
                await event.answer(MAINTENANCE_MESSAGE)
                return

        async with db() as conn:
            async with conn.execute(
                "SELECT is_banned FROM users WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()

        if row and row["is_banned"]:
            if is_callback:
                try:
                    await event.answer("🚫 Ваш аккаунт заблокирован.", show_alert=True)
                except TelegramBadRequest:
                    pass
            else:
                await event.answer("🚫 Ваш аккаунт заблокирован.")
            return

        return await handler(event, data)

@dp.my_chat_member()
async def on_my_chat_member(update: types.ChatMemberUpdated):
    chat = update.chat
    if chat.type not in ("group", "supergroup"):
        return
    new_status = update.new_chat_member.status
    old_status = update.old_chat_member.status

    async with db() as conn:
        await conn.execute(
            "INSERT INTO bot_chats (chat_id, status) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET status = ?",
            (chat.id, new_status, new_status)
        )
        await conn.commit()

    if new_status == "member" and old_status in ("left", "kicked"):
        async with db() as conn:
            async with conn.execute("SELECT chat_id FROM allowed_groups WHERE chat_id = ?", (chat.id,)) as cur:
                allowed = await cur.fetchone()
        if not allowed:
            try:
                await bot.send_message(chat.id,
                    "🚫 <b>Добавление бота в группу запрещено!</b>\n"
                    "Для связи: @ismayil066\n"
                    "Бот покидает группу..."
                )
            except Exception:
                pass
            try:
                await bot.leave_chat(chat.id)
            except Exception as e:
                log.warning("Не удалось покинуть чат %s: %s", chat.id, e)
            async with db() as conn:
                await conn.execute("UPDATE bot_chats SET status='left' WHERE chat_id=?", (chat.id,))
                await conn.commit()

async def clean_groups():
    async with db() as conn:
        async with conn.execute("SELECT chat_id FROM allowed_groups") as cur:
            allowed = {row[0] async for row in cur}
        async with conn.execute("SELECT chat_id FROM bot_chats WHERE status='member'") as cur:
            chats = [row[0] async for row in cur]
    for chat_id in chats:
        if chat_id in allowed:
            continue
        try:
            await bot.send_message(chat_id,
                "🚫 Бот был удалён из группы, так как добавление запрещено.\n"
                "Связь: @ismayil066"
            )
        except Exception:
            pass
        try:
            await bot.leave_chat(chat_id)
        except Exception as e:
            log.warning("Не удалось покинуть чат %s: %s", chat_id, e)
        async with db() as conn:
            await conn.execute("UPDATE bot_chats SET status='left' WHERE chat_id=?", (chat_id,))
            await conn.commit()

@dp.message(Command("allowgroup"), F.from_user.id == ADMIN_ID)
async def cmd_allowgroup(message: types.Message, command: CommandObject):
    if not command.args:
        async with db() as conn:
            async with conn.execute("SELECT chat_id FROM allowed_groups") as cur:
                rows = await cur.fetchall()
        if not rows:
            return await message.answer("Разрешённых групп нет.")
        text = "Разрешённые группы:\n" + "\n".join(str(r[0]) for r in rows)
        return await message.answer(text)
    try:
        chat_id = int(command.args.strip())
    except ValueError:
        return await message.answer("Неверный ID чата (должен быть числом).")
    async with db() as conn:
        await conn.execute("INSERT OR IGNORE INTO allowed_groups (chat_id, added_by) VALUES (?, ?)",
                           (chat_id, message.from_user.id))
        await conn.commit()
    await message.answer(f"✅ Группа {chat_id} добавлена в белый список. Бот больше не будет её покидать.")

@dp.message(Command("disallowgroup"), F.from_user.id == ADMIN_ID)
async def cmd_disallowgroup(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("Использование: /disallowgroup <chat_id>")
    try:
        chat_id = int(command.args.strip())
    except ValueError:
        return await message.answer("Неверный ID чата (должен быть числом).")
    async with db() as conn:
        await conn.execute("DELETE FROM allowed_groups WHERE chat_id = ?", (chat_id,))
        await conn.commit()
    await message.answer(f"❌ Группа {chat_id} удалена из белого списка.")

@dp.message(Command("allowthisgroup"), F.chat.type.in_({"group", "supergroup"}), F.from_user.id == ADMIN_ID)
async def cmd_allowthisgroup(message: types.Message):
    chat_id = message.chat.id
    async with db() as conn:
        await conn.execute("INSERT OR IGNORE INTO allowed_groups (chat_id, added_by) VALUES (?, ?)",
                           (chat_id, message.from_user.id))
        await conn.commit()
    await message.answer("✅ Эта группа теперь разрешена для бота.")

@dp.message(Command("disallowthisgroup"), F.chat.type.in_({"group", "supergroup"}), F.from_user.id == ADMIN_ID)
async def cmd_disallowthisgroup(message: types.Message):
    chat_id = message.chat.id
    async with db() as conn:
        await conn.execute("DELETE FROM allowed_groups WHERE chat_id = ?", (chat_id,))
        await conn.commit()
    await message.answer("❌ Эта группа удалена из белого списка. Бот покинет её при следующем добавлении.")

async def _claim_card_flow(message_or_call):
    is_call = isinstance(message_or_call, types.CallbackQuery)
    uid = message_or_call.from_user.id

    async def _send(text, **kwargs):
        if is_call:
            return await message_or_call.message.answer(text, **kwargs)
        return await message_or_call.answer(text, **kwargs)

    async def _send_photo(photo, **kwargs):
        if is_call:
            return await message_or_call.message.answer_photo(photo, **kwargs)
        return await message_or_call.answer_photo(photo, **kwargs)

    if uid in processing_users:
        if is_call:
            return await message_or_call.answer("⏳ Подожди, твой запрос уже обрабатывается...", show_alert=True)
        return await message_or_call.answer("⏳ Подожди, твой запрос уже обрабатывается...")
    processing_users.add(uid)
    try:
        unsubbed = await get_subs_status(uid)
        if unsubbed:
            kb = InlineKeyboardBuilder()
            for i, (tag, link) in enumerate(unsubbed, 1):
                kb.button(text=f"Канал {i} 📢", url=link)
            kb.button(text="Я подписался ✅", callback_data="check_subs")
            return await _send(
                "❌ <b>Подпишись на каналы для получения карты!</b>",
                reply_markup=kb.adjust(1).as_markup()
            )

        vip_status = await is_vip(uid)
        async with db() as conn:
            await ensure_user(conn, uid, message_or_call.from_user.username)
            async with conn.execute(
                "SELECT last_open FROM users WHERE user_id = ?", (uid,)
            ) as cur:
                user_row = await cur.fetchone()
            await conn.commit()

        remaining = get_pack_remaining(user_row["last_open"] if user_row else None, vip_status)
        if remaining > 0:
            h, m = remaining // 3600, (remaining % 3600) // 60
            return await _send(f"⌛ <b>До следующего пака: {h}ч. {m}м.</b>")

        pool_tiers  = ["bronze", "gold", "brilliant", "ivents", "legend"]
        target_tier = random.choices(pool_tiers, weights=[70, 20, 5, 3, 2], k=1)[0]

        async with db() as conn:
            async with conn.execute(
                f"SELECT * FROM all_cards WHERE {TIER_SQL[target_tier]} ORDER BY RANDOM() LIMIT 1"
            ) as cur:
                card = await cur.fetchone()
            if not card:
                async with conn.execute(
                    "SELECT * FROM all_cards ORDER BY RANDOM() LIMIT 1"
                ) as cur:
                    card = await cur.fetchone()

        if not card:
            return await _send("❌ Ошибка: карты не найдены.")

        rarity_key = card["rarity_type"] if card["rarity_type"] in TIER_REWARDS else determine_tier(card["rating"])
        lo, hi = TIER_REWARDS.get(rarity_key, (250, 500))
        bonus  = random.randint(lo, hi)

        async with db() as conn:
            now_iso = datetime.now().isoformat()
            await conn.execute(
                "UPDATE users SET balance = balance + ?, last_open = ? WHERE user_id = ?",
                (bonus, now_iso, uid)
            )
            await conn.execute(
                "INSERT INTO user_cards (user_id, card_id) VALUES (?, ?)",
                (uid, card["id"])
            )
            await log_stars(conn, uid, bonus, f"Пак: {card['name']} ({rarity_key})")
            await conn.commit()
        await _reset_notif(uid, 'pack_notif')

        loader = await _send("Открываем пак... 💼")
        await asyncio.sleep(2)
        try:
            await loader.delete()
        except Exception:
            pass
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
            async with conn.execute(
                "SELECT id, name, rating, club, position, rarity, photo_id FROM all_cards "
                "WHERE UPPER(name) LIKE UPPER(?) ORDER BY rating DESC LIMIT 10",
                (query,)
            ) as cur:
                cards = await cur.fetchall()
        if not cards:
            return await message.answer(f"Игроки с именем «{command.args.strip()}» не найдены.")
        if len(cards) == 1:
            card = cards[0]
            caption = (f"🃏 <b>{card['name']}</b>\n"
                       f"📊 Рейтинг: {card['rating']}\n"
                       f"🛡 Клуб: {card['club']}\n"
                       f"📍 Позиция: {card['position']}\n"
                       f"🏷 Тип: {card['rarity']}")
            await message.answer_photo(card['photo_id'], caption=caption)
        else:
            kb = InlineKeyboardBuilder()
            for card in cards:
                kb.button(text=f"{card['name']} ({card['rating']})", callback_data=f"ftclcard_{card['id']}")
            kb.adjust(1)
            await message.answer("🔍 Найдено несколько игроков. Выберите:", reply_markup=kb.as_markup())
    else:
        await _claim_card_flow(message)

@dp.callback_query(F.data.startswith("ftclcard_"))
async def ftclcard_callback(call: types.CallbackQuery):
    card_id = int(call.data.split("_")[1])
    async with db() as conn:
        async with conn.execute("SELECT * FROM all_cards WHERE id=?", (card_id,)) as cur:
            card = await cur.fetchone()
    if not card:
        return await call.answer("Карта не найдена", show_alert=True)
    caption = (f"🃏 <b>{card['name']}</b>\n"
               f"📊 Рейтинг: {card['rating']}\n"
               f"🛡 Клуб: {card['club']}\n"
               f"📍 Позиция: {card['position']}\n"
               f"🏷 Тип: {card['rarity']}")
    await call.message.delete()
    await call.message.answer_photo(card['photo_id'], caption=caption)
    await call.answer()

@dp.message(Command("givevip"), F.from_user.id == ADMIN_ID)
async def cmd_givevip(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("Использование: /givevip @username [дней=30]")
    parts = command.args.split()
    username = parts[0].lstrip("@")
    days = 30
    if len(parts) > 1:
        try:
            days = int(parts[1])
        except ValueError:
            return await message.answer("Количество дней должно быть числом.")
    async with db() as conn:
        async with conn.execute("SELECT user_id, username FROM users WHERE username=?", (username,)) as cur:
            user = await cur.fetchone()
        if not user:
            return await message.answer(f"Пользователь @{username} не найден.")
        expiry = (datetime.now() + timedelta(days=days)).isoformat()
        await conn.execute("UPDATE users SET vip_until=? WHERE user_id=?", (expiry, user['user_id']))
        await conn.commit()
    await message.answer(f"💎 VIP выдан @{username} на {days} дней (до {datetime.now()+timedelta(days=days):%d.%m.%Y %H:%M}).")
    try:
        await bot.send_message(user['user_id'],
            f"🎉 Администратор выдал вам VIP на {days} дней! Наслаждайтесь преимуществами.")
    except Exception:
        pass

@dp.message(Command("maintenance"), F.from_user.id == ADMIN_ID)
async def toggle_maintenance(message: types.Message):
    global MAINTENANCE_MODE
    MAINTENANCE_MODE = not MAINTENANCE_MODE
    status = "✅ ВКЛЮЧЕНО" if MAINTENANCE_MODE else "❌ ВЫКЛЮЧЕНО"
    await message.answer(f"🔧 <b>Режим техобслуживания:</b> {status}")


@dp.message(Command("add_player"), F.from_user.id == ADMIN_ID)
async def handle_add_player(message: types.Message, command: CommandObject):
    if not message.photo or not command.args:
        return await message.answer(
            "Формат: фото + <code>/add_player Имя | Рейтинг | Клуб | Позиция</code>"
        )
    try:
        parts = [p.strip() for p in command.args.split("|")]
        name, rate, club, pos = parts
        rating = int(rate)
        tier   = determine_tier(rating)
        async with db() as conn:
            await conn.execute(
                "INSERT INTO all_cards (name, rating, club, photo_id, position, rarity, rarity_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, rating, club, message.photo[-1].file_id, pos, tier, tier)
            )
            await conn.commit()
        await message.answer(f"✅ Карта <b>{name}</b> успешно сохранена.")
    except Exception as err:
        await message.answer(f"❌ Ошибка: {err}")


@dp.message(Command("starslog"), F.from_user.id == ADMIN_ID)
async def admin_stars_log(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer(
            "ℹ️ Использование: <code>/starslog @username [кол-во]</code>\n"
            "Пример: <code>/starslog @ivan 20</code>"
        )
    args         = command.args.strip().split()
    raw_username = args[0].lstrip("@")
    limit        = min(int(args[1]), 50) if len(args) > 1 and args[1].isdigit() else 15
    async with db() as conn:
        async with conn.execute(
            "SELECT user_id, username, balance FROM users WHERE username = ?",
            (raw_username,)
        ) as cur:
            user_row = await cur.fetchone()
        if not user_row:
            return await message.answer(f"❌ @{raw_username} не найден.")
        uid             = user_row["user_id"]
        current_balance = user_row["balance"]
        async with conn.execute(
            "SELECT id, amount, balance_after, reason, created_at "
            "FROM stars_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (uid, limit)
        ) as cur:
            logs = await cur.fetchall()
        async with conn.execute(
            "SELECT amount, balance_after FROM stars_log WHERE user_id = ? ORDER BY id ASC LIMIT 1",
            (uid,)
        ) as cur:
            first = await cur.fetchone()
        async with conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM stars_log WHERE user_id = ?",
            (uid,)
        ) as cur:
            sum_row = await cur.fetchone()
    total_logged = sum_row["total"] if sum_row else 0
    if first and first["balance_after"] is not None:
        initial_balance = first["balance_after"] - first["amount"]
    else:
        initial_balance = 1000
    expected_balance = initial_balance + total_logged
    mismatch         = abs(expected_balance - current_balance)
    header = [
        f"📋 <b>Лог: @{raw_username}</b> | ID: <code>{uid}</code>",
        f"🏦 Начальный баланс: <b>{initial_balance:,} ⭐</b>",
        f"💰 Текущий баланс: <b>{current_balance:,} ⭐</b>",
        f"📊 Сумма транзакций: <b>{total_logged:+,} ⭐</b>",
        f"🧮 Ожидаемый баланс: <b>{expected_balance:,} ⭐</b>",
    ]
    header.append(f"\n{'─'*28}\nПоследние {len(logs)} записей:")
    if not logs:
        return await message.answer("\n".join(header) + "\n\n<i>Записей нет.</i>")
    entry_lines = []
    for r in logs:
        sign      = "+" if r["amount"] >= 0 else ""
        emoji     = "💚" if r["amount"] >= 0 else "🔴"
        dt        = (r["created_at"] or "")[:16].replace("T", " ") or "—"
        bal_after = f"→ <b>{r['balance_after']:,} ⭐</b>" if r["balance_after"] is not None else ""
        entry_lines.append(
            f"{emoji} <b>{sign}{r['amount']:,} ⭐</b> {bal_after}\n"
            f"   📌 {r['reason'] or '—'}\n"
            f"   🕐 {dt}"
        )
    full = "\n".join(header) + "\n\n" + "\n\n".join(entry_lines)
    if len(full) <= 4000:
        await message.answer(full)
    else:
        await message.answer("\n".join(header))
        chunk = []
        for line in entry_lines:
            chunk.append(line)
            if sum(len(l) for l in chunk) > 3200:
                await message.answer("\n\n".join(chunk[:-1]))
                chunk = [line]
        if chunk:
            await message.answer("\n\n".join(chunk))


@dp.message(Command("refcheck"), F.from_user.id == ADMIN_ID)
async def admin_refcheck(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("ℹ️ Использование: <code>/refcheck @username</code>")
    raw_username = command.args.strip().lstrip("@")
    async with db() as conn:
        async with conn.execute(
            "SELECT user_id FROM users WHERE username = ?", (raw_username,)
        ) as cur:
            user_row = await cur.fetchone()
        if not user_row:
            return await message.answer(f"❌ @{raw_username} не найден.")
        uid = user_row["user_id"]
        async with conn.execute(
            "SELECT rc.id, u.username AS ref_uname, rc.joined_at, rc.subscribed_at, "
            "       rc.bonus_paid_at, rc.revoked "
            "FROM referral_checks rc "
            "JOIN users u ON u.user_id = rc.ref_user_id "
            "WHERE rc.inviter_id = ? ORDER BY rc.id DESC LIMIT 20",
            (uid,)
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return await message.answer(f"📋 Рефералы @{raw_username}: записей нет.")
    lines = [f"📋 <b>Рефералы @{raw_username}:</b>\n"]
    for r in rows:
        if r["revoked"]:
            status = "🔥 Бонус сгорел"
        elif r["bonus_paid_at"]:
            status = "✅ Бонус выплачен"
        elif r["subscribed_at"]:
            sub_dt   = parse_dt(r["subscribed_at"])
            deadline = sub_dt + timedelta(days=REF_CHECK_DAYS) if sub_dt else None
            now      = datetime.now()
            if deadline and deadline > now:
                left = int((deadline - now).total_seconds())
                h, m = left // 3600, (left % 3600) // 60
                status = f"⏳ Ожидание ({h}ч {m}м)"
            else:
                status = "🕐 Ожидает проверки"
        else:
            status = "⚠️ Ожидает подписки"
        uname  = r["ref_uname"] or f"#{r['id']}"
        joined = (r["joined_at"] or "")[:10]
        lines.append(f"• @{uname} | вступил {joined} | {status}")
    await message.answer("\n".join(lines))


@dp.message(Command("find_card"), F.from_user.id == ADMIN_ID)
async def find_card(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("ℹ️ Использование: <code>/find_card Месси</code>")
    query = f"%{command.args.strip()}%"
    async with db() as conn:
        async with conn.execute(
            "SELECT id, name, rating, club, position, rarity FROM all_cards "
            "WHERE name LIKE ? ORDER BY rating DESC LIMIT 20",
            (query,)
        ) as cur:
            cards = await cur.fetchall()
    if not cards:
        return await message.answer(f"❌ Карты «{command.args.strip()}» не найдены.")
    if len(cards) == 1:
        return await message.answer(
            _card_info_text(cards[0]),
            reply_markup=_card_manage_kb(cards[0]["id"])
        )
    kb = InlineKeyboardBuilder()
    for c in cards:
        kb.button(
            text=f"{c['name']} | {c['rating']} | {c['club']}",
            callback_data=f"cadmin_select_{c['id']}"
        )
    kb.adjust(1)
    await message.answer(
        f"🔍 Найдено: <b>{len(cards)}</b>. Выбери нужную:",
        reply_markup=kb.as_markup()
    )


def _card_info_text(card) -> str:
    return (
        f"🃏 <b>{card['name']}</b>\n"
        f"📊 Рейтинг: {card['rating']}\n"
        f"🛡 Клуб: {card['club']}\n"
        f"📍 Позиция: {card['position']}\n"
        f"🏷 Тип: {card['rarity']}\n"
        f"🆔 ID карты: <code>{card['id']}</code>"
    )

def _card_manage_kb(card_id: int) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить имя",     callback_data=f"cadmin_edit_name_{card_id}")
    kb.button(text="📊 Изменить рейтинг", callback_data=f"cadmin_edit_rating_{card_id}")
    kb.button(text="🛡 Изменить клуб",    callback_data=f"cadmin_edit_club_{card_id}")
    kb.button(text="📍 Изменить позицию", callback_data=f"cadmin_edit_position_{card_id}")
    kb.button(text="🖼 Изменить фото",    callback_data=f"cadmin_edit_photo_{card_id}")
    kb.button(text="🎁 Выдать игроку",    callback_data=f"cadmin_give_{card_id}")
    kb.button(text="🗑 Удалить карту",    callback_data=f"cadmin_delete_{card_id}")
    kb.adjust(2)
    return kb.as_markup()


@dp.callback_query(F.data.startswith("cadmin_select_"), F.from_user.id == ADMIN_ID)
async def cadmin_select(call: types.CallbackQuery):
    card_id = int(call.data.split("_")[2])
    async with db() as conn:
        async with conn.execute(
            "SELECT id, name, rating, club, position, rarity FROM all_cards WHERE id = ?",
            (card_id,)
        ) as cur:
            card = await cur.fetchone()
    if not card:
        return await call.answer("❌ Карта не найдена.", show_alert=True)
    await call.message.edit_text(_card_info_text(card), reply_markup=_card_manage_kb(card_id))
    await call.answer()


@dp.callback_query(F.data.startswith("cadmin_delete_"), F.from_user.id == ADMIN_ID)
async def cadmin_delete(call: types.CallbackQuery):
    card_id = int(call.data.split("_")[2])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"cadmin_confirm_delete_{card_id}")
    kb.button(text="❌ Отмена",      callback_data=f"cadmin_select_{card_id}")
    await call.message.edit_text(
        f"⚠️ Удалить карту ID <code>{card_id}</code>?\nЭто также удалит её из коллекций!",
        reply_markup=kb.as_markup()
    )
    await call.answer()


@dp.callback_query(F.data.startswith("cadmin_confirm_delete_"), F.from_user.id == ADMIN_ID)
async def cadmin_confirm_delete(call: types.CallbackQuery):
    card_id = int(call.data.split("_")[3])
    async with db() as conn:
        await conn.execute("DELETE FROM all_cards WHERE id = ?", (card_id,))
        await conn.commit()
    await call.message.edit_text(f"🗑 Карта ID <code>{card_id}</code> удалена.")
    await call.answer()


@dp.callback_query(F.data.startswith("cadmin_give_"), F.from_user.id == ADMIN_ID)
async def cadmin_give_prompt(call: types.CallbackQuery):
    card_id = int(call.data.split("_")[2])
    card_edit_sessions[call.from_user.id] = {"card_id": card_id, "step": "give_username"}
    await call.message.edit_text(
        f"🎁 Выдать карту ID <code>{card_id}</code>\n\nВведи <b>@username</b> игрока:"
    )
    await call.answer()


@dp.callback_query(F.data.startswith("cadmin_edit_"), F.from_user.id == ADMIN_ID)
async def cadmin_edit_prompt(call: types.CallbackQuery):
    parts   = call.data.split("_")
    field   = parts[2]
    card_id = int(parts[3])
    prompts = {
        "name":     "Введи новое имя карты:",
        "rating":   "Введи новый рейтинг (50–99):",
        "club":     "Введи новое название клуба:",
        "position": "Введи позицию (Нападающий / Полузащитник / Защитник / Вратарь):",
        "photo":    "Отправь новое фото карты (как фото, не файл):",
    }
    if field not in prompts:
        return await call.answer("❌ Неизвестное поле.", show_alert=True)
    card_edit_sessions[call.from_user.id] = {"card_id": card_id, "step": f"edit_{field}"}
    await call.message.edit_text(f"✏️ {prompts[field]}")
    await call.answer()


@dp.message(lambda msg: msg.from_user.id in card_edit_sessions and msg.from_user.id == ADMIN_ID)
async def cadmin_handle_input(message: types.Message):
    uid     = message.from_user.id
    session = card_edit_sessions.get(uid)
    if not session:
        return
    card_id = session["card_id"]
    step    = session["step"]
    if step == "give_username":
        raw = (message.text or "").strip().lstrip("@")
        async with db() as conn:
            async with conn.execute(
                "SELECT user_id FROM users WHERE username = ?", (raw,)
            ) as cur:
                user_row = await cur.fetchone()
            if not user_row:
                return await message.answer(f"❌ Пользователь @{raw} не найден.")
            target_uid = user_row["user_id"]
            async with conn.execute(
                "SELECT name FROM all_cards WHERE id = ?", (card_id,)
            ) as cur:
                card = await cur.fetchone()
            await conn.execute(
                "INSERT INTO user_cards (user_id, card_id) VALUES (?, ?)",
                (target_uid, card_id)
            )
            await conn.commit()
        card_edit_sessions.pop(uid, None)
        card_name = card["name"] if card else f"ID {card_id}"
        await message.answer(f"✅ Карта <b>{card_name}</b> выдана @{raw}!")
        try:
            await bot.send_message(target_uid, f"🎁 Тебе выдана карта <b>{card_name}</b> от администратора!")
        except Exception:
            pass
        return
    if step == "edit_photo":
        if not message.photo:
            return await message.answer("❌ Отправь фото (не файл).")
        new_val  = message.photo[-1].file_id
        db_field = "photo_id"
        display  = "Фото обновлено"
    elif step == "edit_name":
        new_val  = (message.text or "").strip()
        db_field = "name"
        display  = f"Имя → <b>{new_val}</b>"
    elif step == "edit_rating":
        if not message.text or not message.text.strip().isdigit():
            return await message.answer("❌ Введи число от 50 до 99.")
        rating = int(message.text.strip())
        if not (50 <= rating <= 99):
            return await message.answer("❌ Рейтинг должен быть от 50 до 99.")
        new_rarity = determine_tier(rating)
        async with db() as conn:
            await conn.execute(
                "UPDATE all_cards SET rating = ?, rarity = ?, rarity_type = ? WHERE id = ?",
                (rating, new_rarity, new_rarity, card_id)
            )
            await conn.commit()
        card_edit_sessions.pop(uid, None)
        return await message.answer(f"✅ Рейтинг → <b>{rating}</b> | Тип → <b>{new_rarity}</b>")
    elif step == "edit_club":
        new_val  = (message.text or "").strip()
        db_field = "club"
        display  = f"Клуб → <b>{new_val}</b>"
    elif step == "edit_position":
        new_val  = (message.text or "").strip()
        db_field = "position"
        display  = f"Позиция → <b>{new_val}</b>"
    else:
        card_edit_sessions.pop(uid, None)
        return
    async with db() as conn:
        await conn.execute(
            f"UPDATE all_cards SET {db_field} = ? WHERE id = ?",
            (new_val, card_id)
        )
        await conn.commit()
    card_edit_sessions.pop(uid, None)
    await message.answer(f"✅ {display}")

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    uid      = message.from_user.id
    params   = message.text.split()
    referrer = int(params[1]) if len(params) > 1 and params[1].isdigit() else None
    async with db() as conn:
        await ensure_user(conn, uid, message.from_user.username)
        if referrer and referrer != uid:
            async with conn.execute(
                "SELECT id FROM referral_checks WHERE ref_user_id = ?", (uid,)
            ) as cur:
                existing = await cur.fetchone()
            async with conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (referrer,)
            ) as cur:
                ref_exists = await cur.fetchone()
            if not existing and ref_exists:
                await conn.execute(
                    "INSERT INTO referral_checks (ref_user_id, inviter_id, joined_at) VALUES (?, ?, ?)",
                    (uid, referrer, datetime.now().isoformat())
                )
                await conn.execute(
                    "UPDATE users SET referred_by = ? WHERE user_id = ?",
                    (referrer, uid)
                )
        await conn.commit()
    if referrer and referrer != uid:
        unsubbed = await get_subs_status(uid)
        if unsubbed:
            kb = InlineKeyboardBuilder()
            for i, (tag, link) in enumerate(unsubbed, 1):
                kb.button(text=f"Канал {i} 📢", url=link)
            kb.button(text="✅ Я подписался", callback_data=f"confirm_ref_{referrer}")
            kb.adjust(1)
            await message.answer(
                f"👋 <b>Ты перешёл по реферальной ссылке!</b>\n\n"
                f"Подпишись на все каналы и нажми кнопку проверки:\n\n"
                f"⚠️ После подтверждения, если ты отпишешься в течение {REF_CHECK_DAYS} дней — "
                f"бонус пригласителя сгорит!",
                reply_markup=kb.as_markup()
            )
            return
        else:
            await confirm_referral_subscription(uid, referrer)
    await message.answer("⚽️ <b>FTCL Cards приветствует тебя!</b>", reply_markup=_main_menu_markup())


@dp.callback_query(F.data.startswith("confirm_ref_"))
async def handle_confirm_referral(call: types.CallbackQuery):
    uid = call.from_user.id
    try:
        inviter_id = int(call.data.split("_")[2])
    except (ValueError, IndexError):
        return await call.answer("❌ Ошибка данных.", show_alert=True)
    if await check_user_subscribed(uid):
        await call.message.delete()
        await confirm_referral_subscription(uid, inviter_id)
        await call.answer("✅ Подписки подтверждены!", show_alert=True)
    else:
        unsubbed = await get_subs_status(uid)
        kb = InlineKeyboardBuilder()
        for i, (tag, link) in enumerate(unsubbed, 1):
            kb.button(text=f"Канал {i} 📢", url=link)
        kb.button(text="✅ Я подписался", callback_data=f"confirm_ref_{inviter_id}")
        kb.adjust(1)
        await call.message.edit_text(
            "❌ <b>Не все подписки оформлены!</b>\n\nПодпишись на все каналы и нажми снова:",
            reply_markup=kb.as_markup()
        )
        await call.answer("❌ Не все каналы!", show_alert=True)


@dp.message(F.text == "Получить Карту 🏆")
async def claim_card(message: types.Message):
    await _claim_card_flow(message)


@dp.message(F.text == "Мини-Игры ⚽")
async def games_menu(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🧩 Угадай игрока",  callback_data="game_guess")
    kb.button(text="⚽ Серия пенальти", callback_data="game_penalty")
    kb.adjust(1)
    await message.answer("🎮 <b>Доступные режимы:</b>", reply_markup=kb.as_markup())


@dp.message(F.text == "Магазин 🛒")
async def open_shop(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Bronze (1200⭐)",    callback_data="buy_bronze")
    kb.button(text="📦 Gold (3500⭐)",      callback_data="buy_gold")
    kb.button(text="📦 Brilliant (6700⭐)", callback_data="buy_brilliant")
    kb.button(text="💎 VIP 30d (15000⭐)",  callback_data="buy_vip")
    await message.answer("🛒 <b>Магазин FTCL</b>", reply_markup=kb.adjust(1).as_markup())


@dp.callback_query(F.data.startswith("buy_"))
async def handle_purchase(call: types.CallbackQuery):
    uid = call.from_user.id
    if uid in processing_users:
        return await call.answer("⏳ Подождите...", show_alert=True)
    processing_users.add(uid)
    sku        = call.data.split("_", 1)[1]
    price_list = {"bronze": 1200, "gold": 3500, "brilliant": 6700, "vip": 15000}
    if sku not in price_list:
        processing_users.discard(uid)
        return await call.answer("❌ Неизвестный товар.", show_alert=True)
    price = price_list[sku]
    try:
        async with db() as conn:
            await ensure_user(conn, uid, call.from_user.username)
            balance = await get_user_balance(conn, uid)
            if balance is None:
                return await call.answer("❌ Профиль не найден.", show_alert=True)
            if balance < price:
                return await call.answer("❌ Недостаточно ⭐!", show_alert=True)
            await conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ?", (price, uid)
            )
            await log_stars(conn, uid, -price, f"Покупка: {sku}")
            if sku == "vip":
                expiry = (datetime.now() + timedelta(days=30)).isoformat()
                await conn.execute(
                    "UPDATE users SET vip_until = ? WHERE user_id = ?", (expiry, uid)
                )
                await conn.commit()
                await call.message.answer("💎 VIP статус успешно активирован на 30 дней!")
            else:
                async with conn.execute(
                    f"SELECT * FROM all_cards WHERE {TIER_SQL[sku]} ORDER BY RANDOM() LIMIT 1"
                ) as cur:
                    card = await cur.fetchone()
                if not card:
                    await conn.execute(
                        "UPDATE users SET balance = balance + ? WHERE user_id = ?", (price, uid)
                    )
                    await log_stars(conn, uid, price, f"Возврат: {sku} не найден")
                    await conn.commit()
                    return await call.message.answer("❌ Карты этого типа временно недоступны. Деньги возвращены.")

                rarity_key = card["rarity_type"] if card["rarity_type"] in TIER_REWARDS else determine_tier(card["rating"])
                lo, hi     = TIER_REWARDS.get(rarity_key, (250, 500))
                grant      = random.randint(lo, hi)
                await conn.execute(
                    "INSERT INTO user_cards (user_id, card_id) VALUES (?, ?)", (uid, card["id"])
                )
                await conn.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?", (grant, uid)
                )
                await log_stars(conn, uid, grant, f"Магазин: {card['name']} ({rarity_key})")
                await conn.commit()
                await call.message.answer_photo(
                    card["photo_id"],
                    caption=(
                        f"📦 Получен игрок: <b>{card['name']}</b>!\n"
                        f"🏷 Тип: {card['rarity']}\n"
                        f"💰 Бонус: +{grant:,} ⭐"
                    )
                )
    finally:
        processing_users.discard(uid)
    await call.answer()


@dp.callback_query(F.data == "game_guess")
async def init_guess_game(call: types.CallbackQuery):
    uid = call.from_user.id
    async with db() as conn:
        await ensure_user(conn, uid, call.from_user.username)
        async with conn.execute(
            "SELECT last_guess FROM users WHERE user_id = ?", (uid,)
        ) as cur:
            row = await cur.fetchone()
        await conn.commit()
    remaining = get_guess_remaining(row["last_guess"] if row else None)
    if remaining > 0:
        h, m = remaining // 3600, (remaining % 3600) // 60
        return await call.answer(f"⏳ Перезарядка! Осталось {h}ч. {m}м.", show_alert=True)
    bet_waitlist[uid] = "guess"
    await _reset_notif(uid, "guess_notif")
    await call.message.edit_text("💰 Укажите размер ставки (лимит 30 000 ⭐):")


@dp.message(lambda msg: msg.from_user.id in bet_waitlist)
async def process_bet(message: types.Message):
    uid = message.from_user.id
    if not message.text or not message.text.isdigit():
        return
    amount = int(message.text)
    if amount <= 0:
        return await message.answer("❌ Ставка должна быть больше нуля.")
    if amount > 30000:
        return await message.answer("❌ Лимит ставки — 30 000 ⭐!")
    bet_waitlist.pop(uid, None)
    async with db() as conn:
        balance = await get_user_balance(conn, uid)
        if balance is None or balance < amount:
            return await message.answer("❌ У вас недостаточно средств.")
        async with conn.execute(
            "SELECT id, name, club, position, rating, rarity_type FROM all_cards ORDER BY RANDOM() LIMIT 4"
        ) as cur:
            options_raw = await cur.fetchall()
    if len(options_raw) < 2:
        return await message.answer("❌ Недостаточно карт в базе для игры.")
    target       = dict(options_raw[0])
    options_list = [dict(r) for r in options_raw]
    random.shuffle(options_list)
    rarity = target["rarity_type"] or determine_tier(target["rating"])
    guess_sessions[uid] = {
        "bet":         amount,
        "target_id":   target["id"],
        "target_info": target,
        "options":     options_list,
        "hints_used":  0,
        "card_rarity": rarity,
    }
    await message.answer(build_guess_text(guess_sessions[uid]), reply_markup=build_guess_keyboard(guess_sessions[uid]))


@dp.callback_query(F.data == "guess_hint_1")
async def guess_hint_rating(call: types.CallbackQuery):
    uid = call.from_user.id
    if uid not in guess_sessions:
        return await call.answer("Игра не найдена.", show_alert=True)
    s = guess_sessions[uid]
    if s["hints_used"] >= 1:
        return await call.answer("Уже использована.", show_alert=True)
    s["hints_used"] = 1
    await call.message.edit_text(build_guess_text(s), reply_markup=build_guess_keyboard(s))
    await call.answer()


@dp.callback_query(F.data == "guess_hint_2")
async def guess_hint_letter(call: types.CallbackQuery):
    uid = call.from_user.id
    if uid not in guess_sessions:
        return await call.answer("Игра не найдена.", show_alert=True)
    s = guess_sessions[uid]
    if s["hints_used"] < 1:
        return await call.answer("Сначала первая подсказка.", show_alert=True)
    if s["hints_used"] >= 2:
        return await call.answer("Уже использована.", show_alert=True)
    s["hints_used"] = 2
    await call.message.edit_text(build_guess_text(s), reply_markup=build_guess_keyboard(s))
    await call.answer()


@dp.callback_query(F.data == "guess_give_up")
async def guess_give_up(call: types.CallbackQuery):
    uid = call.from_user.id
    if uid not in guess_sessions:
        return await call.answer("Игра не найдена.", show_alert=True)
    s      = guess_sessions.pop(uid)
    target = s["target_info"]
    bet    = s["bet"]
    async with db() as conn:
        balance     = await get_user_balance(conn, uid)
        actual_loss = min(bet, balance or 0)
        await conn.execute(
            "UPDATE users SET balance = MAX(balance - ?, 0), last_guess = ? WHERE user_id = ?",
            (actual_loss, datetime.now().isoformat(), uid)
        )
        await log_stars(conn, uid, -actual_loss, f"Угадайка: сдался ({target['name']})")
        await conn.commit()
    await call.message.edit_text(
        f"🏳️ Ты сдался!\n\nПравильный ответ: <b>{target['name']}</b>\n"
        f"🛡 Клуб: {target['club']} | 📊 Рейтинг: {target['rating']}\n\n"
        f"💸 Потеряно: <b>{actual_loss:,} ⭐</b>"
    )
    await call.answer()


@dp.callback_query(F.data.startswith("guess_pick_"))
async def guess_pick(call: types.CallbackQuery):
    uid = call.from_user.id
    if uid not in guess_sessions:
        return await call.answer("Игра уже завершена.", show_alert=True)
    try:
        pick_id = int(call.data.split("_")[2])
    except (ValueError, IndexError):
        return
    s          = guess_sessions.pop(uid)
    is_correct = (pick_id == s["target_id"])
    bet        = s["bet"]
    hints_used = s["hints_used"]
    rarity     = s["card_rarity"]
    target     = s["target_info"]
    reward_key = ["max", "hint1", "hint2"][min(hints_used, 2)]
    reward     = GUESS_REWARD.get(rarity, GUESS_REWARD["bronze"])[reward_key]
    now_iso    = datetime.now().isoformat()
    async with db() as conn:
        if is_correct:
            await conn.execute(
                "UPDATE users SET balance = balance + ?, last_guess = ? WHERE user_id = ?",
                (reward, now_iso, uid)
            )
            await log_stars(conn, uid, reward, f"Угадайка: победа ({target['name']}, {hints_used} подск.)")
        else:
            balance     = await get_user_balance(conn, uid)
            actual_loss = min(bet, balance or 0)
            await conn.execute(
                "UPDATE users SET balance = MAX(balance - ?, 0), last_guess = ? WHERE user_id = ?",
                (actual_loss, now_iso, uid)
            )
            await log_stars(conn, uid, -actual_loss, f"Угадайка: проигрыш ({target['name']})")
        await conn.commit()
    if is_correct:
        hints_text = ["без подсказок 🎯", "с 1 подсказкой", "с 2 подсказками"][min(hints_used, 2)]
        await call.message.edit_text(
            f"✅ <b>Верно!</b> Это {target['name']}!\n"
            f"🛡 Клуб: {target['club']} | 📊 Рейтинг: {target['rating']}\n"
            f"🏆 Угадано {hints_text}\n\n"
            f"💰 Выигрыш: <b>+{reward:,} ⭐</b>"
        )
    else:
        await call.message.edit_text(
            f"❌ <b>Неверно!</b>\nПравильный ответ: <b>{target['name']}</b>\n"
            f"🛡 Клуб: {target['club']} | 📊 Рейтинг: {target['rating']}\n\n"
            f"💸 Потеряно: <b>{bet:,} ⭐</b>"
        )
    await call.answer()


@dp.callback_query(F.data == "game_penalty")
async def init_penalty_game(call: types.CallbackQuery):
    uid = call.from_user.id
    async with db() as conn:
        await ensure_user(conn, uid, call.from_user.username)
        async with conn.execute(
            "SELECT last_penalty, balance FROM users WHERE user_id = ?", (uid,)
        ) as cur:
            row = await cur.fetchone()
        async with conn.execute(
            "SELECT COUNT(*) FROM user_cards WHERE user_id = ?", (uid,)
        ) as cur:
            total_cards = (await cur.fetchone())[0]
        await conn.commit()
    remaining = get_penalty_remaining(row["last_penalty"] if row else None)
    if remaining > 0:
        h, m = remaining // 3600, (remaining % 3600) // 60
        return await call.answer(f"⏳ Перезарядка! Осталось {h}ч {m}м.", show_alert=True)
    if total_cards < 5:
        return await call.answer(
            f"❌ Нужно минимум 5 карт!\nУ тебя: {total_cards}", show_alert=True
        )
    penalty_bet_wait[uid] = True
    await _reset_notif(uid, "penalty_notif")
    await call.message.edit_text(
        "⚽ <b>Серия пенальти</b>\n\n"
        "💰 Введи размер ставки (1 — 30 000 ⭐):\n"
        "<i>При победе получишь ставку + 90% бонус.</i>"
    )
    await call.answer()


@dp.message(lambda m: m.from_user.id in penalty_bet_wait)
async def penalty_bet_input(message: types.Message):
    uid = message.from_user.id
    if not message.text or not message.text.strip().isdigit():
        return
    amount = int(message.text.strip())
    if amount <= 0:
        return await message.answer("❌ Ставка должна быть больше нуля.")
    if amount > 30000:
        return await message.answer("❌ Лимит ставки — 30 000 ⭐!")
    async with db() as conn:
        balance = await get_user_balance(conn, uid)
        if balance is None or balance < amount:
            return await message.answer("❌ Недостаточно ⭐ на балансе!")
        async with conn.execute(
            "SELECT DISTINCT c.id, c.name, c.rating, c.club, c.position "
            "FROM user_cards uc JOIN all_cards c ON uc.card_id = c.id "
            "WHERE uc.user_id = ? ORDER BY c.rating DESC LIMIT 20",
            (uid,)
        ) as cur:
            cards_raw = await cur.fetchall()
    penalty_bet_wait.pop(uid, None)
    penalty_team_sel[uid] = {"bet": amount, "selected": [], "cards": [dict(c) for c in cards_raw]}
    await _show_team_selector(message, uid)


async def _show_team_selector(msg_or_call, uid: int):
    data   = penalty_team_sel[uid]
    cards  = data["cards"]
    chosen = data["selected"]
    bet    = data["bet"]
    kb     = InlineKeyboardBuilder()
    for c in cards:
        tick = "✅ " if c["id"] in chosen else ""
        kb.button(text=f"{tick}{c['name']} ({c['rating']})", callback_data=f"pen_pick_{c['id']}")
    kb.adjust(2)
    if len(chosen) == 5:
        kb.button(text="✔️ Подтвердить состав", callback_data="pen_confirm")
    kb.button(text="🔄 Сбросить выбор", callback_data="pen_reset")
    chosen_names = ", ".join(
        next(c["name"] for c in cards if c["id"] == cid) for cid in chosen
    ) if chosen else "—"
    text = (
        f"⚽ <b>Серия пенальти</b>\n"
        f"💵 Ставка: <b>{bet:,} ⭐</b>\n\n"
        f"Выбери <b>5 игроков</b> ({len(chosen)}/5):\n<i>Выбрано: {chosen_names}</i>"
    )
    if isinstance(msg_or_call, types.Message):
        await msg_or_call.answer(text, reply_markup=kb.adjust(2).as_markup())
    else:
        await msg_or_call.message.edit_text(text, reply_markup=kb.adjust(2).as_markup())


@dp.callback_query(F.data.startswith("pen_pick_"))
async def penalty_pick_player(call: types.CallbackQuery):
    uid     = call.from_user.id
    card_id = int(call.data.split("_")[2])
    if uid not in penalty_team_sel:
        return await call.answer("Сессия истекла.", show_alert=True)
    data   = penalty_team_sel[uid]
    chosen = data["selected"]
    if card_id in chosen:
        chosen.remove(card_id)
    elif len(chosen) < 5:
        chosen.append(card_id)
    else:
        return await call.answer("Уже 5 игроков! Сними кого-то.", show_alert=True)
    await _show_team_selector(call, uid)
    await call.answer()


@dp.callback_query(F.data == "pen_reset")
async def penalty_reset(call: types.CallbackQuery):
    uid = call.from_user.id
    if uid in penalty_team_sel:
        penalty_team_sel[uid]["selected"] = []
    await _show_team_selector(call, uid)
    await call.answer()


@dp.callback_query(F.data == "pen_confirm")
async def penalty_confirm_team(call: types.CallbackQuery):
    uid = call.from_user.id
    if uid not in penalty_team_sel:
        return await call.answer("Сессия истекла.", show_alert=True)
    data   = penalty_team_sel.pop(uid)
    chosen = data["selected"]
    bet    = data["bet"]
    cards  = data["cards"]
    if len(chosen) != 5:
        return await call.answer("Выбери ровно 5 игроков!", show_alert=True)
    user_team = [c for c in cards if c["id"] in chosen]
    async with db() as conn:
        balance = await get_user_balance(conn, uid)
        if balance is None or balance < bet:
            return await call.answer("❌ Недостаточно ⭐!", show_alert=True)
        await conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ?", (bet, uid)
        )
        await log_stars(conn, uid, -bet, "Пенальти: ставка заморожена")
        user_ids_str = ",".join(str(c["id"]) for c in user_team)
        async with conn.execute(
            f"SELECT id, name, rating FROM all_cards "
            f"WHERE id NOT IN ({user_ids_str}) ORDER BY RANDOM() LIMIT 5"
        ) as cur:
            bot_cards_raw = await cur.fetchall()
        await conn.commit()
    bot_team  = [dict(c) for c in bot_cards_raw]
    user_avg  = avg_rating(user_team)
    bot_avg   = avg_rating(bot_team)
    underdog  = (bot_avg - user_avg) >= 5.0
    underdog_text = (
        f"\n\n🔥 <b>БОНУС АУТСАЙДЕРА!</b> Состав бота сильнее на {bot_avg - user_avg:.1f}.\n"
        f"В случае победы +{int(UNDERDOG_BONUS_PCT * 100)}% от ставки дополнительно!"
    ) if underdog else ""
    penalty_sessions[uid] = {
        "bet": bet, "user_team": user_team, "bot_team": bot_team,
        "user_avg": user_avg, "bot_avg": bot_avg, "underdog": underdog,
        "round": 0, "extra": False, "user_score": 0, "bot_score": 0,
        "phase": "user_kick", "user_corner": None, "bot_corners": None,
    }
    msg = await call.message.edit_text(
        f"⚽ <b>Серия пенальти начинается!</b>\n\n"
        f"👤 <b>Твой состав</b> (ср. рейтинг: {user_avg:.1f}):\n{_team_display(user_team)}\n\n"
        f"🤖 <b>Состав бота</b> (ср. рейтинг: {bot_avg:.1f}):\n{_team_display(bot_team)}"
        f"{underdog_text}"
    )
    await asyncio.sleep(3)
    await _send_penalty_kick(uid, msg)
    await call.answer()


async def _send_penalty_kick(uid: int, msg: types.Message):
    s = penalty_sessions.get(uid)
    if not s:
        return
    round_num = s["round"] + 1
    extra_tag = " (доп.)" if s["extra"] else f" (раунд {round_num}/5)"
    if s["phase"] == "user_kick":
        kb = InlineKeyboardBuilder()
        for key, label in zip(CORNER_KEYS, CORNERS):
            kb.button(text=label, callback_data=f"pen_kick_{key}")
        kb.adjust(3, 2)
        score_line = _penalty_score_line({**s, "round": max(s["round"], 1)})
        await msg.edit_text(
            f"⚽ <b>Твой удар</b>{extra_tag}\n\n{score_line}\n\n🥅 Выбери угол для удара:",
            reply_markup=kb.as_markup()
        )
    else:
        kb = InlineKeyboardBuilder()
        for key, label in zip(CORNER_KEYS, CORNERS):
            kb.button(text=label, callback_data=f"pen_save_{key}")
        kb.adjust(3, 2)
        bot_corner = random.choice(CORNER_KEYS)
        s["bot_corner_current"] = bot_corner
        score_line = _penalty_score_line({**s, "round": max(s["round"], 1)})
        await msg.edit_text(
            f"🤖 <b>Удар бота</b>{extra_tag}\n\n{score_line}\n\n🧤 Выбери <b>2 угла</b> для сейва:",
            reply_markup=kb.as_markup()
        )
        s["save_picks"] = []


@dp.callback_query(F.data.startswith("pen_kick_"))
async def penalty_user_kick(call: types.CallbackQuery):
    uid = call.from_user.id
    s   = penalty_sessions.get(uid)
    if not s or s["phase"] != "user_kick":
        return await call.answer("Сейчас не твой удар.", show_alert=True)
    corner       = call.data.split("_")[2]
    bot_saves    = bot_saves_corners(CORNER_KEYS)
    saved        = corner in bot_saves
    corner_label = CORNERS[CORNER_KEYS.index(corner)]
    save_labels  = " и ".join(CORNERS[CORNER_KEYS.index(k)] for k in bot_saves)
    await call.message.edit_text(
        f"⚽ Ты бьёшь в угол <b>{corner_label}</b>...\n"
        f"🧤 Вратарь бросается на <b>{save_labels}</b>...\n\n"
        f"{'🎉 ГОЛ!' if not saved else '⏳ Считаем...'}"
    )
    await asyncio.sleep(2)
    if not saved:
        s["user_score"] += 1
        result_text = f"⚽ <b>ГОЛ!</b> Ты пробил в {corner_label}, вратарь не достал!"
    else:
        result_text = f"🧤 <b>СЕЙВ!</b> Вратарь отразил в {corner_label}!"
    await call.message.edit_text(result_text)
    await asyncio.sleep(1.5)
    s["phase"] = "bot_kick"
    await _send_penalty_kick(uid, call.message)
    await call.answer()


@dp.callback_query(F.data.startswith("pen_save_"))
async def penalty_user_save(call: types.CallbackQuery):
    uid  = call.from_user.id
    s    = penalty_sessions.get(uid)
    if not s or s["phase"] != "bot_kick":
        return await call.answer("Сейчас не твоя защита.", show_alert=True)
    corner = call.data.split("_")[2]
    picks  = s.setdefault("save_picks", [])
    if corner in picks:
        return await call.answer("Этот угол уже выбран.", show_alert=True)
    picks.append(corner)
    if len(picks) < 2:
        kb = InlineKeyboardBuilder()
        for key, label in zip(CORNER_KEYS, CORNERS):
            tick = "🛡 " if key in picks else ""
            kb.button(text=f"{tick}{label}", callback_data=f"pen_save_{key}")
        kb.adjust(3, 2)
        score_line = _penalty_score_line({**s, "round": max(s["round"], 1)})
        await call.message.edit_text(
            f"🤖 <b>Удар бота</b>{'(доп.)' if s['extra'] else ''}\n\n{score_line}\n\n"
            f"🧤 Выбрано: <b>{CORNERS[CORNER_KEYS.index(picks[0])]}</b>. Выбери второй угол:",
            reply_markup=kb.as_markup()
        )
        await call.answer()
        return
    bot_corner   = s.pop("bot_corner_current", random.choice(CORNER_KEYS))
    saved        = bot_corner in picks
    corner_label = CORNERS[CORNER_KEYS.index(bot_corner)]
    save_labels  = " и ".join(CORNERS[CORNER_KEYS.index(k)] for k in picks)
    await call.message.edit_text(
        f"🤖 Бот бьёт в угол <b>{corner_label}</b>...\n"
        f"🧤 Ты бросаешься на <b>{save_labels}</b>...\n\n⏳ Считаем..."
    )
    await asyncio.sleep(2)
    if not saved:
        s["bot_score"] += 1
        result_text = f"😬 <b>ГОЛ бота!</b> Он пробил в {corner_label}, ты не достал!"
    else:
        result_text = f"🧤 <b>СЕЙВ!</b> Ты поймал мяч в {corner_label}!"
    await call.message.edit_text(result_text)
    await asyncio.sleep(1.5)
    s["round"] += 1
    s["phase"]  = "user_kick"
    s.pop("save_picks", None)
    rounds_done = s["round"]
    is_extra    = s["extra"]
    if not is_extra and rounds_done >= 5:
        if s["user_score"] != s["bot_score"]:
            await _finish_penalty(uid, call.message)
        else:
            s["extra"] = True
            await call.message.edit_text(
                f"⚡️ <b>Счёт равный {s['user_score']}:{s['bot_score']}!</b>\n\n"
                f"Дополнительные удары до разницы..."
            )
            await asyncio.sleep(2)
            await _send_penalty_kick(uid, call.message)
    elif is_extra:
        if s["user_score"] != s["bot_score"]:
            await _finish_penalty(uid, call.message)
        else:
            await call.message.edit_text(
                f"⚡️ <b>Снова равно {s['user_score']}:{s['bot_score']}!</b> Ещё раунд..."
            )
            await asyncio.sleep(1.5)
            await _send_penalty_kick(uid, call.message)
    else:
        await _send_penalty_kick(uid, call.message)
    await call.answer()


async def _finish_penalty(uid: int, msg: types.Message):
    s = penalty_sessions.pop(uid, None)
    if not s:
        return
    user_score    = s["user_score"]
    bot_score     = s["bot_score"]
    bet           = s["bet"]
    underdog      = s["underdog"]
    user_avg      = s["user_avg"]
    bot_avg       = s["bot_avg"]
    won           = user_score > bot_score
    underdog_bonus = int(bet * UNDERDOG_BONUS_PCT) if underdog else 0
    win_bonus      = int(bet * PENALTY_WIN_BONUS_PCT) if won else 0
    total_payout   = (bet + win_bonus + underdog_bonus) if won else underdog_bonus
    async with db() as conn:
        await conn.execute(
            "UPDATE users SET balance = balance + ?, last_penalty = ? WHERE user_id = ?",
            (total_payout, datetime.now().isoformat(), uid)
        )
        await log_stars(conn, uid, total_payout - bet,
                        f"Пенальти: {'победа' if won else 'поражение'} {user_score}:{bot_score}")
        await conn.commit()
    await msg.edit_text(f"{'🏆' if won else '💔'} <b>Серия завершена!</b>\n\nСчёт: <b>{user_score}:{bot_score}</b>")
    await asyncio.sleep(2)
    lines = [f"{'🏆 ПОБЕДА!' if won else '💔 Поражение'}", "",
             f"📊 Счёт: <b>{user_score} : {bot_score}</b>",
             f"👤 Твой состав: {user_avg:.1f} | 🤖 Бот: {bot_avg:.1f}", ""]
    if won:
        lines += [f"💵 Ставка возвращена: <b>{bet:,} ⭐</b>",
                  f"🏅 Бонус победителя (+90%): <b>+{win_bonus:,} ⭐</b>"]
        if underdog_bonus:
            lines.append(f"🔥 Бонус аутсайдера (+40%): <b>+{underdog_bonus:,} ⭐</b>")
        lines.append(f"💰 <b>Итого: +{total_payout:,} ⭐</b>")
    else:
        lines.append(f"💸 Ставка потеряна: <b>{bet:,} ⭐</b>")
        if underdog_bonus:
            lines += [f"🎖 Бонус аутсайдера (+40%): <b>+{underdog_bonus:,} ⭐</b>",
                      f"💰 Выплачено: <b>+{underdog_bonus:,} ⭐</b>"]
        else:
            lines.append("💰 Выплата: <b>0 ⭐</b>")
    await msg.edit_text("\n".join(lines))


@dp.callback_query(F.data.startswith("vcoll_"))
async def show_collection(call: types.CallbackQuery):
    idx = int(call.data.split("_", 1)[1])
    uid = call.from_user.id
    async with db() as conn:
        async with conn.execute(
            "SELECT c.name, c.rating FROM user_cards uc "
            "JOIN all_cards c ON uc.card_id = c.id "
            "WHERE uc.user_id = ? ORDER BY c.rating DESC LIMIT 15 OFFSET ?",
            (uid, idx * 15)
        ) as cur:
            items = await cur.fetchall()
        async with conn.execute(
            "SELECT COUNT(*) FROM user_cards WHERE user_id = ?", (uid,)
        ) as cur:
            total_count = (await cur.fetchone())[0]
    if not items:
        return await call.message.edit_text("💼 Твоя коллекция пуста.")
    output  = f"💼 <b>Твоя коллекция (стр. {idx + 1}):</b>\n\n"
    output += "\n".join([f"▪️ {r[0]} — <b>{r[1]}</b>" for r in items])
    nav = InlineKeyboardBuilder()
    if idx > 0:
        nav.button(text="⬅️ Назад", callback_data=f"vcoll_{idx - 1}")
    if total_count > (idx + 1) * 15:
        nav.button(text="Вперед ➡️", callback_data=f"vcoll_{idx + 1}")
    await call.message.edit_text(output, reply_markup=nav.adjust(2).as_markup())


@dp.message(F.text == "Профиль 👤")
async def show_profile(message: types.Message):
    uid = message.from_user.id
    async with db() as conn:
        async with conn.execute(
            "SELECT balance, username, vip_until, is_banned FROM users WHERE user_id = ?", (uid,)
        ) as cur:
            data = await cur.fetchone()
    if not data:
        return await message.answer("❌ Профиль не найден. Напиши /start.")
    if data["is_banned"]:
        return await message.answer("🚫 Ваш аккаунт заблокирован.")
    username = data["username"] or "Игрок"
    vip_dt   = parse_dt(data["vip_until"])
    has_vip  = bool(vip_dt and vip_dt > datetime.now())
    kb = InlineKeyboardBuilder()
    kb.button(text="💼 Коллекция", callback_data="vcoll_0")
    await message.answer(
        f"👤 <b>Профиль @{username}</b>\n"
        f"💰 Баланс: {data['balance']:,} ⭐\n"
        f"💎 VIP: {'Активен ✅' if has_vip else 'Нет ❌'}",
        reply_markup=kb.as_markup()
    )


@dp.message(F.text == "Рефералка 👥")
async def invite_link(message: types.Message):
    uid      = message.from_user.id
    bot_info = await bot.get_me()
    link     = f"t.me/{bot_info.username}?start={uid}"
    async with db() as conn:
        async with conn.execute(
            "SELECT "
            "  SUM(CASE WHEN revoked=0 AND bonus_paid_at IS NOT NULL THEN 1 ELSE 0 END) AS paid, "
            "  SUM(CASE WHEN revoked=0 AND subscribed_at IS NOT NULL AND bonus_paid_at IS NULL THEN 1 ELSE 0 END) AS pending, "
            "  SUM(CASE WHEN revoked=0 AND subscribed_at IS NULL THEN 1 ELSE 0 END) AS waiting_sub, "
            "  SUM(CASE WHEN revoked=1 THEN 1 ELSE 0 END) AS revoked "
            "FROM referral_checks WHERE inviter_id = ?",
            (uid,)
        ) as cur:
            stats = await cur.fetchone()
    paid    = (stats["paid"]        or 0) if stats else 0
    pending = (stats["pending"]     or 0) if stats else 0
    waiting = (stats["waiting_sub"] or 0) if stats else 0
    revoked = (stats["revoked"]     or 0) if stats else 0
    await message.answer(
        f"👥 <b>Реферальная программа</b>\n\n"
        f"Приглашай друзей и получай <b>{REF_BONUS_AMOUNT:,} ⭐</b> за каждого!\n\n"
        f"<b>Как это работает:</b>\n"
        f"1. Отправь ссылку другу\n"
        f"2. Друг подписывается на все каналы и подтверждает\n"
        f"3. Через {REF_CHECK_DAYS} дней ты получаешь бонус\n"
        f"4. Если друг отпишется — бонус сгорает\n\n"
        f"📊 Твоя статистика:\n"
        f"  ⚠️ Ожидают подписки: <b>{waiting}</b>\n"
        f"  ⏳ На проверке: <b>{pending}</b>\n"
        f"  ✅ Подтверждено: <b>{paid}</b>\n"
        f"  🔥 Сгорело: <b>{revoked}</b>\n\n"
        f"Твоя ссылка:\n<code>{link}</code>"
    )


@dp.message(F.text == "ТОП-10 📊")
async def leaderboard(message: types.Message):
    async with db() as conn:
        async with conn.execute(
            "SELECT COALESCE(username, 'Игрок'), balance FROM users "
            "ORDER BY balance DESC LIMIT 10"
        ) as cur:
            rows = await cur.fetchall()
    content  = "🏆 <b>Рейтинг лучших:</b>\n\n"
    content += "\n".join([f"{i + 1}. {r[0]} — {r[1]:,} ⭐" for i, r in enumerate(rows)])
    await message.answer(content)


@dp.callback_query(F.data == "check_subs")
async def verify_subs(call: types.CallbackQuery):
    remaining = await get_subs_status(call.from_user.id)
    if not remaining:
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.answer("✅ Подписки подтверждены!", show_alert=True)
        await _claim_card_flow(call)
    else:
        await call.answer("❌ Подписка оформлена не на все каналы!", show_alert=True)


@dp.message(Command("suggest"))
async def suggest_cmd(message: types.Message):
    player_suggest_sessions[message.from_user.id] = {"step": "suggest_name"}
    await message.answer(
        "📝 <b>Заявка в модерацию FTCL CARDS</b>\n\n"
        "Помоги пополнить базу и получи <b>500 ⭐</b> за принятую заявку!\n\n"
        "Шаг 1/5 — Введи ИМЯ игрока КАПСОМ:\n"
        "<i>Пример: МЕССИ</i>"
    )


@dp.message(F.text == "Поиск игрока 🔍")
async def player_search_menu(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 По имени",    callback_data="psearch_mode_name")
    kb.button(text="🛡 По клубу",    callback_data="psearch_mode_club")
    kb.button(text="📊 По рейтингу", callback_data="psearch_mode_rating")
    kb.adjust(1)
    await message.answer(
        "🔍 <b>Поиск игрока</b>\n\nВыбери способ поиска:",
        reply_markup=kb.as_markup()
    )


@dp.callback_query(F.data.startswith("psearch_mode_"))
async def psearch_mode(call: types.CallbackQuery):
    mode = call.data.split("_")[2]
    prompts = {
        "name":   "👤 Введи имя игрока (КАПС или часть):",
        "club":   "🛡 Введи название клуба:",
        "rating": "📊 Введи рейтинг (число от 50 до 99):",
    }
    player_suggest_sessions[call.from_user.id] = {"step": f"search_{mode}"}
    await call.message.edit_text(prompts[mode])
    await call.answer()


@dp.message(lambda msg: msg.from_user.id in player_suggest_sessions
            and player_suggest_sessions[msg.from_user.id].get("step", "").startswith("search_"))
async def psearch_input(message: types.Message):
    uid     = message.from_user.id
    session = player_suggest_sessions.get(uid, {})
    step    = session.get("step", "")
    text    = (message.text or "").strip()
    cards   = []
    label   = ""

    if step == "search_name":
        query = f"%{text}%"
        async with db() as conn:
            async with conn.execute(
                "SELECT id, name, rating, club, position, rarity FROM all_cards "
                "WHERE UPPER(name) LIKE UPPER(?) ORDER BY rating DESC LIMIT 20",
                (query,)
            ) as cur:
                cards = await cur.fetchall()
        label = f"по имени «{text}»"
    elif step == "search_club":
        query = f"%{text}%"
        async with db() as conn:
            async with conn.execute(
                "SELECT id, name, rating, club, position, rarity FROM all_cards "
                "WHERE UPPER(club) LIKE UPPER(?) ORDER BY rating DESC LIMIT 20",
                (query,)
            ) as cur:
                cards = await cur.fetchall()
        label = f"по клубу «{text}»"
    elif step == "search_rating":
        if not text.isdigit() or not (50 <= int(text) <= 99):
            return await message.answer("❌ Введи число от 50 до 99.")
        async with db() as conn:
            async with conn.execute(
                "SELECT id, name, rating, club, position, rarity FROM all_cards "
                "WHERE rating = ? ORDER BY name LIMIT 20",
                (int(text),)
            ) as cur:
                cards = await cur.fetchall()
        label = f"с рейтингом {text}"
    else:
        player_suggest_sessions.pop(uid, None)
        return

    player_suggest_sessions.pop(uid, None)

    if not cards:
        kb = InlineKeyboardBuilder()
        kb.button(text="📝 Предложить игрока модерации", callback_data="psuggest_start")
        kb.button(text="🔍 Новый поиск", callback_data="psearch_back")
        kb.adjust(1)
        return await message.answer(
            f"❌ Игроки {label} не найдены.\n\n"
            "Хочешь помочь модерации FTCL CARDS и добавить игрока?",
            reply_markup=kb.as_markup()
        )

    kb = InlineKeyboardBuilder()
    for c in cards:
        kb.button(
            text=f"{c['name']} | {c['rating']} ⭐ | {c['club']}",
            callback_data=f"pview_{c['id']}"
        )
    kb.button(text="📝 Не нашёл — предложить", callback_data="psuggest_start")
    kb.adjust(1)
    await message.answer(
        f"🔍 <b>ПОИСК</b> — найдено {len(cards)} {label}:\n\nВыбери игрока:",
        reply_markup=kb.as_markup()
    )


@dp.callback_query(F.data == "psearch_back")
async def psearch_back(call: types.CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 По имени",    callback_data="psearch_mode_name")
    kb.button(text="🛡 По клубу",    callback_data="psearch_mode_club")
    kb.button(text="📊 По рейтингу", callback_data="psearch_mode_rating")
    kb.adjust(1)
    await call.message.edit_text(
        "🔍 <b>Поиск игрока</b>\n\nВыбери способ поиска:",
        reply_markup=kb.as_markup()
    )
    await call.answer()


@dp.callback_query(F.data.startswith("pview_"))
async def pview_card(call: types.CallbackQuery):
    card_id = int(call.data.split("_")[1])
    async with db() as conn:
        async with conn.execute(
            "SELECT * FROM all_cards WHERE id = ?", (card_id,)
        ) as cur:
            card = await cur.fetchone()
    if not card:
        return await call.answer("❌ Карта не найдена.", show_alert=True)

    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Новый поиск", callback_data="psearch_back")
    kb.adjust(1)

    caption = (
        "🔍 <b>ПОИСК</b>\n\n"
        f"👤 <b>{card['name']}</b>\n"
        f"📊 Рейтинг: <b>{card['rating']}</b>\n"
        f"🛡 Клуб: <b>{card['club']}</b>\n"
        f"📍 Позиция: <b>{card['position']}</b>\n"
        f"🏷 Тир: <b>{card['rarity']}</b>"
    )

    await call.message.delete()
    await call.message.answer_photo(
        card["photo_id"],
        caption=caption,
        reply_markup=kb.as_markup()
    )
    await call.answer()


@dp.callback_query(F.data == "psuggest_start")
async def psuggest_start(call: types.CallbackQuery):
    player_suggest_sessions[call.from_user.id] = {"step": "suggest_name"}
    await call.message.edit_text(
        "📝 <b>Заявка в модерацию FTCL CARDS</b>\n\n"
        "Шаг 1/5 — Введи ИМЯ игрока КАПСОМ:\n"
        "<i>Пример: МЕССИ</i>"
    )
    await call.answer()


@dp.message(lambda msg: msg.from_user.id in player_suggest_sessions
            and player_suggest_sessions[msg.from_user.id].get("step", "").startswith("suggest_"))
async def psuggest_input(message: types.Message):
    uid     = message.from_user.id
    session = player_suggest_sessions.get(uid, {})
    step    = session.get("step", "")

    if step == "suggest_name":
        name = (message.text or "").strip()
        if not name:
            return await message.answer("❌ Имя не может быть пустым.")
        session["name"] = name
        session["step"] = "suggest_rating"
        await message.answer(
            f"✅ Имя: <b>{name}</b>\n\n"
            "Шаг 2/5 — Введи РЕЙТИНГ игрока (число от 50 до 99):"
        )

    elif step == "suggest_rating":
        text = (message.text or "").strip()
        if not text.isdigit() or not (50 <= int(text) <= 99):
            return await message.answer("❌ Рейтинг должен быть числом от 50 до 99.")
        session["rating"] = int(text)
        session["step"]   = "suggest_club"
        await message.answer(
            f"✅ Рейтинг: <b>{text}</b>\n\n"
            "Шаг 3/5 — Введи КЛУБ игрока:"
        )

    elif step == "suggest_club":
        club = (message.text or "").strip()
        if not club:
            return await message.answer("❌ Клуб не может быть пустым.")
        session["club"] = club
        session["step"] = "suggest_position"
        kb = InlineKeyboardBuilder()
        for pos in ["Нападающий", "Полузащитник", "Защитник", "Вратарь"]:
            kb.button(text=pos, callback_data=f"psuggest_pos_{pos}")
        kb.adjust(2)
        await message.answer(
            f"✅ Клуб: <b>{club}</b>\n\n"
            "Шаг 4/5 — Выбери ПОЗИЦИЮ:",
            reply_markup=kb.as_markup()
        )

    elif step == "suggest_photo":
        is_doc = bool(
            message.document
            and message.document.mime_type
            and message.document.mime_type.startswith("image")
        )
        if not is_doc:
            return await message.answer(
                "❌ Нужно отправить фото <b>файлом</b>, а не как обычное фото!\n\n"
                "Как отправить файлом:\n"
                "1. Нажми скрепку 📎\n"
                "2. Выбери «Файл» (не «Фото»)\n"
                "3. Найди фото игрока и отправь\n\n"
                "⚠️ Фото должно быть чётким, лицо игрока <b>прямо в камеру</b>."
            )

        file_id  = message.document.file_id
        uname    = message.from_user.username or str(uid)
        name     = session["name"]
        rating   = session["rating"]
        club     = session["club"]
        position = session["position"]
        add_cmd  = f"/add_player {name} | {rating} | {club} | {position}"

        player_suggest_sessions.pop(uid, None)

        caption_mod = (
            "📋 <b>Заявка на добавление игрока</b>\n\n"
            f"От: @{uname} (ID: <code>{uid}</code>)\n\n"
            f"👤 Имя: <b>{name}</b>\n"
            f"📊 Рейтинг: <b>{rating}</b>\n"
            f"🛡 Клуб: <b>{club}</b>\n"
            f"📍 Позиция: <b>{position}</b>"
        )

        kb_mod = InlineKeyboardBuilder()
        kb_mod.button(text="✅ Подтвердить (+500⭐ автору)", callback_data=f"psuggest_approve_{uid}")
        kb_mod.button(text="👨‍💻 Команда для добавления",    callback_data=f"psuggest_getcmd_{uid}")
        kb_mod.adjust(1)

        player_suggest_sessions[f"cmd_{uid}"] = add_cmd

        try:
            await bot.send_document(
                MODERATION_GROUP_ID,
                document=file_id,
                caption=caption_mod,
                reply_markup=kb_mod.as_markup()
            )
        except Exception as e:
            log.error("Не удалось отправить заявку в группу: %s", e)
            await message.answer(f"❌ Ошибка при отправке заявки: {e}")
            return

        await message.answer(
            "✅ <b>Заявка отправлена!</b>\n\n"
            "Спасибо за помощь модерации FTCL CARDS!\n"
            "Если заявка будет одобрена — тебе начислят <b>500 ⭐</b>."
        )


@dp.callback_query(F.data.startswith("psuggest_pos_"))
async def psuggest_position(call: types.CallbackQuery):
    uid      = call.from_user.id
    position = call.data[len("psuggest_pos_"):]
    session  = player_suggest_sessions.get(uid)
    if not session or session.get("step") != "suggest_position":
        return await call.answer("❌ Сессия не найдена. Начни заново.", show_alert=True)
    session["position"] = position
    session["step"]     = "suggest_photo"
    await call.message.edit_text(
        f"✅ Позиция: <b>{position}</b>\n\n"
        "Шаг 5/5 — Отправь фото игрока <b>файлом</b>\n\n"
        "⚠️ Требования к фото:\n"
        "• Лицо игрока <b>прямо в камеру</b> (анфас)\n"
        "• Фото чёткое, хорошего качества\n\n"
        "Как отправить файлом:\n"
        "1. Нажми скрепку 📎\n"
        "2. Выбери <b>«Файл»</b> (не «Фото»)\n"
        "3. Найди фото и отправь"
    )
    await call.answer()


@dp.callback_query(F.data.startswith("psuggest_approve_"))
async def psuggest_approve(call: types.CallbackQuery):
    pid = call.from_user.id
    ok  = (pid == ADMIN_ID)
    if not ok:
        try:
            m = await bot.get_chat_member(MODERATION_GROUP_ID, pid)
            ok = m.status in ("administrator", "creator")
        except Exception:
            pass
    if not ok:
        return await call.answer("❌ Только администратор может одобрять заявки.", show_alert=True)
    try:
        author_uid = int(call.data.split("_")[2])
    except (ValueError, IndexError):
        return await call.answer("❌ Ошибка данных.", show_alert=True)

    async with db() as conn:
        await conn.execute(
            "UPDATE users SET balance = balance + 500 WHERE user_id = ?", (author_uid,)
        )
        await log_stars(conn, author_uid, 500, "Заявка на игрока одобрена модерацией")
        await conn.commit()

    try:
        await bot.send_message(
            author_uid,
            "🎉 <b>Твоя заявка одобрена модерацией!</b>\n\n"
            "Спасибо за помощь FTCL CARDS!\n"
            "💰 Тебе начислено <b>+500 ⭐</b>"
        )
    except Exception:
        pass

    approver = call.from_user.username or str(call.from_user.id)
    new_cap  = (call.message.caption or "") + f"\n\n✅ Одобрено @{approver}. Автору выдано 500 ⭐."
    try:
        await call.message.edit_caption(new_cap)
    except Exception:
        pass
    await call.answer("✅ Игрок одобрен, автору выдано 500 ⭐!", show_alert=True)


@dp.callback_query(F.data.startswith("psuggest_getcmd_"))
async def psuggest_getcmd(call: types.CallbackQuery):
    pid = call.from_user.id
    ok  = (pid == ADMIN_ID)
    if not ok:
        try:
            m = await bot.get_chat_member(MODERATION_GROUP_ID, pid)
            ok = m.status in ("administrator", "creator")
        except Exception:
            pass
    if not ok:
        return await call.answer("❌ Только администратор.", show_alert=True)
    try:
        author_uid = int(call.data.split("_")[2])
    except (ValueError, IndexError):
        return await call.answer("❌ Ошибка.", show_alert=True)

    add_cmd = player_suggest_sessions.get(f"cmd_{author_uid}", "Команда не найдена (истёк срок хранения)")

    await bot.send_message(
        call.from_user.id,
        f"👨‍💻 <b>Команда для добавления игрока:</b>\n\n"
        f"<code>{add_cmd}</code>\n\n"
        "<i>Нажми на текст чтобы скопировать, затем отправь боту вместе с фото игрока.</i>"
    )
    await call.answer("Команда отправлена тебе в личку!", show_alert=True)


def _pack_cancel_kb() -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отменить создание пака", callback_data="pack_create_cancel")
    return kb.as_markup()

def _fmt_cooldown(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if m == 0: return f"{h} ч."
    return f"{h} ч. {m} мин."

def _pack_is_active(pack) -> bool:
    if not pack["is_active"]: return False
    exp = parse_dt(pack["expires_at"])
    return bool(exp and exp > datetime.now())

async def _check_pack_sub(user_id: int, channel: str) -> bool:
    try:
        status = await bot.get_chat_member(channel, user_id)
        return status.status in ("member", "administrator", "creator")
    except Exception:
        return False

async def _get_pack_cooldown_remaining(user_id: int, pack_id: int, cooldown_sec: int) -> int:
    async with db() as conn:
        async with conn.execute(
            "SELECT opened_at FROM user_pack_opens "
            "WHERE user_id = ? AND pack_id = ? ORDER BY id DESC LIMIT 1",
            (user_id, pack_id)
        ) as cur:
            row = await cur.fetchone()
    if not row: return 0
    dt = parse_dt(row["opened_at"])
    if not dt: return 0
    return max(0, int(cooldown_sec - (datetime.now() - dt).total_seconds()))


@dp.message(F.text == "Спец. Паки 🎁")
async def special_packs_menu(message: types.Message):
    async with db() as conn:
        async with conn.execute(
            "SELECT id, title, description, channel, channel_url, expires_at, "
            "       cooldown_sec, unique_chance, banner_photo, "
            "       COALESCE(is_active, 1) AS is_active "
            "FROM special_packs WHERE is_active = 1 ORDER BY id DESC"
        ) as cur:
            packs = await cur.fetchall()

    active = [p for p in packs if _pack_is_active(p)]

    if not active:
        return await message.answer(
            "🎁 <b>Специальные паки</b>\n\n"
            "На данный момент активных паков нет.\n"
            "Следи за обновлениями!"
        )

    kb = InlineKeyboardBuilder()
    for p in active:
        exp    = parse_dt(p["expires_at"])
        days   = max(0, (exp - datetime.now()).days) if exp else 0
        kb.button(text=f"🎁 {p['title']} (ещё {days} дн.)", callback_data=f"sp_view_{p['id']}")
    kb.adjust(1)
    await message.answer(
        f"🎁 <b>Специальные паки</b>\n\n"
        f"Доступно паков: <b>{len(active)}</b>\n"
        f"Выбери пак чтобы узнать подробности:",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data.startswith("sp_view_"))
async def sp_view_pack(call: types.CallbackQuery):
    pack_id = int(call.data.split("_")[2])
    uid     = call.from_user.id

    async with db() as conn:
        async with conn.execute(
            "SELECT * FROM special_packs WHERE id = ?", (pack_id,)
        ) as cur:
            pack = await cur.fetchone()
        if not pack or not _pack_is_active(pack):
            return await call.answer("❌ Пак недоступен.", show_alert=True)

        async with conn.execute(
            "SELECT COUNT(*) FROM special_pack_cards WHERE pack_id = ?", (pack_id,)
        ) as cur:
            total_cards = (await cur.fetchone())[0]
        async with conn.execute(
            "SELECT COUNT(*) FROM special_pack_cards WHERE pack_id = ? AND is_unique = 1", (pack_id,)
        ) as cur:
            unique_cards = (await cur.fetchone())[0]

    exp      = parse_dt(pack["expires_at"])
    days_left = max(0, (exp - datetime.now()).days) if exp else 0
    remaining = await _get_pack_cooldown_remaining(uid, pack_id, pack["cooldown_sec"])

    if remaining > 0:
        h, m   = remaining // 3600, (remaining % 3600) // 60
        cd_str = f"⏳ Следующее открытие через: <b>{h}ч. {m}м.</b>"
    else:
        cd_str = "✅ Можно открыть прямо сейчас!"

    text = (
        f"🎁 <b>{pack['title']}</b>\n\n"
        f"{pack['description'] or ''}\n\n"
        f"📋 <b>Информация о паке:</b>\n"
        f"  📅 Доступен ещё: <b>{days_left} дн.</b>\n"
        f"  🔁 Кулдаун: <b>{_fmt_cooldown(pack['cooldown_sec'])}</b>\n"
        f"  🃏 Карт в пуле: <b>{total_cards}</b> (из них уникальных: {unique_cards})\n"
        f"  ✨ Шанс уникальной: <b>{pack['unique_chance']}%</b>\n"
        f"  📢 Требует подписки: <b>{pack['channel']}</b>\n\n"
        f"{cd_str}"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text=f"📢 Подписаться на {pack['channel']}", url=pack["channel_url"])
    if remaining == 0:
        kb.button(text="🎁 Открыть пак!", callback_data=f"sp_open_{pack_id}")
    kb.button(text="◀️ Назад", callback_data="sp_back")
    kb.adjust(1)

    try:
        if pack["banner_photo"]:
            await call.message.delete()
            await call.message.answer_photo(
                pack["banner_photo"], caption=text, reply_markup=kb.as_markup()
            )
        else:
            await call.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await call.message.answer(text, reply_markup=kb.as_markup())
    await call.answer()

@dp.callback_query(F.data == "sp_back")
async def sp_back(call: types.CallbackQuery):
    await call.message.delete()
    await special_packs_menu(call.message)
    await call.answer()


@dp.callback_query(F.data.startswith("sp_open_"))
async def sp_open_pack(call: types.CallbackQuery):
    pack_id = int(call.data.split("_")[2])
    uid     = call.from_user.id

    async with db() as conn:
        async with conn.execute("SELECT * FROM special_packs WHERE id = ?", (pack_id,)) as cur:
            pack = await cur.fetchone()

    if not pack or not _pack_is_active(pack):
        return await call.answer("❌ Пак больше недоступен.", show_alert=True)

    remaining = await _get_pack_cooldown_remaining(uid, pack_id, pack["cooldown_sec"])
    if remaining > 0:
        h, m = remaining // 3600, (remaining % 3600) // 60
        return await call.answer(f"⏳ Подожди ещё {h}ч. {m}м.", show_alert=True)

    is_subbed = await _check_pack_sub(uid, pack["channel"])
    if not is_subbed:
        kb = InlineKeyboardBuilder()
        kb.button(text=f"📢 Подписаться: {pack['channel']}", url=pack["channel_url"])
        kb.button(text="✅ Я подписался", callback_data=f"sp_recheck_{pack_id}")
        kb.adjust(1)
        return await call.message.edit_text(
            f"❌ <b>Нужна подписка на {pack['channel']}</b>\n\n"
            f"Подпишись и нажми проверку!",
            reply_markup=kb.as_markup()
        )

    async with db() as conn:
        async with conn.execute(
            "SELECT spc.card_id, spc.is_unique, c.name, c.rating, c.club, "
            "       c.position, c.rarity, c.photo_id "
            "FROM special_pack_cards spc "
            "JOIN all_cards c ON c.id = spc.card_id "
            "WHERE spc.pack_id = ?",
            (pack_id,)
        ) as cur:
            cards = await cur.fetchall()

    if not cards:
        return await call.answer("❌ В паке нет карт.", show_alert=True)

    unique_cards  = [c for c in cards if c["is_unique"]]
    regular_cards = [c for c in cards if not c["is_unique"]]

    roll      = random.randint(1, 100)
    got_unique = roll <= pack["unique_chance"] and unique_cards
    if got_unique:
        card = random.choice(unique_cards)
    elif regular_cards:
        card = random.choice(regular_cards)
    else:
        card = random.choice(cards)  # фолбэк

    async with db() as conn:
        await conn.execute(
            "INSERT INTO user_pack_opens (user_id, pack_id, opened_at) VALUES (?, ?, ?)",
            (uid, pack_id, datetime.now().isoformat())
        )
        await conn.execute(
            "INSERT INTO user_cards (user_id, card_id) VALUES (?, ?)",
            (uid, card["card_id"])
        )
        await conn.commit()

    chat_id = call.message.chat.id

    try:
        await call.message.delete()
    except Exception:
        pass

    loading = await bot.send_message(chat_id, "🎁 Открываем пак...")
    await asyncio.sleep(1.5)
    try:
        await loading.edit_text("✨ Достаём карту...")
    except Exception:
        pass
    await asyncio.sleep(1.5)
    try:
        await loading.delete()
    except Exception:
        pass

    unique_tag = "✨ <b>УНИКАЛЬНАЯ КАРТА!</b>\n" if got_unique else ""
    caption = (
        f"{unique_tag}"
        f"🎁 Из пака: <b>{pack['title']}</b>\n\n"
        f"👤 <b>{card['name']}</b>\n"
        f"📊 Рейтинг: {card['rating']}\n"
        f"🛡 Клуб: {card['club']}\n"
        f"📍 Позиция: {card['position']}\n"
        f"🏷 Тип: {card['rarity']}"
    )
    if card["photo_id"]:
        await bot.send_photo(chat_id, photo=card["photo_id"], caption=caption)
    else:
        await bot.send_message(chat_id, caption)
    await call.answer()

@dp.callback_query(F.data.startswith("sp_recheck_"))
async def sp_recheck_sub(call: types.CallbackQuery):
    pack_id = int(call.data.split("_")[2])
    uid     = call.from_user.id
    async with db() as conn:
        async with conn.execute("SELECT channel FROM special_packs WHERE id = ?", (pack_id,)) as cur:
            pack = await cur.fetchone()
    if not pack:
        return await call.answer("❌ Пак не найден.", show_alert=True)
    if await _check_pack_sub(uid, pack["channel"]):
        await call.answer("✅ Подписка подтверждена!", show_alert=True)
        call.data = f"sp_open_{pack_id}"
        await sp_open_pack(call)
    else:
        await call.answer("❌ Ты ещё не подписан!", show_alert=True)


@dp.message(Command("createpack"), F.from_user.id == ADMIN_ID)
async def cmd_createpack(message: types.Message):
    pack_create_sessions[message.from_user.id] = {"step": "title", "unique_cards": []}
    await message.answer(
        "📦 <b>Создание специального пака</b>\n\n"
        "Шаг 1/8 — Введи <b>название</b> пака:\n"
        "<i>Пример: Летний пак 2025</i>",
        reply_markup=_pack_cancel_kb()
    )

@dp.callback_query(F.data == "pack_create_cancel")
async def pack_create_cancel(call: types.CallbackQuery):
    pack_create_sessions.pop(call.from_user.id, None)
    await call.message.edit_text("❌ Создание пака отменено.")
    await call.answer()

@dp.message(
    F.from_user.id == ADMIN_ID,
    lambda m: m.from_user.id in pack_create_sessions
              and pack_create_sessions[m.from_user.id].get("step") not in (None, "add_unique")
)
async def pack_create_input(message: types.Message):
    uid     = message.from_user.id
    session = pack_create_sessions[uid]
    step    = session["step"]

    if message.text and message.text.startswith("/") and message.text != "/createpack":
        pack_create_sessions.pop(uid, None)
        return await message.answer("❌ Создание пака отменено — ты использовал команду.")

    if step == "title":
        session["title"] = (message.text or "").strip()
        session["step"]  = "description"
        await message.answer(
            f"✅ Название: <b>{session['title']}</b>\n\n"
            "Шаг 2/8 — Введи <b>описание</b> пака\n"
            "<i>(или отправь - чтобы пропустить)</i>",
            reply_markup=_pack_cancel_kb()
        )

    elif step == "description":
        text = (message.text or "").strip()
        session["description"] = "" if text == "-" else text
        session["step"]        = "channel"
        await message.answer(
            "Шаг 3/8 — Введи <b>@тег канала</b> для проверки подписки:\n"
            "<i>Пример: @ftcl_summer</i>",
            reply_markup=_pack_cancel_kb()
        )

    elif step == "channel":
        channel = (message.text or "").strip()
        if not channel.startswith("@"):
            return await message.answer("❌ Канал должен начинаться с @", reply_markup=_pack_cancel_kb())
        session["channel"] = channel
        session["step"]    = "channel_url"
        await message.answer(
            f"✅ Канал: <b>{channel}</b>\n\n"
            "Шаг 4/8 — Введи <b>ссылку на канал</b> (https://t.me/...):",
            reply_markup=_pack_cancel_kb()
        )

    elif step == "channel_url":
        url = (message.text or "").strip()
        if not url.startswith("https://t.me/"):
            return await message.answer(
                "❌ Ссылка должна начинаться с https://t.me/",
                reply_markup=_pack_cancel_kb()
            )
        session["channel_url"] = url
        session["step"]        = "days"
        await message.answer(
            "Шаг 5/8 — Сколько <b>дней</b> активен пак? (1–365)\n"
            "<i>По истечении пак скроется для всех</i>",
            reply_markup=_pack_cancel_kb()
        )

    elif step == "days":
        text = (message.text or "").strip()
        if not text.isdigit() or not (1 <= int(text) <= 365):
            return await message.answer("❌ Введи число от 1 до 365.", reply_markup=_pack_cancel_kb())
        session["days"]  = int(text)
        session["step"]  = "cooldown"
        await message.answer(
            f"✅ Активен: <b>{text} дн.</b>\n\n"
            "Шаг 6/8 — Кулдаун между открытиями (в <b>часах</b>): (1–168)\n"
            "<i>Пример: 24 = раз в сутки</i>",
            reply_markup=_pack_cancel_kb()
        )

    elif step == "cooldown":
        text = (message.text or "").strip()
        if not text.isdigit() or not (1 <= int(text) <= 168):
            return await message.answer("❌ Введи число от 1 до 168.", reply_markup=_pack_cancel_kb())
        session["cooldown_hours"] = int(text)
        session["step"]           = "unique_chance"
        await message.answer(
            f"✅ Кулдаун: <b>{text} ч.</b>\n\n"
            "Шаг 7/8 — <b>Шанс выпадения уникальной карты</b> (в %): (1–50)\n"
            "<i>Рекомендуем 10–20%</i>",
            reply_markup=_pack_cancel_kb()
        )

    elif step == "unique_chance":
        text = (message.text or "").strip()
        if not text.isdigit() or not (1 <= int(text) <= 50):
            return await message.answer("❌ Введи число от 1 до 50.", reply_markup=_pack_cancel_kb())
        session["unique_chance"] = int(text)
        session["step"]          = "banner"
        await message.answer(
            f"✅ Шанс уникальной: <b>{text}%</b>\n\n"
            "Шаг 8/8 — Отправь <b>баннер-фото</b> пака\n"
            "<i>(или отправь - чтобы без баннера)</i>",
            reply_markup=_pack_cancel_kb()
        )

    elif step == "banner":
        if message.text and message.text.strip() == "-":
            session["banner_photo"] = None
        elif message.photo:
            session["banner_photo"] = message.photo[-1].file_id
        else:
            return await message.answer(
                "❌ Отправь фото или напиши -", reply_markup=_pack_cancel_kb()
            )
        session["step"]            = "add_unique"
        session["unique_cards"]    = []
        session["unique_card_step"] = "photo"
        await message.answer(
            "✅ Баннер сохранён!\n\n"
            "🃏 <b>Теперь добавим уникальные карты</b> (от 3 до 5)\n\n"
            "Карта 1/3 — Отправь <b>фото карточки</b> файлом\n"
            "📐 Рекомендуем убрать фон через @carvephotos_bot",
            reply_markup=_pack_unique_card_kb(len(session["unique_cards"]))
        )

def _pack_unique_card_kb(current_count: int) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if current_count >= 3:
        kb.button(text=f"✅ Готово ({current_count} карт добавлено)", callback_data="pack_unique_done")
    kb.button(text="❌ Отменить создание пака", callback_data="pack_create_cancel")
    kb.adjust(1)
    return kb.as_markup()


@dp.message(
    F.from_user.id == ADMIN_ID,
    lambda m: m.from_user.id in pack_create_sessions
              and pack_create_sessions[m.from_user.id].get("step") == "add_unique"
)
async def pack_add_unique_card(message: types.Message):
    uid     = message.from_user.id
    session = pack_create_sessions[uid]
    cards   = session["unique_cards"]
    substep = session.get("unique_card_step", "photo")

    if message.text and message.text.startswith("/"):
        pack_create_sessions.pop(uid, None)
        return await message.answer("❌ Создание пака отменено.")

    n = len(cards) + 1 

    if substep == "photo":
        if not message.document or not (message.document.mime_type or "").startswith("image"):
            return await message.answer(
                "❌ Отправь фото <b>файлом</b> (не как фото)!\n"
                "Совет: сначала убери фон в @carvephotos_bot",
                reply_markup=_pack_unique_card_kb(len(cards))
            )
        session["_cur_card"] = {"photo_id": message.document.file_id}
        session["unique_card_step"] = "name"
        await message.answer(
            f"✅ Фото принято!\n\nКарта {n} — Введи <b>имя игрока</b>:",
            reply_markup=_pack_cancel_kb()
        )

    elif substep == "name":
        name = (message.text or "").strip()
        if not name:
            return await message.answer("❌ Имя не может быть пустым.", reply_markup=_pack_cancel_kb())
        session["_cur_card"]["name"] = name
        session["unique_card_step"]  = "rating"
        await message.answer(
            f"✅ Имя: <b>{name}</b>\n\nКарта {n} — Введи <b>рейтинг</b> (50–99):",
            reply_markup=_pack_cancel_kb()
        )

    elif substep == "rating":
        text = (message.text or "").strip()
        if not text.isdigit() or not (50 <= int(text) <= 99):
            return await message.answer("❌ Рейтинг 50–99.", reply_markup=_pack_cancel_kb())
        session["_cur_card"]["rating"] = int(text)
        session["unique_card_step"]    = "club"
        await message.answer(
            f"✅ Рейтинг: <b>{text}</b>\n\nКарта {n} — Введи <b>клуб</b>:",
            reply_markup=_pack_cancel_kb()
        )

    elif substep == "club":
        session["_cur_card"]["club"] = (message.text or "").strip()
        session["unique_card_step"]   = "position"
        kb = InlineKeyboardBuilder()
        for pos in ["Нападающий", "Полузащитник", "Защитник", "Вратарь"]:
            kb.button(text=pos, callback_data=f"pack_pos_{pos}")
        kb.adjust(2)
        kb.button(text="❌ Отменить создание пака", callback_data="pack_create_cancel")
        await message.answer(
            f"✅ Клуб: <b>{session['_cur_card']['club']}</b>\n\n"
            f"Карта {n} — Выбери <b>позицию</b>:",
            reply_markup=kb.as_markup()
        )

@dp.callback_query(F.data.startswith("pack_pos_"), F.from_user.id == ADMIN_ID)
async def pack_pick_position(call: types.CallbackQuery):
    uid      = call.from_user.id
    session  = pack_create_sessions.get(uid)
    if not session or session.get("step") != "add_unique":
        return await call.answer("Сессия не найдена.", show_alert=True)

    position = call.data[len("pack_pos_"):]
    cur_card = session["_cur_card"]
    cur_card["position"] = position

    async with db() as conn:
        tier = determine_tier(cur_card["rating"])
        await conn.execute(
            "INSERT INTO all_cards (name, rating, club, position, rarity, rarity_type, photo_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cur_card["name"], cur_card["rating"], cur_card["club"],
             position, tier, tier, cur_card["photo_id"])
        )
        async with conn.execute("SELECT last_insert_rowid() AS id") as cur2:
            card_id = (await cur2.fetchone())["id"]
        await conn.commit()

    session["unique_cards"].append(card_id)
    session["_cur_card"]        = {}
    session["unique_card_step"] = "photo"

    cards   = session["unique_cards"]
    n_done  = len(cards)
    can_end = n_done >= 3
    can_add = n_done < 5

    await call.message.edit_text(
        f"✅ Карта добавлена! ({n_done}/5 уникальных)\n\n"
        + (f"Карта {n_done+1} — Отправь <b>фото следующей карточки</b> файлом:\n"
           f"📐 @carvephotos_bot для удаления фона"
           if can_add else
           "✅ Добавлено максимум 5 карт — нажми «Готово»"),
        reply_markup=_pack_unique_card_kb(n_done)
    )
    await call.answer()

@dp.callback_query(F.data == "pack_unique_done", F.from_user.id == ADMIN_ID)
async def pack_unique_done(call: types.CallbackQuery):
    uid     = call.from_user.id
    session = pack_create_sessions.pop(uid, None)
    if not session:
        return await call.answer("Сессия не найдена.", show_alert=True)

    unique_ids = session["unique_cards"]
    if len(unique_ids) < 3:
        return await call.answer("❌ Добавь минимум 3 уникальные карты!", show_alert=True)

    expires_at   = (datetime.now() + timedelta(days=session["days"])).isoformat()
    cooldown_sec = session["cooldown_hours"] * 3600

    async with db() as conn:
        await conn.execute(
            "INSERT INTO special_packs "
            "(title, description, channel, channel_url, days_active, cooldown_sec, "
            " unique_chance, expires_at, banner_photo) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session["title"], session.get("description", ""),
             session["channel"], session["channel_url"],
             session["days"], cooldown_sec,
             session["unique_chance"], expires_at,
             session.get("banner_photo"))
        )
        async with conn.execute("SELECT last_insert_rowid() AS id") as cur:
            pack_id = (await cur.fetchone())["id"]

        for card_id in unique_ids:
            await conn.execute(
                "INSERT INTO special_pack_cards (pack_id, card_id, is_unique) VALUES (?, ?, 1)",
                (pack_id, card_id)
            )

        excl = ",".join(str(i) for i in unique_ids)
        async with conn.execute(
            f"SELECT id FROM all_cards WHERE id NOT IN ({excl}) ORDER BY RANDOM() LIMIT 10"
        ) as cur:
            regular = await cur.fetchall()
        for r in regular:
            await conn.execute(
                "INSERT INTO special_pack_cards (pack_id, card_id, is_unique) VALUES (?, ?, 0)",
                (pack_id, r["id"])
            )
        await conn.commit()

    await call.message.edit_text(
        f"🎉 <b>Пак создан!</b>\n\n"
        f"📦 Название: <b>{session['title']}</b>\n"
        f"🆔 ID пака: <code>{pack_id}</code>\n"
        f"📢 Канал: {session['channel']}\n"
        f"📅 Активен до: {expires_at[:10]}\n"
        f"🔁 Кулдаун: {_fmt_cooldown(cooldown_sec)}\n"
        f"✨ Шанс уникальной: {session['unique_chance']}%\n"
        f"🃏 Уникальных карт: {len(unique_ids)}\n"
        f"🃏 Обычных карт: {len(regular)}\n\n"
        f"Пак виден пользователям в разделе «Спец. Паки 🎁»"
    )
    await call.answer()


@dp.message(Command("packs"), F.from_user.id == ADMIN_ID)
async def cmd_packs_admin(message: types.Message):
    async with db() as conn:
        async with conn.execute(
            "SELECT id, title, COALESCE(is_active,1) AS is_active, expires_at, "
            "(SELECT COUNT(*) FROM special_pack_cards WHERE pack_id = special_packs.id) AS card_count "
            "FROM special_packs ORDER BY id DESC LIMIT 20"
        ) as cur:
            packs = await cur.fetchall()

    if not packs:
        return await message.answer("📦 Паков пока нет. Создай через /createpack")

    kb = InlineKeyboardBuilder()
    for p in packs:
        active   = _pack_is_active(p)
        status   = "🟢" if active else "🔴"
        exp      = (p["expires_at"] or "")[:10]
        kb.button(
            text=f"{status} {p['title']} | до {exp} | {p['card_count']} карт",
            callback_data=f"spadmin_view_{p['id']}"
        )
    kb.adjust(1)
    await message.answer("📦 <b>Все специальные паки:</b>", reply_markup=kb.as_markup())

@dp.message(Command("packstat"), F.from_user.id == ADMIN_ID)
async def cmd_packstat(message: types.Message, command: CommandObject):
    async with db() as conn:
        async with conn.execute(
            "SELECT id, title, COALESCE(is_active,1) AS is_active, expires_at, "
            "unique_chance, cooldown_sec "
            "FROM special_packs ORDER BY id DESC LIMIT 20"
        ) as cur:
            packs = await cur.fetchall()

    if not packs:
        return await message.answer("📦 Паков нет.")

    if not command.args or not command.args.strip().isdigit():
        kb = InlineKeyboardBuilder()
        for p in packs:
            active = _pack_is_active(p)
            status = "🟢" if active else "🔴"
            kb.button(
                text=f"{status} [{p['id']}] {p['title']}",
                callback_data=f"pstat_view_{p['id']}"
            )
        kb.adjust(1)
        return await message.answer(
            "📊 <b>Выбери пак для просмотра статистики:</b>\n"
            "<i>Или укажи ID: /packstat 3</i>",
            reply_markup=kb.as_markup()
        )

    pack_id = int(command.args.strip())
    await _send_packstat(message, pack_id)

async def _send_packstat(target, pack_id: int):
    async with db() as conn:
        async with conn.execute(
            "SELECT id, title, COALESCE(is_active,1) AS is_active, expires_at, "
            "unique_chance, cooldown_sec, channel, created_at "
            "FROM special_packs WHERE id = ?", (pack_id,)
        ) as cur:
            pack = await cur.fetchone()

        if not pack:
            txt = f"❌ Пак с ID {pack_id} не найден."
            if isinstance(target, types.Message):
                return await target.answer(txt)
            return await target.message.edit_text(txt)

        async with conn.execute(
            "SELECT COUNT(*) FROM user_pack_opens WHERE pack_id = ?", (pack_id,)
        ) as cur:
            total_opens = (await cur.fetchone())[0]

        async with conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM user_pack_opens WHERE pack_id = ?", (pack_id,)
        ) as cur:
            unique_users = (await cur.fetchone())[0]

        async with conn.execute(
            "SELECT spc.card_id, c.name, c.rating "
            "FROM special_pack_cards spc "
            "JOIN all_cards c ON c.id = spc.card_id "
            "WHERE spc.pack_id = ? AND spc.is_unique = 1 "
            "ORDER BY c.rating DESC",
            (pack_id,)
        ) as cur:
            unique_cards = await cur.fetchall()

        card_winners = {}
        for uc in unique_cards:
            async with conn.execute(
                "SELECT u.username, u.user_id, uc_open.id "
                "FROM user_cards uc_open "
                "JOIN users u ON u.user_id = uc_open.user_id "
                "WHERE uc_open.card_id = ? "
                "ORDER BY uc_open.id ASC",
                (uc["card_id"],)
            ) as cur:
                winners = await cur.fetchall()
            card_winners[uc["card_id"]] = winners

        async with conn.execute(
            "SELECT u.username, u.user_id, upo.opened_at "
            "FROM user_pack_opens upo "
            "JOIN users u ON u.user_id = upo.user_id "
            "WHERE upo.pack_id = ? ORDER BY upo.id DESC LIMIT 5",
            (pack_id,)
        ) as cur:
            recent_opens = await cur.fetchall()

    active    = _pack_is_active(pack)
    exp       = parse_dt(pack["expires_at"])
    days_left = max(0, (exp - datetime.now()).days) if exp else 0

    lines = [
        f"📊 <b>Статистика пака: {pack['title']}</b>",
        f"🆔 ID: <code>{pack_id}</code>  |  "
        f"{'🟢 Активен' if active else '🔴 Неактивен'}",
        f"📅 Истекает: {(pack['expires_at'] or '')[:10]} (ещё {days_left} дн.)",
        f"📢 Канал: {pack['channel']}",
        f"✨ Шанс уникальной: {pack['unique_chance']}%",
        f"🔁 Кулдаун: {_fmt_cooldown(pack['cooldown_sec'])}",
        f"",
        f"📈 <b>Открытий всего:</b> {total_opens}",
        f"👥 <b>Уникальных игроков:</b> {unique_users}",
        f"",
        f"🃏 <b>Уникальные карты и победители:</b>",
    ]

    for uc in unique_cards:
        winners = card_winners.get(uc["card_id"], [])
        if winners:
            w_list = ", ".join(
                f"@{w['username']}" if w["username"] else f"ID {w['user_id']}"
                for w in winners[:5]
            )
            extra = f" (+ещё {len(winners)-5})" if len(winners) > 5 else ""
            lines.append(
                f"  ⭐ <b>{uc['name']}</b> ({uc['rating']}) — "
                f"выбили {len(winners)}x: {w_list}{extra}"
            )
        else:
            lines.append(f"  ⭐ <b>{uc['name']}</b> ({uc['rating']}) — ещё никто не выбил")

    if recent_opens:
        lines.append(f"\n🕐 <b>Последние открытия:</b>")
        for r in recent_opens:
            uname = f"@{r['username']}" if r["username"] else f"ID {r['user_id']}"
            dt    = (r["opened_at"] or "")[:16].replace("T", " ")
            lines.append(f"  • {uname} — {dt}")

    kb = InlineKeyboardBuilder()
    if active:
        kb.button(text="🔴 Деактивировать",    callback_data=f"spadmin_toggle_{pack_id}_0")
    else:
        kb.button(text="🟢 Активировать",       callback_data=f"spadmin_toggle_{pack_id}_1")
    kb.button(text="🗑 Удалить пак полностью", callback_data=f"pstat_delete_{pack_id}")
    kb.button(text="◀️ К списку паков",        callback_data="pstat_back")
    kb.adjust(1)

    text = "\n".join(lines)
    if isinstance(target, types.Message):
        await target.answer(text, reply_markup=kb.as_markup())
    else:
        await target.message.edit_text(text, reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("pstat_view_"), F.from_user.id == ADMIN_ID)
async def pstat_view(call: types.CallbackQuery):
    pack_id = int(call.data.split("_")[2])
    await _send_packstat(call, pack_id)
    await call.answer()

@dp.callback_query(F.data == "pstat_back", F.from_user.id == ADMIN_ID)
async def pstat_back(call: types.CallbackQuery):
    await call.message.delete()
    await cmd_packstat(call.message, type("obj", (), {"args": None})())
    await call.answer()

@dp.callback_query(F.data.startswith("pstat_delete_"), F.from_user.id == ADMIN_ID)
async def pstat_delete_prompt(call: types.CallbackQuery):
    pack_id = int(call.data.split("_")[2])
    async with db() as conn:
        async with conn.execute("SELECT title FROM special_packs WHERE id = ?", (pack_id,)) as cur:
            pack = await cur.fetchone()
    if not pack:
        return await call.answer("Пак не найден.", show_alert=True)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить навсегда",  callback_data=f"pstat_confirm_del_{pack_id}")
    kb.button(text="❌ Отмена",                callback_data=f"pstat_view_{pack_id}")
    kb.adjust(1)
    await call.message.edit_text(
        f"⚠️ <b>Удалить пак «{pack['title']}»?</b>\n\n"
        f"🆔 ID: <code>{pack_id}</code>\n\n"
        f"Это действие необратимо:\n"
        f"• Пак исчезнет из меню пользователей\n"
        f"• История открытий удалится\n"
        f"• Уникальные карты <b>останутся</b> у пользователей",
        reply_markup=kb.as_markup()
    )
    await call.answer()

@dp.callback_query(F.data.startswith("pstat_confirm_del_"), F.from_user.id == ADMIN_ID)
async def pstat_confirm_delete(call: types.CallbackQuery):
    pack_id = int(call.data.split("_")[3])
    async with db() as conn:
        async with conn.execute("SELECT title FROM special_packs WHERE id = ?", (pack_id,)) as cur:
            pack = await cur.fetchone()
        title = pack["title"] if pack else f"ID {pack_id}"
        await conn.execute("DELETE FROM user_pack_opens WHERE pack_id = ?", (pack_id,))
        await conn.execute("DELETE FROM special_packs WHERE id = ?", (pack_id,))
        await conn.commit()
    await call.message.edit_text(
        f"🗑 <b>Пак «{title}» удалён.</b>\n\n"
        f"Уникальные карты сохранены в коллекциях пользователей."
    )
    await call.answer("Удалено.", show_alert=True)

@dp.callback_query(F.data.startswith("spadmin_view_"), F.from_user.id == ADMIN_ID)
async def spadmin_view(call: types.CallbackQuery):
    pack_id = int(call.data.split("_")[2])
    await _send_packstat(call, pack_id)
    await call.answer()

@dp.callback_query(F.data.startswith("spadmin_toggle_"), F.from_user.id == ADMIN_ID)
async def spadmin_toggle(call: types.CallbackQuery):
    parts   = call.data.split("_")
    pack_id = int(parts[2])
    new_val = int(parts[3])
    async with db() as conn:
        await conn.execute("UPDATE special_packs SET is_active = ? WHERE id = ?", (new_val, pack_id))
        await conn.commit()
    status = "🟢 активирован" if new_val else "🔴 деактивирован"
    await call.answer(f"Пак {status}.", show_alert=True)
    await _send_packstat(call, pack_id)

@dp.callback_query(F.data.startswith("spadmin_delete_"), F.from_user.id == ADMIN_ID)
async def spadmin_delete(call: types.CallbackQuery):
    pack_id = int(call.data.split("_")[2])
    call.data = f"pstat_delete_{pack_id}"
    await pstat_delete_prompt(call)

@dp.callback_query(F.data.startswith("spadmin_confirm_del_"), F.from_user.id == ADMIN_ID)
async def spadmin_confirm_delete(call: types.CallbackQuery):
    pack_id = int(call.data.split("_")[3])
    call.data = f"pstat_confirm_del_{pack_id}"
    await pstat_confirm_delete(call)

@dp.callback_query(F.data == "spadmin_back", F.from_user.id == ADMIN_ID)
async def spadmin_back(call: types.CallbackQuery):
    await call.message.delete()
    await cmd_packs_admin(call.message)
    await call.answer()




async def start_engine():
    await init_db()
    log.info("Бот запущен. База: %s", DB_PATH)
    if MAINTENANCE_MODE:
        log.warning("⚠️ Бот запущен в РЕЖИМЕ ТЕХОБСЛУЖИВАНИЯ!")

    await clean_groups()

    dp.message.middleware(MaintenanceBanMiddleware())
    dp.callback_query.middleware(MaintenanceBanMiddleware())

    asyncio.create_task(referral_checker_loop())
    asyncio.create_task(notification_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(start_engine())
