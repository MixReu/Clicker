import asyncio
import sqlite3
import uuid
import time
import datetime
import os
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ================ НАСТРОЙКИ ================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
MERCHANT_ID = os.environ["MERCHANT_ID"]
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "6743070898").split(",")]

# Игровые настройки
CLICK_REWARD = 150          # базовая награда за клик
CLICK_COOLDOWN = 1         # кулдаун между кликами (секунды)
ENERGY_MAX = 100           # максимальная энергия
ENERGY_PER_CLICK = 1       # энергия за клик
ENERGY_REGEN_INTERVAL = 20  # 1 ед энергии раз в 20 секунд
REFERRAL_BONUS = 200       # бонус за приглашённого
REFERRAL_CLICK_PERCENT = 25  # % от клика реферала goes to referrer
SHOP_PRICE_INCREASE = 75   # % повышения цены в магазине после каждой покупки
LEVEL_UP_BASE_COST = 1000  # базовая цена прокачки (умножается на уровень)
LEVEL_BONUS = 10           # +10 к клику за каждый уровень

# Магазин бустов
SHOP_ITEMS = {
    "boost15":  {"name": "🚀 Буст клика +15%",  "price": 10000,  "type": "boost",  "value": 15},
    "boost30":  {"name": "💥 Буст клика +30%",  "price": 25000,  "type": "boost",  "value": 30},
    "boost50":  {"name": "🔥 Буст клика +50%",  "price": 50000,  "type": "boost",  "value": 50},
    "boost100": {"name": "⚡ Буст клика +100%", "price": 100000, "type": "boost",  "value": 100},
    "energy50": {"name": "🔋 +50 к макс. энергии",  "price": 15000, "type": "energy", "value": 50},
    "energy100":{"name": "🔋 +100 к макс. энергии", "price": 30000, "type": "energy", "value": 100},
}

# Ежедневная награда за клики
DAILY_REWARDS = [100000, 50000, 30000, 20000, 10000, 5000, 5000, 5000, 5000, 5000]

API_BASE = "https://paper-scroll.online/developer.php"

# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ================ БАЗА ДАННЫХ ================
DB_PATH = "clicker.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        paper_id TEXT,
        username TEXT,
        balance INTEGER DEFAULT 0,
        total_clicks INTEGER DEFAULT 0,
        energy INTEGER DEFAULT 100,
        last_click REAL DEFAULT 0,
        last_energy_regen REAL DEFAULT 0,
        level INTEGER DEFAULT 1,
        referrer INTEGER,
        created_at REAL,
        is_banned INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS withdrawals (
        id TEXT PRIMARY KEY,
        tg_id INTEGER,
        amount INTEGER,
        status TEXT DEFAULT 'pending',
        created_at REAL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS bot_fund (
        id INTEGER PRIMARY KEY DEFAULT 1,
        total INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS promocodes (
        code TEXT PRIMARY KEY,
        reward INTEGER,
        max_uses INTEGER DEFAULT -1,
        used_count INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        created_at REAL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS promo_used (
        code TEXT,
        tg_id INTEGER,
        PRIMARY KEY (code, tg_id)
    )""")
    # Добавляем колонки если их нет (для старых баз)
    for col, coldef in [
        ("is_banned", "INTEGER DEFAULT 0"),
        ("boost_click", "INTEGER DEFAULT 0"),
        ("energy_bonus", "INTEGER DEFAULT 0"),
        ("ref_earnings", "INTEGER DEFAULT 0"),
    ]:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {coldef}")
        except:
            pass
    # Таблица магазина
    c.execute("""CREATE TABLE IF NOT EXISTS shop_purchases (
        id TEXT PRIMARY KEY,
        tg_id INTEGER,
        item_key TEXT,
        price INTEGER,
        created_at REAL
    )""")
    # Таблица покупок каждого товара каждым юзером (для динамических цен)
    c.execute("""CREATE TABLE IF NOT EXISTS shop_user_purchases (
        tg_id INTEGER,
        item_key TEXT,
        count INTEGER DEFAULT 0,
        PRIMARY KEY (tg_id, item_key)
    )""")
    # Таблица ежедневных кликов
    c.execute("""CREATE TABLE IF NOT EXISTS daily_clicks (
        tg_id INTEGER,
        date TEXT,
        clicks INTEGER DEFAULT 0,
        PRIMARY KEY (tg_id, date)
    )""")
    # Таблица выданных ежедневных наград
    c.execute("""CREATE TABLE IF NOT EXISTS daily_rewards_given (
        date TEXT PRIMARY KEY,
        created_at REAL
    )""")
    conn.commit()
    conn.close()

init_db()

def get_user(tg_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    keys = ["tg_id", "paper_id", "username", "balance", "total_clicks",
            "energy", "last_click", "last_energy_regen", "level", "referrer",
            "created_at", "is_banned", "boost_click", "energy_bonus", "ref_earnings"]
    return dict(zip(keys, row))

def create_user(tg_id: int, username: str, referrer: int | None = None) -> dict:
    now = time.time()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""INSERT OR IGNORE INTO users
        (tg_id, paper_id, username, balance, total_clicks, energy,
         last_click, last_energy_regen, level, referrer, created_at, is_banned)
        VALUES (?, NULL, ?, 0, 0, ?, ?, ?, 1, ?, ?, 0)""",
        (tg_id, username, ENERGY_MAX, now, now, referrer, now))
    if referrer:
        c.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?",
                  (REFERRAL_BONUS, referrer))
    conn.commit()
    conn.close()
    return get_user(tg_id)

def update_user(tg_id: int, **fields):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [tg_id]
    c.execute(f"UPDATE users SET {sets} WHERE tg_id = ?", vals)
    conn.commit()
    conn.close()

def regen_energy(user: dict) -> dict:
    now = time.time()
    elapsed = now - user["last_energy_regen"]
    regen_amount = int(elapsed / ENERGY_REGEN_INTERVAL)
    if regen_amount > 0:
        emax = get_energy_max(user)
        new_energy = min(user["energy"] + regen_amount, emax)
        user["energy"] = new_energy
        update_user(user["tg_id"], energy=new_energy, last_energy_regen=now)
    return user

def get_click_reward(user: dict) -> int:
    # Экспоненциальная прокачка: +50% от бонуса предыдущего уровня
    # Уровень 1: база, уровень 2: +10, уровень 3: +25, уровень 4: +47, и т.д.
    level = user["level"]
    if level <= 1:
        base = CLICK_REWARD
    else:
        total_bonus = int(LEVEL_BONUS * 2 * (1.5 ** (level - 1) - 1))
        base = CLICK_REWARD + total_bonus
    boost = user.get("boost_click", 0)
    return int(base * (1 + boost / 100))

def get_energy_max(user: dict = None) -> int:
    if user is None:
        return ENERGY_MAX
    return ENERGY_MAX + user.get("energy_bonus", 0)

def get_level_up_cost(user: dict) -> int:
    return user["level"] * LEVEL_UP_BASE_COST

def is_banned(tg_id: int) -> bool:
    user = get_user(tg_id)
    if not user:
        return False
    return user.get("is_banned", 0) == 1

def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS

def get_user_by_identifier(identifier: str) -> dict | None:
    """Найти юзера по Telegram ID или по @username (с @ или без)."""
    identifier = identifier.strip()
    if identifier.startswith("@"):
        identifier = identifier[1:]
    # Try as numeric Telegram ID
    try:
        tg_id = int(identifier)
        user = get_user(tg_id)
        if user:
            return user
    except ValueError:
        pass
    # Try by username (case-insensitive)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (identifier,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    keys = ["tg_id", "paper_id", "username", "balance", "total_clicks",
            "energy", "last_click", "last_energy_regen", "level", "referrer",
            "created_at", "is_banned", "boost_click", "energy_bonus", "ref_earnings"]
    return dict(zip(keys, row))


