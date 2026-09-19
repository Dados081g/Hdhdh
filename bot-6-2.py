import asyncio
import hmac
import html
import logging
import os
import re
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    ReplyKeyboardRemove,
)

# ══════════════════════════ НАСТРОЙКИ ══════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "8763383205:AAEBAEq_AHc7AR2ReuEnLtzaDtfUIr2roPI)

# Вход в админку: команда  /admin ПАРОЛЬ
# После первого входа бот запоминает вас, дальше хватает просто /admin.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "maksumtop1")

# Необязательно: можно сразу прописать Telegram ID админов, например {111111111}
ADMIN_IDS = set()
if os.getenv("ADMIN_IDS"):
    ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS").split(",") if x.strip().isdigit()}

# База данных (создаётся автоматически рядом с bot.py). НЕ УДАЛЯЙТЕ её —
# в ней товары, ключи, пользователи и заказы.
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).resolve().parent / "shop.db"))

# Часовой пояс для статистики оборота: 3 = Москва (UTC+3). Для Екатеринбурга 5 и т.д.
TZ_OFFSET = 3

REVIEWS_URL = "https://t.me/+uU61ylClWhI0YWMy"
POLICY_URL = "https://telegra.ph/Politika-konfidencialnosti-09-18-102"
TERMS_URL = "https://telegra.ph/Polzovatelskoe-soglashenie-09-18-64"
PRIVATE_URL = "https://t.me/+aO0aEfw38GgwYWRi"  # приватка со всеми файлами (выдаётся после оплаты)
SUPPORT_TEXT = "Здравствуйте если у вас возникла проблема то отпишитесь сюда @Forevebz"

# Цвета кнопок в Telegram: danger = красная, success = зелёная, primary = синяя
# (розового цвета у кнопок нет).
RED, GREEN, BLUE = "danger", "success", "primary"

# Эмодзи на кнопках каталога
EMOJI_DEVICE = "5258514780469075716"
EMOJI_PRODUCT = "5399986364634641475"

# Премиум-эмодзи в сообщении об успешной оплате
PAY_EMOJI_OK = "5399986364634641475"       # заголовок «Покупка успешна»
PAY_EMOJI_PRODUCT = "5942734685976138521"  # название товара
PAY_EMOJI_TERM = "5775896410780079073"     # срок
PAY_EMOJI_PRICE = "5258514780469075716"    # списано
PAY_EMOJI_KEY = "5280640845660330048"      # ключ

# Разделы, куда админ может поставить баннер
BANNER_SLOTS = {
    "main": "Главное меню",
    "games": "Выбор игры",
    "devices": "Выбор устройства",
    "profile": "Профиль",
}

log = logging.getLogger("shop")
esc = html.escape

# ═══ DB-START ═══════════════════ БАЗА ДАННЫХ ═════════════════════
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row


def q(sql, *args):
    return db.execute(sql, args).fetchall()


def q1(sql, *args):
    return db.execute(sql, args).fetchone()


def scalar(sql, *args):
    r = db.execute(sql, args).fetchone()
    return r[0] if r else None