def parse_amount(text: str) -> int:
    """Парсит сокращения: 1к=1000, 1кк=1000000, 1м=1000000, 2.5к=2500 и т.д."""
    text = text.strip().lower().replace(" ", "")
    multipliers = {"к": 1000, "кк": 1000000, "м": 1000000, "мм": 1000000000}
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if text.endswith(suffix):
            num = text[:-len(suffix)]
            try:
                return int(float(num) * mult)
            except ValueError:
                pass
    try:
        return int(text)
    except ValueError:
        return -1



def get_item_price(tg_id: int, item_key: str) -> int:
    """Динамическая цена: base * (1.75 ^ кол-во_покупок)."""
    base_price = SHOP_ITEMS[item_key]["price"]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT count FROM shop_user_purchases WHERE tg_id = ? AND item_key = ?", (tg_id, item_key))
    row = c.fetchone()
    conn.close()
    count = row[0] if row else 0
    return int(base_price * (1 + SHOP_PRICE_INCREASE / 100) ** count)


# ================ PAPER SCROLL API ================
async def paper_api_call(method: str, payload: dict) -> dict:
    payload["accessToken"] = ACCESS_TOKEN
    payload["merchantId"] = int(MERCHANT_ID)
    url = f"{API_BASE}?method={method}"
    headers = {"Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            return await resp.json()

# ================ КЛАВИАТУРЫ ================
def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👆 Кликнуть!", callback_data="click")],
        [InlineKeyboardButton(text="💰 Баланс", callback_data="balance"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="📈 Прокачать уровень", callback_data="levelup"),
         InlineKeyboardButton(text="🛒 Магазин", callback_data="shop")],
        [InlineKeyboardButton(text="🏆 Топ игроков", callback_data="top"),
         InlineKeyboardButton(text="📅 Топ кликеров дня", callback_data="top_clicks")],
        [InlineKeyboardButton(text="🔗 Реф. ссылка", callback_data="reflink"),
         InlineKeyboardButton(text="🎁 Промокод", callback_data="promo")],
        [InlineKeyboardButton(text="🔍 Мой PaperScroll ID", callback_data="mypaperid")],
    ])

def admin_keyboard():
    kb = main_keyboard()
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="📤 Пополнить бот", callback_data="admin_topup"),
        InlineKeyboardButton(text="📋 Статистика бота", callback_data="admin_stats"),
    ])
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users"),
        InlineKeyboardButton(text="🚫 Блокировки", callback_data="admin_bans"),
    ])
    kb.inline_keyboard.append([
        InlineKeyboardButton(text="📅 Награда дня", callback_data="admin_daily"),
    ])
    return kb

# ================ ХЕНДЛЕРЫ ================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    # АДМИН НИКОГДА НЕ БАНИТСЯ
    if is_admin(message.from_user.id):
        user = get_user(message.from_user.id)
        if not user:
            user = create_user(message.from_user.id, message.from_user.username or "—")
        await message.answer(
            f"👑 Админ-панель\n\n"
            f"💰 Баланс: {user['balance']} | ⚡ Энергия: {user['energy']}/{get_energy_max(user)}\n"
            f"📈 Уровень: {user['level']} | Награда за клик: +{get_click_reward(user)}",
            reply_markup=admin_keyboard()
        )
        return

    if is_banned(message.from_user.id):
        await message.answer("🚫 Вы заблокированы админом бота.")
        return

    referrer = None
    if len(message.text.split()) > 1:
        try:
            referrer = int(message.text.split()[1])
            if referrer == message.from_user.id:
                referrer = None
        except ValueError:
            referrer = None

    user = get_user(message.from_user.id)
    if not user:
        user = create_user(message.from_user.id, message.from_user.username or "—", referrer)
        if referrer:
            await message.answer(
                f"👋 Добро пожаловать!\n"
                f"Ты пришёл по приглашению. Реферер получил +{REFERRAL_BONUS} бумаги."
            )
        else:
            await message.answer(
                f"👋 Добро пожаловать в кликер на бумаге!\n\n"
                f"👆 Нажимай «Кликнуть» и зарабатывай бумагу.\n"
                f"💸 Выводи заработанное на свой PaperScroll аккаунт.\n"
                f"🔗 Приглашай друзей и получай +{REFERRAL_BONUS} бумаги за каждого!\n"
                f"💵 А ещё {REFERRAL_CLICK_PERCENT}% от каждого клика реферала!\n\n"
                f"За каждый клик: +{CLICK_REWARD} бумаги\n"
                f"⚡ Энергия: {ENERGY_MAX} (восстанавливается 1 ед / 20 сек)\n"
                f"📈 Прокачивай уровень — больше бумаги за клик!"
            )
    else:
        user = regen_energy(user)
        await message.answer(
            f"👋 С возвращением!\n"
            f"💰 Баланс: {user['balance']} | ⚡ Энергия: {user['energy']}/{get_energy_max(user)}\n"
            f"📈 Уровень: {user['level']} | Кликов: {user['total_clicks']}"
        )

    kb = admin_keyboard() if is_admin(message.from_user.id) else main_keyboard()
    await message.answer("👇 Выбери действие:", reply_markup=kb)


@dp.callback_query(F.data == "click")
async def cb_click(cb: types.CallbackQuery):
    if is_admin(cb.from_user.id):
        pass
    elif is_banned(cb.from_user.id):
        await cb.answer("🚫 Вы заблокированы!", show_alert=True)
        return

    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала нажми /start", show_alert=True)
        return

    user = regen_energy(user)

    now = time.time()
    if now - user["last_click"] < CLICK_COOLDOWN:
        wait = CLICK_COOLDOWN - (now - user["last_click"])
        await cb.answer(f"⏱ Подожди {wait:.1f} сек", show_alert=False)
        return

    if user["energy"] < ENERGY_PER_CLICK:
        await cb.answer("⚡ Недостаточно энергии! Подожди восстановления.", show_alert=True)
        return

    reward = get_click_reward(user)
    new_balance = user["balance"] + reward
    new_clicks = user["total_clicks"] + 1
    new_energy = user["energy"] - ENERGY_PER_CLICK

    update_user(
        cb.from_user.id,
        balance=new_balance,
        total_clicks=new_clicks,
        energy=new_energy,
        last_click=now,
    )

    # Запись клика в ежедневный топ
    today = time.strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO daily_clicks (tg_id, date, clicks) VALUES (?, ?, 0)", (cb.from_user.id, today))
    c.execute("UPDATE daily_clicks SET clicks = clicks + 1 WHERE tg_id = ? AND date = ?", (cb.from_user.id, today))

    # Реферальный бонус: 25% от клика рефералу
    referrer_id = user.get("referrer")
    if referrer_id:
        ref_bonus = int(reward * REFERRAL_CLICK_PERCENT / 100)
        if ref_bonus > 0:
            c.execute("UPDATE users SET balance = balance + ?, ref_earnings = ref_earnings + ? WHERE tg_id = ?",
                      (ref_bonus, ref_bonus, referrer_id))

    conn.commit()
    conn.close()

    await cb.answer(f"+{reward} бумаги! 💰", show_alert=False)
    await cb.message.edit_text(
        f"👆 Кликер\n\n"
        f"💰 Баланс: {new_balance}\n"
        f"⚡ Энергия: {new_energy}/{get_energy_max(user)}\n"
        f"📊 Кликов: {new_clicks} | Уровень: {user['level']}\n"
        f"💵 За клик: +{reward}",
        reply_markup=main_keyboard() if not is_admin(cb.from_user.id) else admin_keyboard()
    )