def run(sql, *args):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def init_db():
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0,
            created TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS games(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT COLLATE NOCASE NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS devices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            name TEXT COLLATE NOCASE NOT NULL,
            title TEXT NOT NULL,
            UNIQUE(game_id, name)
        );
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            title TEXT NOT NULL,
            days INTEGER NOT NULL,
            price INTEGER NOT NULL,
            review_url TEXT
        );
        CREATE TABLE IF NOT EXISTS stock(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            price INTEGER NOT NULL,
            code TEXT NOT NULL,
            created TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS promos(
            code TEXT PRIMARY KEY,
            amount INTEGER NOT NULL,
            max_uses INTEGER NOT NULL,
            used_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS promo_uses(
            code TEXT,
            user_id INTEGER,
            PRIMARY KEY(code, user_id)
        );
        CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER UNIQUE,
            user_id INTEGER NOT NULL,
            author TEXT,
            product_name TEXT,
            rating INTEGER NOT NULL,
            text TEXT NOT NULL,
            created TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    db.commit()
    for r in q("SELECT id FROM admins"):
        ADMIN_IDS.add(r["id"])
    seed()


def add_admin(uid):
    run("INSERT OR IGNORE INTO admins(id) VALUES(?)", uid)
    ADMIN_IDS.add(uid)


def get_setting(k):
    return scalar("SELECT v FROM settings WHERE k=?", k)


def set_setting(k, v):
    run("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", k, v)


def get_banner(slot):
    return get_setting(f"banner:{slot}")


def product_slot(pid):
    """Баннер конкретного товара, а если не задан — общий «Выбор устройства»."""
    return f"product:{pid}" if get_banner(f"product:{pid}") else "devices"


def ensure_user(uid):
    run("INSERT OR IGNORE INTO users(id) VALUES(?)", uid)


def get_balance(uid):
    return scalar("SELECT balance FROM users WHERE id=?", uid) or 0


def get_or_create_game(name):
    r = q1("SELECT id FROM games WHERE name=?", name)
    return r["id"] if r else run("INSERT INTO games(name) VALUES(?)", name).lastrowid


def get_or_create_device(game_id, name):
    r = q1("SELECT id FROM devices WHERE game_id=? AND name=?", game_id, name)
    if r:
        return r["id"]
    g = q1("SELECT name FROM games WHERE id=?", game_id)
    title = f"{g['name'].upper()} · {name}"
    return run(
        "INSERT INTO devices(game_id, name, title) VALUES(?, ?, ?)", game_id, name, title
    ).lastrowid


def add_product(device_id, name, days, price, review_url):
    r = q1(
        "SELECT g.name AS g, d.name AS d FROM devices d "
        "JOIN games g ON g.id = d.game_id WHERE d.id=?",
        device_id,
    )
    title = f"{r['g']} {r['d']}".upper()
    return run(
        "INSERT INTO products(device_id, name, title, days, price, review_url) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        device_id, name, title, days, price, review_url,
    ).lastrowid


def seed():
    """Стартовый товар (из вашего ТЗ). Создаётся один раз."""
    if get_setting("seeded"):
        return
    gid = get_or_create_game("Oxide")
    did = run(
        "INSERT INTO devices(game_id, name, title) VALUES(?, ?, ?)",
        gid, "Android Non Root", "OXIDE · Android • NROOT",
    ).lastrowid
    run(
        "INSERT INTO products(device_id, name, title, days, price, review_url) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        did, "Cry4me 1D", "OXIDE ANDROID", 1, 160, "https://t.me/ozorcry4me/5?single",
    )
    set_setting("seeded", "1")


def _off():
    return f"{TZ_OFFSET:+d} hours"


def turnover():
    """(заказов, сумма) за сегодня / 7 дней / 30 дней / всё время. Время — по TZ_OFFSET."""
    off = _off()
    out = {}
    for key, days in (("day", 1), ("week", 7), ("month", 30)):
        r = q1(
            "SELECT COUNT(*) AS c, COALESCE(SUM(price), 0) AS s FROM orders "
            "WHERE date(created, ?) >= date('now', ?, ?)",
            off, off, f"-{days - 1} days",
        )
        out[key] = (r["c"], r["s"])
    r = q1("SELECT COUNT(*) AS c, COALESCE(SUM(price), 0) AS s FROM orders")
    out["all"] = (r["c"], r["s"])
    return out


def daily_turnover(n=7):
    """[(дата, заказов, сумма), ...] за последние n дней, сегодня первым."""
    off = _off()
    today = date.fromisoformat(scalar("SELECT date('now', ?)", off))
    found = {
        r["d"]: (r["c"], r["s"])
        for r in q(
            "SELECT date(created, ?) AS d, COUNT(*) AS c, SUM(price) AS s FROM orders "
            "WHERE date(created, ?) >= date('now', ?, ?) GROUP BY d",
            off, off, off, f"-{n - 1} days",
        )
    }
    result = []
    for i in range(n):
        d = today - timedelta(days=i)
        c, total = found.get(d.isoformat(), (0, 0))
        result.append((d, c, total))
    return result


def add_keys(product_id, codes):
    existing = {r[0] for r in db.execute("SELECT code FROM stock WHERE product_id=?", (product_id,))}
    added = dup = 0
    for c in codes:
        if c in existing:
            dup += 1
            continue
        existing.add(c)
        db.execute("INSERT INTO stock(product_id, code) VALUES(?, ?)", (product_id, c))
        added += 1
    db.commit()
    return added, dup


def stock_count(product_id):
    return scalar("SELECT COUNT(*) FROM stock WHERE product_id=? AND used=0", product_id) or 0


def purchase(uid, product_id):
    """Покупка с баланса + автовыдача ключа. Возвращает (статус, товар, ключ, id заказа)."""
    p = q1("SELECT * FROM products WHERE id=?", product_id)
    if not p:
        return "no_product", None, None, None
    s = q1(
        "SELECT id, code FROM stock WHERE product_id=? AND used=0 ORDER BY id LIMIT 1",
        product_id,
    )
    if not s:
        return "no_stock", p, None, None
    if get_balance(uid) < p["price"]:
        return "no_funds", p, None, None
    db.execute("UPDATE stock SET used=1 WHERE id=?", (s["id"],))
    db.execute("UPDATE users SET balance = balance - ? WHERE id=?", (p["price"], uid))
    cur = db.execute(
        "INSERT INTO orders(user_id, product_name, price, code) VALUES(?, ?, ?, ?)",
        (uid, f"{p['title']} · {p['name']}", p["price"], s["code"]),
    )
    db.commit()
    return "ok", p, s["code"], cur.lastrowid


def redeem_promo(uid, code):
    """Активация промокода (зачисляет сумму на баланс). Возвращает (успех, текст)."""
    code = code.strip().upper()
    row = q1("SELECT * FROM promos WHERE code=?", code)
    if not row:
        return False, "Промокод не найден."
    if row["used_count"] >= row["max_uses"]:
        return False, "Лимит активаций этого промокода исчерпан."
    if q1("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", code, uid):
        return False, "Вы уже использовали этот промокод."
    db.execute("INSERT INTO promo_uses(code, user_id) VALUES(?, ?)", (code, uid))
    db.execute("UPDATE promos SET used_count = used_count + 1 WHERE code=?", (code,))
    db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (row["amount"], uid))
    db.commit()
    return True, f"Промокод активирован! На баланс зачислено {row['amount']} ₽."
# ═══ DB-END ═════════════════════════════════════════════════════


# ══════════════════════════ ТЕКСТЫ И КНОПКИ ══════════════════════════
def em(emoji_id: str, fallback: str) -> str:
    """Премиум-эмодзи в тексте сообщения."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def days_text(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def ib(text, data=None, emoji=None, style=None, url=None):
    """Кнопка. Если цвет не указан — по умолчанию синяя (так не остаётся кнопок без цвета)."""
    return InlineKeyboardButton(
        text=text,
        callback_data=data,
        url=url,
        icon_custom_emoji_id=emoji,
        style=style or BLUE,
    )


def kb(*buttons) -> InlineKeyboardMarkup:
    """Кнопки друг под другом, на всю ширину."""
    return InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons])


LINE = "➖➖➖➖➖➖➖➖➖➖➖➖"

WELCOME_TEXT = (
    f"{em('5280475364865381482', '💎')} Добро пожаловать!\n\n"
    "Вы попали в магазин игровых утилит. Здесь можно пополнить баланс "
    "и купить нужный товар в пару нажатий.\n\n"
    f"{em('5258024802010026053', '🛒')} Каталог — весь ассортимент\n"
    f"{em('5886285355279193209', '👛')} Профиль — баланс, заказы, промокоды\n"
    f"{em('5983580310292402968', '🎙')} Поддержка — если что-то пошло не так\n\n"
    f"{em('5766994197705921104', '☝️')} Перед покупкой ознакомьтесь с правилами "
    "и политикой конфиденциальности — они в разделе «Профиль»."
)

CATALOG_TEXT = (
    f"{em('5258024802010026053', '🛒')} Выберите вашу игру\n\n"
    f"{em('5280475364865381482', '☝️')} Все разделы магазина — на кнопках ниже."
)

MAIN_KB = kb(
    ib("Каталог", "go:catalog", emoji="5229064374403998351", style=RED),
    ib("Мой профиль", "go:profile", emoji="5904630315946611415", style=BLUE),
    ib("Отзывы", url=REVIEWS_URL, emoji="5280472706280622601", style=GREEN),
    ib("Поддержка", "go:support", emoji="5904630315946611415", style=BLUE),
)

CANCEL_KB = kb(ib("Отмена", "a:cancel", style=RED))


# ══════════════════════════ ЭКРАНЫ ПОЛЬЗОВАТЕЛЯ ══════════════════════════
# Каждый экран возвращает (текст, клавиатура, слот_баннера) или None.
def screen_main():
    return WELCOME_TEXT, MAIN_KB, "main"


def screen_catalog():
    games = q(
        "SELECT g.id, g.name FROM games g WHERE EXISTS ("
        "SELECT 1 FROM devices d JOIN products p ON p.device_id = d.id "
        "WHERE d.game_id = g.id) ORDER BY g.id"
    )
    if not games:
        return (
            f"{em('5258024802010026053', '🛒')} Каталог пока пуст.",
            kb(ib("Главное меню", "go:main")),
            "games",
        )
    buttons = [ib(g["name"], f"g:{g['id']}", style=RED) for g in games]
    buttons.append(ib("Главное меню", "go:main"))
    return CATALOG_TEXT, kb(*buttons), "games"


def screen_game(gid):
    g = q1("SELECT * FROM games WHERE id=?", gid)
    devs = q(
        "SELECT d.* FROM devices d WHERE d.game_id=? AND EXISTS ("
        "SELECT 1 FROM products p WHERE p.device_id = d.id) ORDER BY d.id",
        gid,
    )
    if not g or not devs:
        return None
    text = (
        f"{em('5258508428212445001', '🎮')} {esc(g['name'].upper())}\n"
        f"{LINE}\n"
        f"{em('5258514780469075716', '📥')} Выберите ваше устройство:"
    )
    buttons = [ib(d["name"], f"d:{d['id']}", emoji=EMOJI_DEVICE, style=RED) for d in devs]
    buttons.append(ib("Назад", "go:catalog"))
    return text, kb(*buttons), "devices"


def screen_device(did):
    d = q1("SELECT * FROM devices WHERE id=?", did)
    prods = q("SELECT * FROM products WHERE device_id=? ORDER BY id", did)
    if not d or not prods:
        return None
    text = (
        f"{em('5942734685976138521', '🖥')} {esc(d['title'])}\n"
        f"{LINE}\n"
        f"{em('5280640845660330048', '🔎')} Ознакомьтесь с тарифами — "
        "у каждого свой функционал и срок."
    )
    buttons = [
        ib(f"{p['name']} {p['price']} руб", f"p:{p['id']}", emoji=EMOJI_PRODUCT, style=RED)
        for p in prods
    ]
    buttons.append(ib("Назад", f"g:{d['game_id']}"))
    return text, kb(*buttons), "devices"


def card_text(p):
    return (
        f"{esc(p['title'])}\n"
        f"{LINE}\n"
        f"{em('5775896410780079073', '🕓')} Срок: {days_text(p['days'])}\n"
        f"{em('5231449120635370684', '💸')} Цена: {p['price']} ₽"
    )


def screen_card(pid):
    p = q1("SELECT * FROM products WHERE id=?", pid)
    if not p:
        return None
    buttons = [ib(f"Купить за {p['price']}Р", f"buy:{pid}", emoji="5258024802010026053", style=GREEN)]
    if p["review_url"]:
        buttons.append(ib("Обзор", url=p["review_url"]))
    buttons.append(ib("Назад", f"d:{p['device_id']}"))
    return card_text(p), kb(*buttons), product_slot(pid)


def screen_pay(pid, uid):
    p = q1("SELECT * FROM products WHERE id=?", pid)
    if not p:
        return None
    markup = kb(
        ib("Оплатить по СБП", f"pay:sbp:{pid}", emoji="5280973778640211229", style=GREEN),
        ib(f"Оплатить с баланса ({get_balance(uid)} ₽)", f"pay:bal:{pid}",
           emoji="5886285355279193209", style=BLUE),
        ib("Назад", f"p:{pid}"),
    )
    return card_text(p), markup, product_slot(pid)


def screen_profile(uid):
    orders = scalar("SELECT COUNT(*) FROM orders WHERE user_id=?", uid) or 0
    text = (
        f"{em('5886285355279193209', '👛')} Профиль\n\n"
        f"ID: {uid}\n"
        f"Баланс: {get_balance(uid)} ₽\n"
        f"Заказов: {orders}"
    )
    markup = kb(
        ib("Промокод", "promo:ask", style=BLUE),
        ib("Политика конфиденциальности", "go:policy", style=RED),
        ib("Главное меню", "go:main"),
    )
    return text, markup, "profile"


def screen_policy():
    text = (
        "Политика конфиденциальности\n\n"
        f"{em('5766994197705921104', '☝️')} Перед покупкой ознакомьтесь с документами "
        "по кнопкам ниже."
    )
    markup = kb(
        ib("Политика конфиденциальности", url=POLICY_URL, style=GREEN),
        ib("Пользовательское соглашение", url=TERMS_URL, style=GREEN),
        ib("Назад", "go:profile"),
    )
    return text, markup, "profile"


def screen_support():
    return SUPPORT_TEXT, kb(ib("Главное меню", "go:main")), "main"


def build_screen(data: str, uid: int):
    kind, _, arg = data.partition(":")
    if kind == "go":
        if arg == "main":
            return screen_main()
        if arg == "catalog":
            return screen_catalog()
        if arg == "profile":
            return screen_profile(uid)
        if arg == "policy":
            return screen_policy()
        if arg == "support":
            return screen_support()
    elif kind == "g":
        return screen_game(int(arg))
    elif kind == "d":
        return screen_device(int(arg))
    elif kind == "p":
        return screen_card(int(arg))
    elif kind == "buy":
        return screen_pay(int(arg), uid)
    return None


# ══════════════════════════ ОТПРАВКА / РЕДАКТИРОВАНИЕ ══════════════════════════
async def send_screen(msg: Message, text, markup, slot=None):
    """Новое сообщение (с баннером, если он установлен)."""
    photo = get_banner(slot) if slot else None
    if photo:
        try:
            return await msg.answer_photo(photo, caption=text, reply_markup=markup)
        except TelegramBadRequest:
            log.exception("Не удалось отправить баннер %s", slot)
    return await msg.answer(text, reply_markup=markup)


async def show(msg: Message, text, markup, slot=None):
    """Заменить содержимое сообщения (текст ↔ баннер переключается сам)."""
    photo = get_banner(slot) if slot else None
    try:
        if photo and msg.photo:
            await msg.edit_media(
                InputMediaPhoto(media=photo, caption=text, parse_mode=ParseMode.HTML),
                reply_markup=markup,
            )
            return
        if not photo and not msg.photo:
            await msg.edit_text(text, reply_markup=markup)
            return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            try:
                await msg.edit_reply_markup(reply_markup=markup)
            except TelegramBadRequest:
                pass
            return
        log.warning("edit failed, resend: %s", e)
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    await send_screen(msg, text, markup, slot)


# ══════════════════════════ FSM-СОСТОЯНИЯ ══════════════════════════
class PromoState(StatesGroup):
    code = State()


class ReviewState(StatesGroup):
    text = State()


class AddProduct(StatesGroup):
    game = State()
    device = State()
    name = State()
    days = State()
    price = State()
    review = State()


class EditProduct(StatesGroup):
    value = State()


class AddKeys(StatesGroup):
    keys = State()


class SetBanner(StatesGroup):
    photo = State()


class Broadcast(StatesGroup):
    msg = State()


class NewPromo(StatesGroup):
    code = State()
    amount = State()
    uses = State()


# ══════════════════════════ КОМАНДЫ ══════════════════════════
common = Router()
admin_r = Router()
user_r = Router()

admin_r.message.filter(lambda m: m.from_user.id in ADMIN_IDS)
admin_r.callback_query.filter(lambda c: c.from_user.id in ADMIN_IDS)

# защита от подбора пароля: 5 неверных попыток за 10 минут → блокировка
_fails: dict[int, list[float]] = {}


def too_many_fails(uid: int) -> bool:
    now = time.time()
    _fails[uid] = [t for t in _fails.get(uid, []) if now - t < 600]
    return len(_fails[uid]) >= 5


@common.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    ensure_user(msg.from_user.id)
    # убираем старую клавиатуру внизу экрана (если осталась от прошлых версий)
    tmp = await msg.answer("⏳", reply_markup=ReplyKeyboardRemove())
    try:
        await tmp.delete()
    except TelegramBadRequest:
        pass
    await send_screen(msg, *screen_main())


@common.message(Command("id"))
async def cmd_id(msg: Message):
    await msg.answer(f"Ваш Telegram ID: <code>{msg.from_user.id}</code>")


@common.message(Command("admin"))
async def cmd_admin(msg: Message, command: CommandObject, state: FSMContext):
    uid = msg.from_user.id
    password = (command.args or "").strip()

    if password:  # прячем сообщение с паролем
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass

    if uid not in ADMIN_IDS:
        if not password:
            return  # обычным пользователям бот не отвечает
        if too_many_fails(uid):
            await msg.answer("Слишком много попыток. Попробуйте через 10 минут.")
            return
        if not hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
            _fails.setdefault(uid, []).append(time.time())
            await msg.answer("Неверный пароль.")
            return
        add_admin(uid)
        await msg.answer("✅ Доступ открыт.")

    await state.clear()
    await send_screen(msg, *screen_admin())


# ══════════════════════════ ПОЛЬЗОВАТЕЛЬ ══════════════════════════
@user_r.callback_query(F.data.regexp(r"^(go|g|d|p|buy):"))
async def navigate(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    ensure_user(cb.from_user.id)
    scr = build_screen(cb.data, cb.from_user.id)
    if scr is None:
        await cb.answer("Этот раздел больше недоступен.", show_alert=True)
        scr = screen_catalog()
    else:
        await cb.answer()
    await show(cb.message, *scr)


@user_r.callback_query(F.data == "promo:ask")
async def promo_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PromoState.code)
    await cb.answer()
    await show(cb.message, "Введите промокод:", kb(ib("Назад", "go:profile")), "profile")


@user_r.message(PromoState.code, F.text)
async def promo_apply(msg: Message, state: FSMContext):
    await state.clear()
    ensure_user(msg.from_user.id)
    ok, text = redeem_promo(msg.from_user.id, msg.text)
    buttons = [] if ok else [ib("Ввести ещё раз", "promo:ask", style=BLUE)]
    buttons += [ib("Профиль", "go:profile"), ib("Главное меню", "go:main")]
    await msg.answer(esc(text), reply_markup=kb(*buttons))


@user_r.callback_query(F.data.startswith("pay:sbp:"))
async def pay_sbp(cb: CallbackQuery):
    # TODO: создать платёж по СБП в вашей платёжной системе. После успешной
    # оплаты зачислите сумму на баланс: UPDATE users SET balance = balance + ...
    await cb.answer("Оплата по СБП пока не подключена.", show_alert=True)


def success_text(p, code) -> str:
    return (
        f"{em(PAY_EMOJI_OK, '✅')} Покупка успешна\n\n"
        f"{em(PAY_EMOJI_PRODUCT, '🖥')} {esc(p['title'])} · {esc(p['name'])}\n"
        f"{em(PAY_EMOJI_TERM, '🕓')} Срок: {days_text(p['days'])}\n"
        f"{em(PAY_EMOJI_PRICE, '💸')} Списано: {p['price']} ₽\n\n"
        f"{em(PAY_EMOJI_KEY, '🔑')} Ваш ключ:\n<code>{esc(code)}</code>\n\n"
        f'Зайдите в <a href="{PRIVATE_URL}">приватку</a> — там все файлы.'
    )


def success_kb() -> InlineKeyboardMarkup:
    return kb(
        ib("Перейти в приватку", url=PRIVATE_URL, style=GREEN),
        ib("Главное меню", "go:main"),
    )


REVIEW_ASK_TEXT = (
    "⭐ Оцените покупку\n\n"
    "Поставьте оценку от 1 до 5 звёзд — нам важно ваше мнение."
)


def stars_kb(oid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib(f"{n}⭐", f"rv:{oid}:{n}") for n in range(1, 6)],
            [ib("Пропустить", "rv:skip")],
        ]
    )


@user_r.callback_query(F.data.startswith("pay:bal:"))
async def pay_balance(cb: CallbackQuery):
    uid = cb.from_user.id
    ensure_user(uid)
    status, p, code, oid = purchase(uid, int(cb.data.split(":")[2]))
    if status == "no_product":
        await cb.answer("Этот товар больше недоступен.", show_alert=True)
        return
    if status == "no_stock":
        await cb.answer("Товар временно закончился. Попробуйте позже.", show_alert=True)
        return
    if status == "no_funds":
        await cb.answer("Недостаточно средств на балансе.", show_alert=True)
        return
    await cb.answer("Покупка успешна!")
    await show(cb.message, success_text(p, code), success_kb(), None)
    # после выдачи ключа предлагаем оставить отзыв
    await cb.message.answer(REVIEW_ASK_TEXT, reply_markup=stars_kb(oid))


# ══════════════════════════ ОТЗЫВЫ ══════════════════════════
def author_name(u) -> str:
    return f"{u.full_name} (@{u.username})" if u.username else u.full_name


@user_r.callback_query(F.data.regexp(r"^rv:\d+:[1-5]$"))
async def review_rate(cb: CallbackQuery, state: FSMContext):
    _, oid, stars = cb.data.split(":")
    oid, stars = int(oid), int(stars)
    if not q1("SELECT 1 FROM orders WHERE id=? AND user_id=?", oid, cb.from_user.id):
        await cb.answer("Заказ не найден.", show_alert=True)
        return
    if q1("SELECT 1 FROM reviews WHERE order_id=?", oid):
        await cb.answer("Вы уже оставили отзыв на этот заказ. Спасибо!", show_alert=True)
        return
    await state.set_state(ReviewState.text)
    await state.update_data(oid=oid, stars=stars)
    await cb.answer()
    await show(
        cb.message,
        f"{'⭐' * stars} Ваша оценка: {stars}/5\n\n"
        "Теперь напишите отзыв одним сообщением:",
        kb(ib("Изменить оценку", f"rv:re:{oid}"), ib("Пропустить", "rv:skip")),
        None,
    )


@user_r.callback_query(F.data.regexp(r"^rv:re:\d+$"))
async def review_rerate(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, REVIEW_ASK_TEXT, stars_kb(int(cb.data.split(":")[2])), None)


@user_r.callback_query(F.data == "rv:skip")
async def review_skip(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, "Спасибо за покупку! 🙌", kb(ib("Главное меню", "go:main")), None)


@user_r.message(ReviewState.text, F.text)
async def review_text(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    oid, stars = data.get("oid"), data.get("stars")
    text = msg.text.strip()[:1000]
    order = q1("SELECT * FROM orders WHERE id=? AND user_id=?", oid, msg.from_user.id)
    if not order or not stars:
        await state.clear()
        await msg.answer("Не получилось найти заказ. Попробуйте ещё раз после следующей покупки.")
        return
    if q1("SELECT 1 FROM reviews WHERE order_id=?", oid):
        await state.clear()
        await msg.answer("Вы уже оставили отзыв на этот заказ. Спасибо!")
        return
    author = author_name(msg.from_user)
    run(
        "INSERT INTO reviews(order_id, user_id, author, product_name, rating, text) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        oid, msg.from_user.id, author, order["product_name"], stars, text,
    )
    await state.clear()
    await msg.answer(
        "Спасибо за отзыв! Он уже у нас 🙌", reply_markup=kb(ib("Главное меню", "go:main"))
    )
    # отзыв — всем админам в личку
    notice = (
        f"⭐ Новый отзыв · {'⭐' * stars} {stars}/5\n\n"
        f"Товар: {esc(order['product_name'])}\n"
        f"От: {esc(author)} · ID <code>{msg.from_user.id}</code>\n\n"
        f"{esc(text)}"
    )
    for aid in list(ADMIN_IDS):
        try:
            await bot.send_message(aid, notice, reply_markup=kb(ib("⭐ Все отзывы", "a:rv")))
        except Exception:
            log.warning("Не удалось отправить отзыв админу %s", aid)


@user_r.message(ReviewState.text)
async def review_not_text(msg: Message):
    await msg.answer("Напишите отзыв текстом, пожалуйста.", reply_markup=kb(ib("Пропустить", "rv:skip")))


# ══════════════════════════ АДМИН-ПАНЕЛЬ: ЭКРАНЫ ══════════════════════════
def screen_admin():
    users = scalar("SELECT COUNT(*) FROM users") or 0
    orders = scalar("SELECT COUNT(*) FROM orders") or 0
    text = f"🛠 Админ-панель\n\nПользователей: {users}\nЗаказов: {orders}"
    markup = kb(
        ib("📊 Оборот", "a:stats", style=GREEN),
        ib("📦 Товары", "a:prods"),
        ib("🖼 Баннеры", "a:bn"),
        ib("📣 Объявление", "a:bc"),
        ib("🎁 Промокоды", "a:pr"),
        ib("⭐ Отзывы", "a:rv"),
    )
    return text, markup, None


WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


# период: (название кнопки, дней, подпись «сейчас», подпись «раньше»)
STATS_PERIODS = {
    "day": ("День", 1, "Сегодня", "Вчера"),
    "week": ("Неделя", 7, "Последние 7 дней", "Предыдущие 7 дней"),
    "month": ("Месяц", 30, "Последние 30 дней", "Предыдущие 30 дней"),
}


def money(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def period_report(days):
    """Оборот за последние `days` дней (включая сегодня), предыдущий такой же период и топ товаров."""
    off = _off()
    start = f"-{days - 1} days"
    cur = q1(
        "SELECT COUNT(*) AS c, COALESCE(SUM(price), 0) AS s FROM orders "
        "WHERE date(created, ?) >= date('now', ?, ?)",
        off, off, start,
    )
    prev = q1(
        "SELECT COUNT(*) AS c, COALESCE(SUM(price), 0) AS s FROM orders "
        "WHERE date(created, ?) BETWEEN date('now', ?, ?) AND date('now', ?, ?)",
        off, off, f"-{days * 2 - 1} days", off, f"-{days} days",
    )
    top = q(
        "SELECT product_name AS n, COUNT(*) AS c, SUM(price) AS s FROM orders "
        "WHERE date(created, ?) >= date('now', ?, ?) GROUP BY product_name ORDER BY s DESC LIMIT 5",
        off, off, start,
    )
    return cur, prev, top


def hourly_turnover():
    """Сегодня по часам — только часы, в которых были заказы."""
    off = _off()
    return q(
        "SELECT strftime('%H', created, ?) AS h, COUNT(*) AS c, SUM(price) AS s FROM orders "
        "WHERE date(created, ?) = date('now', ?) GROUP BY h ORDER BY h",
        off, off, off,
    )


def weekly_buckets(n_days=30):
    """[(с даты, по дату, заказов, сумма), ...] — последние n_days дней по 7 дней, свежие первыми."""
    days = daily_turnover(n_days)  # сегодня первым
    out = []
    for i in range(0, len(days), 7):
        chunk = days[i:i + 7]
        out.append((chunk[-1][0], chunk[0][0], sum(x[1] for x in chunk), sum(x[2] for x in chunk)))
    return out


def trend(cur, prev) -> str:
    if prev == 0:
        return ""
    pct = round((cur - prev) * 100 / prev)
    arrow = "📈" if pct > 0 else "📉" if pct < 0 else "➖"
    return f" {arrow} {pct:+d}%"


def screen_stats(period="day"):
    if period not in STATS_PERIODS:
        period = "day"
    title, days, cur_label, prev_label = STATS_PERIODS[period]
    cur, prev, top = period_report(days)
    avg = cur["s"] // cur["c"] if cur["c"] else 0

    if period == "day":
        rows = hourly_turnover()
        head = "По часам (заказов в скобках):"
        lines = [f"{r['h']}:00 — {money(r['s'])} ₽ ({r['c']})" for r in rows]
    elif period == "week":
        head = "По дням (заказов в скобках):"
        lines = [
            f"{WEEKDAYS[d.weekday()]} {d:%d.%m} — {money(total)} ₽ ({c})"
            for d, c, total in daily_turnover(7)
        ]
    else:
        head = "По неделям (заказов в скобках):"
        lines = [
            f"{a:%d.%m}–{b:%d.%m} — {money(total)} ₽ ({c})"
            for a, b, c, total in weekly_buckets(30)
        ]

    all_row = q1("SELECT COUNT(*) AS c, COALESCE(SUM(price), 0) AS s FROM orders")
    top_lines = [
        f"{i}. {esc(r['n'])} — {money(r['s'])} ₽ ({r['c']})" for i, r in enumerate(top, 1)
    ]
    text = (
        f"📊 Оборот · {title}\n\n"
        f"{cur_label}: {money(cur['s'])} ₽{trend(cur['s'], prev['s'])}\n"
        f"Заказов: {cur['c']} · средний чек: {money(avg)} ₽\n"
        f"{prev_label}: {money(prev['s'])} ₽ ({prev['c']})\n\n"
        + (f"Топ товаров:\n" + "\n".join(top_lines) + "\n\n" if top_lines else "")
        + (f"{head}\n" + "\n".join(lines) if lines else "Заказов за этот период пока нет.")
        + f"\n\nВсего за всё время: {money(all_row['s'])} ₽ · заказов: {all_row['c']}"
    )
    tabs = [
        ib(("• " if k == period else "") + v[0], f"a:stats:{k}", style=GREEN if k == period else BLUE)
        for k, v in STATS_PERIODS.items()
    ]
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            tabs,  # День | Неделя | Месяц — в одну строку
            [ib("🔄 Обновить", f"a:stats:{period}")],
            [ib("Назад", "a:menu")],
        ]
    )
    return text, markup, None


def screen_products():
    rows = q(
        "SELECT p.id, p.name, p.price, g.name AS g, d.name AS d FROM products p "
        "JOIN devices d ON d.id = p.device_id JOIN games g ON g.id = d.game_id "
        "ORDER BY g.id, d.id, p.id LIMIT 90"
    )
    text = "📦 Товары\n\nВыберите товар или добавьте новый." if rows else "📦 Товаров пока нет."
    buttons = [ib(f"{r['g']} · {r['d']} · {r['name']} — {r['price']} ₽", f"a:p:{r['id']}") for r in rows]
    buttons += [ib("➕ Добавить товар", "a:add", style=GREEN), ib("Назад", "a:menu")]
    return text, kb(*buttons), None


def screen_product(pid):
    r = q1(
        "SELECT p.*, g.name AS g, d.name AS d FROM products p "
        "JOIN devices d ON d.id = p.device_id JOIN games g ON g.id = d.game_id "
        "WHERE p.id=?",
        pid,
    )
    if not r:
        return None
    left = stock_count(pid)
    sold = scalar("SELECT COUNT(*) FROM stock WHERE product_id=? AND used=1", pid) or 0
    text = (
        f"📦 {esc(r['g'])} · {esc(r['d'])}\n"
        f"{esc(r['name'])}\n\n"
        f"Срок: {days_text(r['days'])}\n"
        f"Цена: {r['price']} ₽\n"
        f"Ключей в наличии: {left}\n"
        f"Выдано: {sold}\n"
        f"Обзор: {esc(r['review_url']) if r['review_url'] else '—'}"
    )
    markup = kb(
        ib("💰 Изменить цену", f"a:edit:{pid}:price"),
        ib("🔑 Добавить ключи", f"a:keys:{pid}", style=GREEN),
        ib("🔗 Ссылка на обзор", f"a:edit:{pid}:review"),
        ib("🖼 Баннер товара", f"a:bnp:{pid}"),
        ib("🗑 Удалить товар", f"a:del:{pid}", style=RED),
        ib("Назад", "a:prods"),
    )
    return text, markup, None


def screen_banners():
    buttons = [
        ib(f"{name} — {'✅ есть' if get_banner(slot) else 'нет'}", f"a:bn:{slot}")
        for slot, name in BANNER_SLOTS.items()
    ]
    buttons.append(ib("📦 Баннеры товаров (по срокам)", "a:bnp", style=BLUE))
    buttons.append(ib("Назад", "a:menu"))
    return "🖼 Баннеры\n\nВыберите раздел, для которого нужен баннер.", kb(*buttons), None


def banner_title(slot):
    if slot.startswith("product:"):
        r = q1("SELECT name FROM products WHERE id=?", int(slot.split(":")[1]))
        return f"Товар «{esc(r['name'])}»" if r else "Товар"
    return BANNER_SLOTS.get(slot, slot)


def screen_product_banners():
    rows = q(
        "SELECT p.id, p.name, g.name AS g, d.name AS d FROM products p "
        "JOIN devices d ON d.id = p.device_id JOIN games g ON g.id = d.game_id "
        "ORDER BY g.id, d.id, p.id LIMIT 90"
    )
    text = (
        "📦 Баннеры товаров\n\nВыберите тариф (например, на 1 день), для которого нужен "
        "свой баннер. Он покажется на карточке товара и когда человек нажмёт «Купить». "
        "Если баннер не задан, будет общий «Выбор устройства»."
    )
    buttons = [
        ib(f"{r['g']} · {r['d']} · {r['name']} — "
           f"{'✅' if get_banner('product:' + str(r['id'])) else 'нет'}", f"a:bnp:{r['id']}")
        for r in rows
    ]
    buttons.append(ib("Назад", "a:bn"))
    return text, kb(*buttons), None


def screen_promos():
    rows = q("SELECT * FROM promos ORDER BY rowid DESC LIMIT 20")
    lines = [
        f"<code>{esc(r['code'])}</code> — {r['amount']} ₽ ({r['used_count']}/{r['max_uses']})"
        for r in rows
    ]
    text = "🎁 Промокоды (начисляют сумму на баланс)\n\n" + (
        "\n".join(lines) if lines else "Пока нет промокодов."
    )
    markup = kb(ib("➕ Создать промокод", "a:prnew", style=GREEN), ib("Назад", "a:menu"))
    return text, markup, None


def screen_reviews():
    total = scalar("SELECT COUNT(*) FROM reviews") or 0
    avg = scalar("SELECT AVG(rating) FROM reviews")
    rows = q(
        "SELECT *, strftime('%d.%m %H:%M', created, ?) AS t FROM reviews ORDER BY id DESC LIMIT 10",
        _off(),
    )
    head = f"⭐ Отзывы\n\nВсего: {total}" + (f" · средняя оценка: {avg:.1f}/5" if avg else "")
    if not rows:
        return head + "\n\nПока нет отзывов.", kb(ib("Назад", "a:menu")), None
    items = [
        f"{'⭐' * r['rating']} · {esc(r['product_name'])}\n"
        f"{esc(r['author'] or '—')} · {r['t']}\n"
        f"{esc(r['text'][:150])}{'…' if len(r['text']) > 150 else ''}"
        for r in rows
    ]
    text = head + "\n\nПоследние 10:\n\n" + "\n\n".join(items)
    return text, kb(ib("🔄 Обновить", "a:rv"), ib("Назад", "a:menu")), None


# ══════════════════════════ АДМИН-ПАНЕЛЬ: НАВИГАЦИЯ ══════════════════════════
@admin_r.callback_query(F.data == "a:menu")
async def a_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, *screen_admin())


@admin_r.callback_query(F.data == "a:cancel")
async def a_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("Отменено")
    await show(cb.message, *screen_admin())


@admin_r.callback_query(F.data == "a:rv")
async def a_reviews(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, *screen_reviews())


@admin_r.callback_query(F.data.regexp(r"^a:stats(:(day|week|month))?$"))
async def a_stats(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = cb.data.split(":")
    period = parts[2] if len(parts) == 3 else "day"
    await cb.answer()
    await show(cb.message, *screen_stats(period))


@admin_r.callback_query(F.data == "a:prods")
async def a_prods(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, *screen_products())


@admin_r.callback_query(F.data.regexp(r"^a:p:\d+$"))
async def a_product(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, *(screen_product(int(cb.data.split(":")[2])) or screen_products()))


# ── удаление товара
@admin_r.callback_query(F.data.regexp(r"^a:del:\d+$"))
async def a_del(cb: CallbackQuery):
    pid = int(cb.data.split(":")[2])
    await cb.answer()
    await show(
        cb.message,
        "Удалить этот товар? Неиспользованные ключи тоже будут удалены.",
        kb(ib("Да, удалить", f"a:delok:{pid}", style=RED), ib("Отмена", f"a:p:{pid}")),
    )


@admin_r.callback_query(F.data.regexp(r"^a:delok:\d+$"))
async def a_del_ok(cb: CallbackQuery):
    pid = int(cb.data.split(":")[2])
    db.execute("DELETE FROM stock WHERE product_id=? AND used=0", (pid,))
    db.execute("DELETE FROM products WHERE id=?", (pid,))
    db.execute("DELETE FROM settings WHERE k=?", (f"banner:product:{pid}",))
    db.commit()
    await cb.answer("Товар удалён")
    await show(cb.message, *screen_products())


# ── добавление товара (6 шагов)
@admin_r.callback_query(F.data == "a:add")
async def a_add(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddProduct.game)
    await cb.answer()
    await show(cb.message, "➕ Новый товар\n\n1/6. Название игры (например: Oxide):", CANCEL_KB)


@admin_r.message(AddProduct.game, F.text)
async def add_game(msg: Message, state: FSMContext):
    await state.update_data(game=msg.text.strip()[:40])
    await state.set_state(AddProduct.device)
    await msg.answer("2/6. Устройство (например: Android Non Root):", reply_markup=CANCEL_KB)


@admin_r.message(AddProduct.device, F.text)
async def add_device(msg: Message, state: FSMContext):
    await state.update_data(device=msg.text.strip()[:40])
    await state.set_state(AddProduct.name)
    await msg.answer("3/6. Название товара/тарифа (например: Cry4me 1D):", reply_markup=CANCEL_KB)


@admin_r.message(AddProduct.name, F.text)
async def add_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text.strip()[:40])
    await state.set_state(AddProduct.days)
    await msg.answer("4/6. Срок в днях (число):", reply_markup=CANCEL_KB)


@admin_r.message(AddProduct.days, F.text)
async def add_days(msg: Message, state: FSMContext):
    t = msg.text.strip()
    if not t.isdigit() or int(t) < 1:
        await msg.answer("Нужно целое число больше 0. Попробуйте ещё раз:", reply_markup=CANCEL_KB)
        return
    await state.update_data(days=int(t))
    await state.set_state(AddProduct.price)
    await msg.answer("5/6. Цена в ₽ (число):", reply_markup=CANCEL_KB)


@admin_r.message(AddProduct.price, F.text)
async def add_price(msg: Message, state: FSMContext):
    t = msg.text.strip()
    if not t.isdigit() or int(t) < 1:
        await msg.answer("Нужно целое число больше 0. Попробуйте ещё раз:", reply_markup=CANCEL_KB)
        return
    await state.update_data(price=int(t))
    await state.set_state(AddProduct.review)
    await msg.answer(
        "6/6. Ссылка на обзор (https://...) или «-», чтобы пропустить:", reply_markup=CANCEL_KB
    )


@admin_r.message(AddProduct.review, F.text)
async def add_review(msg: Message, state: FSMContext):
    t = msg.text.strip()
    if t == "-":
        url = None
    elif t.startswith(("http://", "https://")):
        url = t
    else:
        await msg.answer("Пришлите ссылку (https://...) или «-».", reply_markup=CANCEL_KB)
        return
    d = await state.get_data()
    await state.clear()
    gid = get_or_create_game(d["game"])
    did = get_or_create_device(gid, d["device"])
    pid = add_product(did, d["name"], d["days"], d["price"], url)
    await msg.answer(
        "✅ Товар добавлен. Теперь добавьте ключи для автовыдачи.",
        reply_markup=kb(ib("🔑 Добавить ключи", f"a:keys:{pid}", style=GREEN), ib("К товару", f"a:p:{pid}")),
    )


# ── изменение цены / ссылки на обзор
@admin_r.callback_query(F.data.regexp(r"^a:edit:\d+:(price|review)$"))
async def a_edit(cb: CallbackQuery, state: FSMContext):
    _, _, pid, field = cb.data.split(":")
    await state.clear()
    await state.set_state(EditProduct.value)
    await state.update_data(pid=int(pid), field=field)
    prompt = (
        "Введите новую цену в ₽ (число):"
        if field == "price"
        else "Отправьте ссылку на обзор (https://...) или «-», чтобы убрать:"
    )
    await cb.answer()
    await show(cb.message, prompt, CANCEL_KB)


@admin_r.message(EditProduct.value, F.text)
async def a_edit_value(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid, field, t = data["pid"], data["field"], msg.text.strip()
    if field == "price":
        if not t.isdigit() or int(t) < 1:
            await msg.answer("Нужно целое число больше 0. Попробуйте ещё раз:", reply_markup=CANCEL_KB)
            return
        run("UPDATE products SET price=? WHERE id=?", int(t), pid)
    else:
        if t == "-":
            url = None
        elif t.startswith(("http://", "https://")):
            url = t
        else:
            await msg.answer("Пришлите ссылку (https://...) или «-».", reply_markup=CANCEL_KB)
            return
        run("UPDATE products SET review_url=? WHERE id=?", url, pid)
    await state.clear()
    await send_screen(msg, *(screen_product(pid) or screen_products()))


# ── ключи для автовыдачи
@admin_r.callback_query(F.data.regexp(r"^a:keys:\d+$"))
async def a_keys(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    await state.clear()
    await state.set_state(AddKeys.keys)
    await state.update_data(pid=pid)
    await cb.answer()
    await show(
        cb.message,
        "🔑 Отправьте ключи — каждый с новой строки.\n"
        "Можно текстом или файлом .txt (по одному ключу в строке).",
        CANCEL_KB,
    )


@admin_r.message(AddKeys.keys, F.text | F.document)
async def a_keys_receive(msg: Message, state: FSMContext, bot: Bot):
    pid = (await state.get_data())["pid"]
    if msg.document:
        if (msg.document.file_size or 0) > 2_000_000:
            await msg.answer("Файл слишком большой (максимум 2 МБ).", reply_markup=CANCEL_KB)
            return
        buf = await bot.download(msg.document)
        raw = buf.read().decode("utf-8-sig", errors="ignore")
    else:
        raw = msg.text
    codes = [line.strip() for line in raw.splitlines() if line.strip()]
    if not codes:
        await msg.answer("Не нашёл ни одного ключа. Попробуйте ещё раз:", reply_markup=CANCEL_KB)
        return
    added, dup = add_keys(pid, codes)
    await state.clear()
    await msg.answer(
        f"✅ Добавлено ключей: {added}\nПропущено дублей: {dup}\nВ наличии: {stock_count(pid)}",
        reply_markup=kb(ib("Добавить ещё", f"a:keys:{pid}", style=GREEN), ib("К товару", f"a:p:{pid}")),
    )


# ── баннеры
@admin_r.callback_query(F.data == "a:bn")
async def a_bn(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, *screen_banners())


@admin_r.callback_query(F.data.regexp(r"^a:bn:(main|games|devices|profile)$"))
async def a_bn_slot(cb: CallbackQuery):
    slot = cb.data.split(":")[2]
    has = bool(get_banner(slot))
    buttons = [ib("Загрузить новый", f"a:bnset:{slot}", style=GREEN)]
    if has:
        buttons.append(ib("Убрать баннер", f"a:bnrm:{slot}", style=RED))
    buttons.append(ib("Назад", "a:bn"))
    text = f"🖼 Баннер «{BANNER_SLOTS[slot]}»\n" + ("Сейчас установлен (показан выше)." if has else "Сейчас не установлен.")
    await cb.answer()
    await show(cb.message, text, kb(*buttons), slot if has else None)


@admin_r.callback_query(F.data.regexp(r"^a:bnset:(main|games|devices|profile)$"))
async def a_bn_set(cb: CallbackQuery, state: FSMContext):
    slot = cb.data.split(":")[2]
    await state.set_state(SetBanner.photo)
    await state.update_data(slot=slot)
    await cb.answer()
    await show(
        cb.message,
        f"Отправьте картинку для раздела «{BANNER_SLOTS[slot]}» (как фото, не файлом).",
        CANCEL_KB,
    )


@admin_r.message(SetBanner.photo, F.photo)
async def a_bn_save(msg: Message, state: FSMContext):
    slot = (await state.get_data())["slot"]
    file_id = msg.photo[-1].file_id
    set_setting(f"banner:{slot}", file_id)
    await state.clear()
    await msg.answer_photo(
        file_id,
        caption=f"✅ Баннер: {banner_title(slot)} — установлен.",
        reply_markup=kb(ib("К баннерам", "a:bnp" if slot.startswith("product:") else "a:bn")),
    )


@admin_r.message(SetBanner.photo)
async def a_bn_wrong(msg: Message):
    await msg.answer("Пришлите именно картинку (как фото, не файлом).", reply_markup=CANCEL_KB)


@admin_r.callback_query(F.data.regexp(r"^a:bnrm:(main|games|devices|profile)$"))
async def a_bn_remove(cb: CallbackQuery):
    slot = cb.data.split(":")[2]
    run("DELETE FROM settings WHERE k=?", f"banner:{slot}")
    await cb.answer("Баннер убран")
    await show(cb.message, *screen_banners())


# ── баннеры отдельных товаров (по срокам)
@admin_r.callback_query(F.data == "a:bnp")
async def a_bnp(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, *screen_product_banners())


@admin_r.callback_query(F.data.regexp(r"^a:bnp:\d+$"))
async def a_bnp_view(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    pid = int(cb.data.split(":")[2])
    slot = f"product:{pid}"
    r = q1("SELECT name FROM products WHERE id=?", pid)
    if not r:
        await cb.answer("Товар не найден.", show_alert=True)
        return
    has = bool(get_banner(slot))
    buttons = [ib("Загрузить новый", f"a:bnpset:{pid}", style=GREEN)]
    if has:
        buttons.append(ib("Убрать баннер", f"a:bnprm:{pid}", style=RED))
    buttons.append(ib("Назад", "a:bnp"))
    text = (
        f"🖼 Баннер товара «{esc(r['name'])}»\n"
        + ("Сейчас установлен (показан выше)." if has else "Сейчас не установлен.")
        + "\n\nПоказывается на карточке товара и на экране покупки."
    )
    await cb.answer()
    await show(cb.message, text, kb(*buttons), slot if has else None)


@admin_r.callback_query(F.data.regexp(r"^a:bnpset:\d+$"))
async def a_bnp_set(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    r = q1("SELECT name FROM products WHERE id=?", pid)
    if not r:
        await cb.answer("Товар не найден.", show_alert=True)
        return
    await state.set_state(SetBanner.photo)
    await state.update_data(slot=f"product:{pid}")
    await cb.answer()
    await show(
        cb.message,
        f"Отправьте картинку для товара «{esc(r['name'])}» (как фото, не файлом).",
        CANCEL_KB,
    )


@admin_r.callback_query(F.data.regexp(r"^a:bnprm:\d+$"))
async def a_bnp_remove(cb: CallbackQuery):
    run("DELETE FROM settings WHERE k=?", f"banner:product:{int(cb.data.split(':')[2])}")
    await cb.answer("Баннер убран")
    await show(cb.message, *screen_product_banners())


# ── общее объявление (рассылка)
@admin_r.callback_query(F.data == "a:bc")
async def a_bc(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Broadcast.msg)
    n = scalar("SELECT COUNT(*) FROM users") or 0
    await cb.answer()
    await show(
        cb.message,
        f"📣 Объявление\n\nОтправьте сообщение, которое получат все пользователи ({n}). "
        "Можно текст или фото с подписью — оно уйдёт в точности как вы его напишете.",
        CANCEL_KB,
    )


@admin_r.message(Broadcast.msg)
async def a_bc_preview(msg: Message, state: FSMContext, bot: Bot):
    await state.update_data(chat_id=msg.chat.id, mid=msg.message_id)
    await bot.copy_message(msg.chat.id, msg.chat.id, msg.message_id)
    await msg.answer(
        "☝️ Так увидят пользователи. Отправить всем?",
        reply_markup=kb(ib("Отправить всем", "a:bcok", style=GREEN), ib("Отмена", "a:cancel", style=RED)),
    )


async def safe_copy(bot: Bot, uid: int, chat_id: int, mid: int) -> bool:
    for _ in range(2):
        try:
            await bot.copy_message(uid, chat_id, mid)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception:
            return False
    return False


@admin_r.callback_query(F.data == "a:bcok")
async def a_bc_send(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    if not data.get("mid"):
        await cb.answer("Нет сообщения для рассылки.", show_alert=True)
        return
    await cb.answer("Рассылка запущена")
    await cb.message.edit_text("⏳ Идёт рассылка…")
    ok = fail = 0
    for (uid,) in [tuple(r) for r in q("SELECT id FROM users")]:
        if await safe_copy(bot, uid, data["chat_id"], data["mid"]):
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.05)
    await cb.message.edit_text(
        f"✅ Рассылка завершена\nДоставлено: {ok}\nНе доставлено: {fail}",
        reply_markup=kb(ib("В админ-панель", "a:menu")),
    )


# ── промокоды
@admin_r.callback_query(F.data == "a:pr")
async def a_pr(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show(cb.message, *screen_promos())


@admin_r.callback_query(F.data == "a:prnew")
async def a_pr_new(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(NewPromo.code)
    await cb.answer()
    await show(cb.message, "🎁 Новый промокод\n\n1/3. Введите код (латиница/цифры, например: START100):", CANCEL_KB)


@admin_r.message(NewPromo.code, F.text)
async def pr_code(msg: Message, state: FSMContext):
    code = msg.text.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{2,32}", code):
        await msg.answer("Только латиница, цифры, _ и -, от 2 до 32 символов.", reply_markup=CANCEL_KB)
        return
    if q1("SELECT 1 FROM promos WHERE code=?", code):
        await msg.answer("Такой промокод уже есть. Введите другой:", reply_markup=CANCEL_KB)
        return
    await state.update_data(code=code)
    await state.set_state(NewPromo.amount)
    await msg.answer("2/3. Сколько ₽ начислять на баланс (число):", reply_markup=CANCEL_KB)


@admin_r.message(NewPromo.amount, F.text)
async def pr_amount(msg: Message, state: FSMContext):
    t = msg.text.strip()
    if not t.isdigit() or int(t) < 1:
        await msg.answer("Нужно целое число больше 0:", reply_markup=CANCEL_KB)
        return
    await state.update_data(amount=int(t))
    await state.set_state(NewPromo.uses)
    await msg.answer("3/3. Сколько раз можно активировать (число):", reply_markup=CANCEL_KB)


@admin_r.message(NewPromo.uses, F.text)
async def pr_uses(msg: Message, state: FSMContext):
    t = msg.text.strip()
    if not t.isdigit() or int(t) < 1:
        await msg.answer("Нужно целое число больше 0:", reply_markup=CANCEL_KB)
        return
    d = await state.get_data()
    await state.clear()
    run("INSERT INTO promos(code, amount, max_uses) VALUES(?, ?, ?)", d["code"], d["amount"], int(t))
    await send_screen(msg, *screen_promos())


# ══════════════════════════ ЗАПУСК ══════════════════════════
async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_routers(common, admin_r, user_r)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