@dp.callback_query(F.data == "balance")
async def cb_balance(cb: types.CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала /start", show_alert=True)
        return
    user = regen_energy(user)
    await cb.answer()
    kb = admin_keyboard() if is_admin(cb.from_user.id) else main_keyboard()
    await cb.message.edit_text(
        f"💰 Твой баланс: {user['balance']} бумаги\n"
        f"📊 Всего кликов: {user['total_clicks']}\n"
        f"⚡ Энергия: {user['energy']}/{get_energy_max(user)}\n"
        f"📈 Уровень: {user['level']} | +{get_click_reward(user)}/клик",
        reply_markup=kb
    )


@dp.callback_query(F.data == "stats")
async def cb_stats(cb: types.CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала /start", show_alert=True)
        return
    user = regen_energy(user)
    kb = admin_keyboard() if is_admin(cb.from_user.id) else main_keyboard()
    # Рефералы
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE referrer = ?", (cb.from_user.id,))
    ref_count = c.fetchone()[0]
    ref_earnings = user.get("ref_earnings", 0)
    conn.close()

    await cb.answer()
    await cb.message.edit_text(
        f"📊 Статистика\n\n"
        f"👤 Игрок: @{user['username']}\n"
        f"💰 Баланс: {user['balance']}\n"
        f"👆 Кликов: {user['total_clicks']}\n"
        f"📈 Уровень: {user['level']} (+{get_click_reward(user)}/клик)\n"
        f"⚡ Энергия: {user['energy']}/{get_energy_max(user)}\n"
        f"🔗 PaperScroll ID: {user['paper_id'] or 'не привязан'}\n"
        f"🤝 Рефералов: {ref_count}\n"
        f"🏆 Заработано с рефералов: {ref_earnings}",
        reply_markup=kb
    )


@dp.callback_query(F.data == "levelup")
async def cb_levelup(cb: types.CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала /start", show_alert=True)
        return
    user = regen_energy(user)
    cost = get_level_up_cost(user)
    next_reward = get_click_reward({**user, "level": user["level"] + 1})
    kb = admin_keyboard() if is_admin(cb.from_user.id) else main_keyboard()
    if user['balance'] < cost:
        await cb.answer()
        await cb.message.edit_text(
            f"📈 Прокачка уровня\n\n"
            f"Текущий уровень: {user['level']}\n"
            f"Награда за клик: +{get_click_reward(user)}\n\n"
            f"Следующий уровень: {user['level'] + 1}\n"
            f"Награда за клик: +{next_reward}\n"
            f"💰 Цена: {cost} бумаги\n\n"
            f"❌ Недостаточно бумаги! У тебя: {user['balance']}",
            reply_markup=kb
        )
    else:
        await cb.answer()
        await cb.message.edit_text(
            f"📈 Прокачка уровня\n\n"
            f"Текущий уровень: {user['level']}\n"
            f"Награда за клик: +{get_click_reward(user)}\n\n"
            f"Следующий уровень: {user['level'] + 1}\n"
            f"Награда за клик: +{next_reward}\n"
            f"💰 Цена: {cost} бумаги\n"
            f"💵 У тебя: {user['balance']}\n\n"
            f"Напиши: прокачать",
            reply_markup=kb
        )


@dp.callback_query(F.data == "reflink")
async def cb_reflink(cb: types.CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала /start", show_alert=True)
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE referrer = ?", (cb.from_user.id,))
    ref_count = c.fetchone()[0]
    c.execute("SELECT COALESCE(ref_earnings, 0) FROM users WHERE tg_id = ?", (cb.from_user.id,))
    ref_earnings = c.fetchone()[0]
    conn.close()
    kb = admin_keyboard() if is_admin(cb.from_user.id) else main_keyboard()
    await cb.answer()
    await cb.message.edit_text(
        f"🔗 Реферальная ссылка\n\n"
        f"https://t.me/Bioeb_bot?start={cb.from_user.id}\n\n"
        f"🤝 Приглашено: {ref_count} чел.\n"
        f"💰 Бонус за каждого: +{REFERRAL_BONUS} бумаги\n"
        f"💵 {REFERRAL_CLICK_PERCENT}% от каждого клика реферала!\n"
        f"🏆 Заработано с рефералов: {ref_earnings} бумаги",
        reply_markup=kb
    )


@dp.callback_query(F.data == "promo")
async def cb_promo(cb: types.CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала /start", show_alert=True)
        return
    kb = admin_keyboard() if is_admin(cb.from_user.id) else main_keyboard()
    await cb.answer()
    await cb.message.edit_text(
        "🎁 Промокод\n\n"
        "Напиши: промокод КОД\n"
        "Например: промокод BONUS500",
        reply_markup=kb
    )


@dp.callback_query(F.data == "mypaperid")
async def cb_mypaperid(cb: types.CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала /start", show_alert=True)
        return
    kb = admin_keyboard() if is_admin(cb.from_user.id) else main_keyboard()

    # Запрос к API: передаём Telegram ID (userSnids) — привязка paper_id не нужна
    try:
        result = await paper_api_call("users/get", {"userSnids": [str(cb.from_user.id)]})
    except Exception as e:
        await cb.answer("Ошибка API", show_alert=True)
        return

    await cb.answer()
    # PaperScroll API возвращает: {"ok": true, "documents": [{"_id": ..., "name": ..., ...}]}
    if isinstance(result, dict) and result.get("ok") is True:
        documents = result.get("documents", [])
        if isinstance(documents, list) and len(documents) > 0:
            data = documents[0]
        elif isinstance(documents, dict):
            data = documents
        else:
            data = {}
        pid = data.get("_id", user.get('paper_id') or "?")
        pname = data.get("name", "?")
        pbalance = data.get("balance", "недоступно")
        await cb.message.edit_text(
            f"🔍 Мой PaperScroll\n\n"
            f"🆔 ID: {pid}\n"
            f"👤 Имя: {pname}\n"
            f"💰 Баланс PaperScroll: {pbalance}",
            reply_markup=kb
        )
    else:
        msg = result.get("message", "") if isinstance(result, dict) else str(result)
        await cb.message.edit_text(
            f"🔍 Мой PaperScroll\n\n"
            f"Привязанный ID: {user.get('paper_id') or 'не привязан'}\n"
            f"❌ Не удалось получить данные: {msg}\n\n"
            f"Чтобы привязать — напиши: paperid 136",
            reply_markup=kb
        )


@dp.callback_query(F.data == "withdraw")
async def cb_withdraw(cb: types.CallbackQuery):
    if is_banned(cb.from_user.id):
        await cb.answer("🚫 Вы заблокированы!", show_alert=True)
        return
    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала /start", show_alert=True)
        return
    if not user['paper_id']:
        await cb.answer("Сначала привяжи PaperScroll ID!", show_alert=True)
        return
    if user['balance'] < 1:
        await cb.answer("Недостаточно бумаги для вывода!", show_alert=True)
        return
    await cb.answer()
    kb = admin_keyboard() if is_admin(cb.from_user.id) else main_keyboard()
    await cb.message.edit_text(
        f"💸 Вывод\n\n"
        f"💰 Доступно: {user['balance']} бумаги\n"
        f"🔗 PaperScroll ID: {user['paper_id']}\n"
        f"⚠️ Комиссия PaperScroll: 5%\n"
        f"При выводе 1000 получишь 950\n\n"
        f"Напиши: вывод 1000\n"
        f"(где 1000 — сколько вывести)",
        reply_markup=kb
    )


@dp.callback_query(F.data == "link")
async def cb_link(cb: types.CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала /start", show_alert=True)
        return
    kb = admin_keyboard() if is_admin(cb.from_user.id) else main_keyboard()
    if user['paper_id']:
        await cb.answer()
        await cb.message.edit_text(
            f"🔗 Твой PaperScroll ID: {user['paper_id']}\n\n"
            f"Чтобы изменить — напиши: paperid 136",
            reply_markup=kb
        )
    else:
        await cb.answer()
        await cb.message.edit_text(
            "🔗 Привязка PaperScroll\n\n"
            "Узнай свой ID в PaperScroll.\n"
            "Напиши: paperid 136\n"
            "(где 136 — твой ID)",
            reply_markup=kb
        )


# --- Текстовые команды ---
@dp.message(F.text.lower().startswith("paperid"))
async def handle_link_id(message: types.Message):
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: paperid 136")
        return
    paper_id = parts[1]
    update_user(message.from_user.id, paper_id=paper_id)
    await message.answer(f"✅ PaperScroll ID привязан: {paper_id}")


@dp.message(F.text.lower().startswith("вывод"))
async def handle_withdraw(message: types.Message):
    if not is_admin(message.from_user.id) and is_banned(message.from_user.id):
        await message.answer("🚫 Вы заблокированы!")
        return
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    if not user['paper_id']:
        await message.answer("❌ Сначала привяжи PaperScroll ID: paperid 136")
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: вывод 1000")
        return

    amount = parse_amount(parts[1])
    if amount <= 0:
        await message.answer("Сумма должна быть положительной. Примеры: 1000, 1к, 10к")
        return

    if amount > user['balance']:
        await message.answer(f"❌ Недостаточно бумаги. Доступно: {user['balance']}")
        return

    request_id = uuid.uuid4().hex
    payload = {
        "requestId": request_id,
        "to": [{"id": user['paper_id'], "direction": "Users", "amount": amount * 1000}],
        "dialog": {
            "sticker": "winner",
            "title": "Вывод из кликера",
            "label": "Вывод средств",
        },
    }

    try:
        result = await paper_api_call("transfers/create", payload)
    except Exception as e:
        await message.answer(f"❌ Ошибка соединения: {e}")
        return

    if isinstance(result, dict) and result.get("ok") is True:
        received = int(amount * 0.95)
        update_user(message.from_user.id, balance=user['balance'] - amount)
        await message.answer(
            f"✅ Вывод выполнен!\n"
            f"💸 Сумма: {amount} бумаги\n"
            f"👤 PaperScroll ID: {user['paper_id']}\n"
            f"📉 Комиссия: 5%\n"
            f"💰 Получено: {received}"
        )
    else:
        msg = result.get("message", "") if isinstance(result, dict) else str(result)
        await message.answer(f"❌ Ошибка вывода: {msg}")


@dp.message(F.text.lower().startswith("прокачать"))
async def handle_levelup(message: types.Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    cost = get_level_up_cost(user)
    if user['balance'] < cost:
        await message.answer(f"❌ Недостаточно бумаги! Нужно: {cost}, у тебя: {user['balance']}")
        return
    new_level = user['level'] + 1
    update_user(message.from_user.id, level=new_level, balance=user['balance'] - cost)
    await message.answer(
        f"🎉 Уровень повышен!\n"
        f"📈 Новый уровень: {new_level}\n"
        f"💵 Награда за клик: +{get_click_reward({**user, 'level': new_level})}\n"
        f"💰 Списано: {cost} бумаги\n"
        f"💵 Остаток: {user['balance'] - cost}"
    )


@dp.message(F.text.lower().startswith("промокод"))
async def handle_promo(message: types.Message):
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: промокод КОД")
        return
    code = parts[1].upper()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT reward, max_uses, used_count, is_active FROM promocodes WHERE code = ?", (code,))
    row = c.fetchone()
    if not row:
        conn.close()
        await message.answer("❌ Промокод не найден.")
        return
    reward, max_uses, used_count, is_active = row
    if not is_active:
        conn.close()
        await message.answer("❌ Промокод неактивен.")
        return
    if max_uses > 0 and used_count >= max_uses:
        conn.close()
        await message.answer("❌ Промокод исчерпан.")
        return
    c.execute("SELECT 1 FROM promo_used WHERE code = ? AND tg_id = ?", (code, message.from_user.id))
    if c.fetchone():
        conn.close()
        await message.answer("❌ Ты уже использовал этот промокод.")
        return
    # Активируем
    c.execute("INSERT INTO promo_used (code, tg_id) VALUES (?, ?)", (code, message.from_user.id))
    c.execute("UPDATE promocodes SET used_count = used_count + 1 WHERE code = ?", (code,))
    c.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (reward, message.from_user.id))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Промокод активирован!\n🎁 +{reward} бумаги на баланс!")



# --- МАГАЗИН ---
@dp.callback_query(F.data == "shop")
async def cb_shop(cb: types.CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала /start", show_alert=True)
        return
    kb = admin_keyboard() if is_admin(cb.from_user.id) else main_keyboard()
    boost = user.get("boost_click", 0)
    emax = get_energy_max(user)
    text = "🛒 Магазин бустов\n\n"
    text += f"💰 Ваш баланс: {user['balance']} бумаги\n"
    text += f"📈 Текущий буст клика: +{boost}%\n"
    text += f"⚡ Макс. энергия: {emax}\n\n"
    text += "Товары (бусты суммируются, цены растут на 75% после каждой покупки):\n\n"
    for key, item in SHOP_ITEMS.items():
        price = get_item_price(cb.from_user.id, key)
        text += f"{item['name']} — {price} бумаги\n"
        text += f"  Напиши: купить {key}\n"
    await cb.answer()
    await cb.message.edit_text(text, reply_markup=kb)


@dp.message(F.text.lower().startswith("купить"))
async def handle_buy(message: types.Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: купить boost15\nДоступные товары: boost15, boost30, boost50, boost100, energy50, energy100")
        return
    item_key = parts[1].lower()
    if item_key not in SHOP_ITEMS:
        await message.answer("❌ Товар не найден. Доступные: boost15, boost30, boost50, boost100, energy50, energy100")
        return
    item = SHOP_ITEMS[item_key]
    price = get_item_price(message.from_user.id, item_key)
    if user['balance'] < price:
        await message.answer(f"❌ Недостаточно бумаги! Нужно: {price}, у тебя: {user['balance']}")
        return
    # Списываем и выдаём
    new_balance = user['balance'] - price
    if item['type'] == "boost":
        new_boost = user.get('boost_click', 0) + item['value']
        update_user(message.from_user.id, balance=new_balance, boost_click=new_boost)
        await message.answer(
            f"✅ Куплено: {item['name']}\n"
            f"💰 Списано: {price} бумаги\n"
            f"📈 Новый буст клика: +{new_boost}%\n"
            f"💵 Остаток: {new_balance}"
        )
    elif item['type'] == "energy":
        new_energy_bonus = user.get('energy_bonus', 0) + item['value']
        update_user(message.from_user.id, balance=new_balance, energy_bonus=new_energy_bonus)
        await message.answer(
            f"✅ Куплено: {item['name']}\n"
            f"💰 Списано: {price} бумаги\n"
            f"⚡ Новая макс. энергия: {ENERGY_MAX + new_energy_bonus}\n"
            f"💵 Остаток: {new_balance}"
        )
    # Лог покупки + увеличение счётчика покупок для динамических цен
    next_price = int(price * (1 + SHOP_PRICE_INCREASE / 100))
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO shop_purchases (id, tg_id, item_key, price, created_at) VALUES (?, ?, ?, ?, ?)",
              (uuid.uuid4().hex, message.from_user.id, item_key, price, time.time()))
    # Увеличиваем счётчик покупок (без ON CONFLICT — работает на любой версии SQLite)
    c.execute("SELECT count FROM shop_user_purchases WHERE tg_id = ? AND item_key = ?",
              (message.from_user.id, item_key))
    row = c.fetchone()
    if row:
        c.execute("UPDATE shop_user_purchases SET count = count + 1 WHERE tg_id = ? AND item_key = ?",
                  (message.from_user.id, item_key))
    else:
        c.execute("INSERT INTO shop_user_purchases (tg_id, item_key, count) VALUES (?, ?, 1)",
                  (message.from_user.id, item_key))
    conn.commit()
    conn.close()
    await message.answer(f"📈 Следующая цена: {next_price} бумаги")


# --- ТОП ИГРОКОВ ---
@dp.callback_query(F.data == "top")
async def cb_top(cb: types.CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала /start", show_alert=True)
        return
    kb = admin_keyboard() if is_admin(cb.from_user.id) else main_keyboard()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username, balance, level, total_clicks FROM users WHERE is_banned = 0 ORDER BY balance DESC LIMIT 10")
    rows = c.fetchall()
    c.execute("SELECT COUNT(*) FROM users WHERE balance > ? AND is_banned = 0", (user['balance'],))
    rank = c.fetchone()[0] + 1
    conn.close()
    medals = ["🥇", "🥈", "🥉"]
    text = "🏆 Топ-10 игроков\n\n"
    for i, (uname, balance, level, clicks) in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i+1}."
        text += f"{medal} @{uname or '—'} — 💰{balance} | LVL{level} | 🖱{clicks}\n"
    text += f"\n📍 Ваша позиция: #{rank} (💰{user['balance']})"
    await cb.answer()
    await cb.message.edit_text(text, reply_markup=kb)


# --- ТОП КЛИКЕРОВ ДНЯ ---
@dp.callback_query(F.data == "top_clicks")
async def cb_top_clicks(cb: types.CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала /start", show_alert=True)
        return
    kb = admin_keyboard() if is_admin(cb.from_user.id) else main_keyboard()
    today = time.strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT dc.tg_id, dc.clicks, u.username FROM daily_clicks dc
                 JOIN users u ON dc.tg_id = u.tg_id
                 WHERE dc.date = ? AND u.is_banned = 0
                 ORDER BY dc.clicks DESC LIMIT 10""", (today,))
    rows = c.fetchall()
    c.execute("SELECT clicks FROM daily_clicks WHERE tg_id = ? AND date = ?", (cb.from_user.id, today))
    my_row = c.fetchone()
    my_clicks = my_row[0] if my_row else 0
    c.execute("SELECT COUNT(*) FROM daily_clicks WHERE date = ? AND clicks > ?", (today, my_clicks))
    my_rank = c.fetchone()[0] + 1
    conn.close()
    medals = ["🥇", "🥈", "🥉"]
    text = f"📅 Топ кликеров за сегодня\n\n"
    if not rows:
        text += "Пока никто не кликал сегодня. Будь первым! 🚀\n"
    else:
        for i, (tg_id, clicks, uname) in enumerate(rows):
            medal = medals[i] if i < 3 else f"{i+1}."
            text += f"{medal} @{uname or '—'} — 🖱{clicks}\n"
    text += f"\n📍 Вы за сегодня: 🖱{my_clicks} (место #{my_rank})\n"
    text += "\n🎁 Награды за день: 1 — 100к, 2 — 50к, 3 — 30к, 4 — 20к, 5 — 10к, 6-10 — 5к"
    await cb.answer()
    await cb.message.edit_text(text, reply_markup=kb)


# --- АДМИН: ЕЖЕДНЕВНАЯ НАГРАДА ---
@dp.message(F.text.lower().startswith("daily"))
async def admin_daily(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    today = time.strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM daily_rewards_given WHERE date = ?", (today,))
    if c.fetchone():
        conn.close()
        await message.answer("❌ Награда за сегодня уже выдана!")
        return
    c.execute("""SELECT dc.tg_id, dc.clicks, u.username FROM daily_clicks dc
                 JOIN users u ON dc.tg_id = u.tg_id
                 WHERE dc.date = ? AND u.is_banned = 0
                 ORDER BY dc.clicks DESC LIMIT 10""", (today,))
    rows = c.fetchall()
    if not rows:
        conn.close()
        await message.answer("❌ Сегодня никто не кликал. Нечего выдавать.")
        return
    c.execute("INSERT INTO daily_rewards_given (date, created_at) VALUES (?, ?)", (today, time.time()))
    text = "📅 Ежедневная награда за клики\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (tg_id, clicks, uname) in enumerate(rows):
        reward = DAILY_REWARDS[i] if i < len(DAILY_REWARDS) else 0
        if reward > 0:
            c.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (reward, tg_id))
            medal = medals[i] if i < 3 else f"{i+1}."
            text += f"{medal} @{uname or '—'} — 🖱{clicks} → +{reward} бумаги\n"
    conn.commit()
    conn.close()
    await message.answer(text)


# --- АДМИН КОМАНДЫ ---
@dp.message(F.text.lower().startswith("выдать"))
async def admin_give(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Формат: выдать TELEGRAM_ID_ИЛИ_@USERNAME СУММА\nНапример: выдать 6743070898 5000\nИли: выдать @MixReu 5000")
        return
    identifier = parts[1]
    amount = parse_amount(parts[2])
    if amount <= 0:
        await message.answer("Сумма должна быть положительной. Примеры: 5000, 1к, 10к, 1кк")
        return
    target = get_user_by_identifier(identifier)
    if not target:
        await message.answer(f"❌ Пользователь '{identifier}' не найден. Убедитесь, что он нажал /start.")
        return
    update_user(target['tg_id'], balance=target['balance'] + amount)
    await message.answer(
        f"✅ Выдано!\n"
        f"👤 Пользователь: @{target.get('username') or '—'} (ID: {target['tg_id']})\n"
        f"💰 Начислено: {amount}\n"
        f"💵 Новый баланс: {target['balance'] + amount}"
    )


@dp.message(F.text.lower().startswith("обнул"))
async def admin_reset(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: обнул TELEGRAM_ID_ИЛИ_@USERNAME\nНапример: обнул 6743070898\nИли: обнул @MixReu")
        return
    identifier = parts[1]
    target = get_user_by_identifier(identifier)
    if not target:
        await message.answer(f"❌ Пользователь '{identifier}' не найден.")
        return
    old_balance = target['balance']
    update_user(target['tg_id'], balance=0)
    await message.answer(
        f"♻️ Баланс обнулён!\n"
        f"👤 Пользователь: @{target.get('username') or '—'} (ID: {target['tg_id']})\n"
        f"💰 Было: {old_balance} → Стало: 0"
    )


@dp.message(F.text.lower().startswith("promo "))
async def admin_promo(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Формат: promo КОД НАГРАДА [МАКС_ИСПОЛЬЗОВАНИЙ]")
        return
    code = parts[1].upper()
    reward = parse_amount(parts[2])
    if reward <= 0:
        await message.answer("Награда должна быть положительной. Примеры: 500, 1к, 10к")
        return
    max_uses = int(parts[3]) if len(parts) > 3 else -1
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO promocodes (code, reward, max_uses, used_count, is_active, created_at) VALUES (?, ?, ?, 0, 1, ?)",
              (code, reward, max_uses, time.time()))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Промокод создан!\nКод: {code}\nНаграда: {reward}\nМакс: {'∞' if max_uses == -1 else max_uses}")


@dp.message(F.text.lower().startswith("promolist"))
async def admin_promolist(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code, reward, max_uses, used_count, is_active FROM promocodes")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await message.answer("Промокодов нет.")
        return
    text = "📋 Промокоды:\n\n"
    for code, reward, max_uses, used_count, is_active in rows:
        status = "✅" if is_active else "❌"
        limit = f"{used_count}/{max_uses}" if max_uses > 0 else f"{used_count}/∞"
        text += f"{status} {code} — {reward} бумаги (исп: {limit})\n"
    await message.answer(text)


@dp.message(F.text.lower().startswith("promodel"))
async def admin_promodel(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: promodel КОД")
        return
    code = parts[1].upper()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE promocodes SET is_active = 0 WHERE code = ?", (code,))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Промокод {code} деактивирован.")


@dp.message(F.text.lower().startswith("ban "))
async def admin_ban(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: ban TELEGRAM_ID_ИЛИ_@USERNAME")
        return
    target = get_user_by_identifier(parts[1])
    if not target:
        await message.answer(f"❌ Пользователь '{parts[1]}' не найден.")
        return
    if target['tg_id'] in ADMIN_IDS:
        await message.answer("❌ Нельзя забанить админа!")
        return
    update_user(target['tg_id'], is_banned=1)
    await message.answer(f"🚫 Пользователь @{target.get('username') or '—'} (ID: {target['tg_id']}) заблокирован.")


@dp.message(F.text.lower().startswith("unban "))
async def admin_unban(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: unban TELEGRAM_ID_ИЛИ_@USERNAME")
        return
    target = get_user_by_identifier(parts[1])
    if not target:
        await message.answer(f"❌ Пользователь '{parts[1]}' не найден.")
        return
    update_user(target['tg_id'], is_banned=0)
    await message.answer(f"✅ Пользователь @{target.get('username') or '—'} (ID: {target['tg_id']}) разблокирован.")


@dp.message(F.text.lower().startswith("users"))
async def admin_users(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT tg_id, username, balance, level, paper_id, is_banned, total_clicks FROM users ORDER BY balance DESC")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await message.answer("Пользователей нет.")
        return
    text = f"👥 Пользователи ({len(rows)}):\n\n"
    for i, (tg_id, username, balance, level, paper_id, is_banned, clicks) in enumerate(rows[:30]):
        ban = "🚫" if is_banned else "✅"
        pid = paper_id or "—"
        text += f"{ban} ID:{tg_id} @{username or '—'} | 💰{balance} | LVL{level} | 🖱{clicks} | PS:{pid}\n"
    if len(rows) > 30:
        text += f"\n... и ещё {len(rows) - 30} юзеров"
    await message.answer(text)


@dp.message(Command("topup"))
async def cmd_topup(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда только для админа.")
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: /topup 100000")
        return
    amount = parse_amount(parts[1])
    if amount <= 0:
        await message.answer("Сумма должна быть положительной. Примеры: 100000, 1кк, 500к")
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT total FROM bot_fund WHERE id = 1")
    row = c.fetchone()
    if row:
        c.execute("UPDATE bot_fund SET total = total + ? WHERE id = 1", (amount,))
    else:
        c.execute("INSERT INTO bot_fund (id, total) VALUES (1, ?)", (amount,))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Игровой фонд пополнен на {amount} бумаги.")


# --- АДМИН КНОПКИ ---
@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*), COALESCE(SUM(balance), 0), COALESCE(SUM(total_clicks), 0) FROM users")
    users_count, total_balance, total_clicks = c.fetchone()
    c.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    banned_count = c.fetchone()[0]
    c.execute("SELECT COALESCE(total, 0) FROM bot_fund WHERE id = 1")
    fund = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(price), 0) FROM shop_purchases")
    shop_revenue = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM shop_purchases")
    shop_purchases_count = c.fetchone()[0]
    conn.close()
    await cb.answer()
    await cb.message.edit_text(
        f"📋 Статистика бота\n\n"
        f"👥 Пользователей: {users_count}\n"
        f"🚫 Заблокировано: {banned_count}\n"
        f"💰 У игроков на руках: {total_balance}\n"
        f"👆 Всего кликов: {total_clicks}\n"
        f"🏦 Фонд бота: {fund}\n"
        f"🛒 Покупок в магазине: {shop_purchases_count} (на {shop_revenue} бумаги)",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "admin_topup")
async def cb_admin_topup(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text("📤 Напиши: /topup 100000", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "admin_users")
async def cb_admin_users(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT tg_id, username, balance, level, is_banned FROM users ORDER BY balance DESC LIMIT 20")
    rows = c.fetchall()
    conn.close()
    await cb.answer()
    text = f"👥 Пользователи (топ-20):\n\n"
    for tg_id, username, balance, level, is_banned in rows:
        ban = "🚫" if is_banned else "✅"
        text += f"{ban} ID:{tg_id} @{username or '—'} 💰{balance} LVL{level}\n"
    text += "\nПолный список: users"
    await cb.message.edit_text(text, reply_markup=admin_keyboard())


@dp.callback_query(F.data == "admin_bans")
async def cb_admin_bans(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT tg_id, username FROM users WHERE is_banned = 1")
    rows = c.fetchall()
    conn.close()
    await cb.answer()
    if not rows:
        await cb.message.edit_text("🚫 Заблокированных нет.\n\nКоманда: ban ID", reply_markup=admin_keyboard())
    else:
        text = "🚫 Заблокированные:\n\n"
        for tg_id, username in rows:
            text += f"ID:{tg_id} @{username or '—'} — unban {tg_id}\n"
        await cb.message.edit_text(text, reply_markup=admin_keyboard())


@dp.callback_query(F.data == "admin_daily")
async def cb_admin_daily(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    today = time.strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM daily_rewards_given WHERE date = ?", (today,))
    if c.fetchone():
        conn.close()
        await cb.answer("Награда за сегодня уже выдана!", show_alert=True)
        return
    c.execute("""SELECT dc.tg_id, dc.clicks, u.username FROM daily_clicks dc
                 JOIN users u ON dc.tg_id = u.tg_id
                 WHERE dc.date = ? AND u.is_banned = 0
                 ORDER BY dc.clicks DESC LIMIT 10""", (today,))
    rows = c.fetchall()
    if not rows:
        conn.close()
        await cb.answer("Сегодня никто не кликал!", show_alert=True)
        return
    c.execute("INSERT INTO daily_rewards_given (date, created_at) VALUES (?, ?)", (today, time.time()))
    text = "📅 Ежедневная награда за клики\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (tg_id, clicks, uname) in enumerate(rows):
        reward = DAILY_REWARDS[i] if i < len(DAILY_REWARDS) else 0
        if reward > 0:
            c.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (reward, tg_id))
            medal = medals[i] if i < 3 else f"{i+1}."
            text += f"{medal} @{uname or '—'} — 🖱{clicks} → +{reward} бумаги\n"
    conn.commit()
    conn.close()
    await cb.answer("Награды выданы!", show_alert=False)
    await cb.message.edit_text(text, reply_markup=admin_keyboard())


# --- ОБЩАЯ ФУНКЦИЯ ВЫДАЧИ НАГРАД ---
def give_daily_rewards(date_str: str) -> str:
    """Выдаёт награды топ кликерам за указанную дату. Возвращает текст результата."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Проверка: уже выдавали за эту дату?
    c.execute("SELECT 1 FROM daily_rewards_given WHERE date = ?", (date_str,))
    if c.fetchone():
        conn.close()
        return f"❌ Награда за {date_str} уже выдана!"
    # Топ кликеров за указанную дату
    c.execute("""SELECT dc.tg_id, dc.clicks, u.username FROM daily_clicks dc
                 JOIN users u ON dc.tg_id = u.tg_id
                 WHERE dc.date = ? AND u.is_banned = 0
                 ORDER BY dc.clicks DESC LIMIT 10""", (date_str,))
    rows = c.fetchall()
    if not rows:
        conn.close()
        return f"❌ За {date_str} никто не кликал. Нечего выдавать."
    c.execute("INSERT INTO daily_rewards_given (date, created_at) VALUES (?, ?)", (date_str, time.time()))
    text = f"📅 Ежедневная награда за {date_str}\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (tg_id, clicks, uname) in enumerate(rows):
        reward = DAILY_REWARDS[i] if i < len(DAILY_REWARDS) else 0
        if reward > 0:
            c.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (reward, tg_id))
            medal = medals[i] if i < 3 else f"{i+1}."
            text += f"{medal} @{uname or '—'} — 🖱{clicks} → +{reward} бумаги\n"
    conn.commit()
    conn.close()
    return text


async def daily_reward_scheduler():
    """Фоновая задача: каждый день в 00:00 выдаёт награды за завершившийся день."""
    while True:
        now = datetime.datetime.now()
        # Сколько секунд до следующей полуночи
        tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait_seconds = (tomorrow - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        # В 00:00 выдаём награды за вчера
        yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        result = give_daily_rewards(yesterday)
        # Пишем всем админам
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, f"⏰ Авто-выдача наград (00:00)\n\n{result}")
            except Exception:
                pass


# --- FALLBACK: НЕПОНЯТНЫЕ СООБЩЕНИЯ ---
@dp.message(F.text)
async def fallback_message(message: types.Message):
    kb = admin_keyboard() if is_admin(message.from_user.id) else main_keyboard()
    await message.answer(
        "🤔 Не понял команду. Используй кнопки ниже!\n\n"
        "• прокачать — повысить уровень\n"
        "• купить boost15 — покупка в магазине\n"
        "• промокод КОД — активировать промокод\n"
        "• paperid 136 — привязать PaperScroll ID\n"
        "• вывод 1000 — вывести бумагу",
        reply_markup=kb
    )


async def main():
    # Запускаем фоновый планировщик авто-выдачи наград
    asyncio.create_task(daily_reward_scheduler())
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
