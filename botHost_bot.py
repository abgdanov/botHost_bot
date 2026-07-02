import telebot
from telebot import types
from datetime import datetime, timedelta
import random
import logging
import threading
import time
import sqlite3
import json
import os
import sys
from contextlib import contextmanager

# Включаем логирование
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# --- ЗАГРУЗКА ТОКЕНА (из переменных окружения Bothost) ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    print("❌ ОШИБКА: Токен не найден! Установите переменную TELEGRAM_BOT_TOKEN")
    sys.exit(1)

bot = telebot.TeleBot(TOKEN)

# --- ЛОКАЛЬНОЕ ХРАНИЛИЩЕ ---
USER_FAMILIES = {}
FAMILY_MEMBERS = {}
USER_STATES = {}
TRANSACTIONS = []
REMINDERS = {}
BUDGET_LIMITS = {}

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
DB_NAME = os.path.join(os.path.dirname(__file__), "family_budget.db")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS families (family_id INTEGER PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, family_id INTEGER, first_name TEXT, joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (family_id) REFERENCES families (family_id))"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, family_id INTEGER, user_id INTEGER, user_name TEXT, type TEXT, category_id INTEGER, amount REAL, date TIMESTAMP, FOREIGN KEY (family_id) REFERENCES families (family_id), FOREIGN KEY (user_id) REFERENCES users (user_id), FOREIGN KEY (category_id) REFERENCES categories(id))"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY AUTOINCREMENT, family_id INTEGER, name TEXT NOT NULL, type TEXT CHECK(type IN ('expense', 'income')), parent_id INTEGER DEFAULT NULL, is_standard INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (family_id) REFERENCES families (family_id), FOREIGN KEY (parent_id) REFERENCES categories(id), UNIQUE(family_id, name, type, parent_id))"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, family_id INTEGER, user_id INTEGER, title TEXT, amount REAL, category TEXT, type TEXT, frequency TEXT, day INTEGER, next_due_date TIMESTAMP, notify_days_before TEXT, FOREIGN KEY (family_id) REFERENCES families (family_id), FOREIGN KEY (user_id) REFERENCES users (user_id))"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS budget_limits (family_id INTEGER, category_id INTEGER, limit_amount REAL, PRIMARY KEY (family_id, category_id), FOREIGN KEY (category_id) REFERENCES categories(id))"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS user_states (user_id INTEGER PRIMARY KEY, state_data TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_family ON transactions(family_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_categories_family ON categories(family_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_family ON reminders(family_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(user_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_next_due ON reminders(next_due_date)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_budget_limits_family ON budget_limits(family_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_budget_limits_category ON budget_limits(category_id)"
        )

        # Создаём стандартные категории
        standard_categories = {
            "expense": [
                "🛒 Продукты",
                "🚗 Авто / Транспорт",
                "💊 Здоровье",
                "🎉 Отдых",
                "💳 Платежи",
            ],
            "income": ["💰 Зарплата"],
        }

        for cat_type, categories in standard_categories.items():
            for cat_name in categories:
                cursor.execute(
                    "INSERT OR IGNORE INTO categories (family_id, name, type, is_standard) VALUES (NULL, ?, ?, 1)",
                    (cat_name, cat_type),
                )

        print("✅ База данных инициализирована")


# --- ФУНКЦИИ ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ ---
def get_user_family_db(user_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT family_id FROM users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        return result["family_id"] if result else None


def add_user_to_family_db(user_id, family_id, first_name):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO users (user_id, family_id, first_name) VALUES (?, ?, ?)",
            (user_id, family_id, first_name),
        )


def create_family_db(family_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO families (family_id) VALUES (?)", (family_id,)
        )


def add_transaction_db(
    family_id, user_id, user_name, trans_type, category_id, amount, date_obj
):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO transactions (family_id, user_id, user_name, type, category_id, amount, date) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (family_id, user_id, user_name, trans_type, category_id, amount, date_obj),
        )


def get_transactions_db(
    family_id, user_id=None, start_date=None, end_date=None, limit=None
):
    with get_db() as conn:
        cursor = conn.cursor()
        query = """
            SELECT t.*, c.name as category_name, c.parent_id, c.is_standard,
                   p.name as parent_category_name
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            LEFT JOIN categories p ON c.parent_id = p.id
            WHERE t.family_id = ?
        """
        params = [family_id]
        if user_id:
            query += " AND t.user_id = ?"
            params.append(user_id)
        if start_date:
            query += " AND t.date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND t.date <= ?"
            params.append(end_date)
        query += " ORDER BY t.date DESC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def get_last_user_transaction_db(family_id, user_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT t.*, c.name as category_name, c.parent_id, c.is_standard,
                   p.name as parent_category_name
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            LEFT JOIN categories p ON c.parent_id = p.id
            WHERE t.family_id = ? AND t.user_id = ?
            ORDER BY t.date DESC LIMIT 1
        """,
            (family_id, user_id),
        )
        result = cursor.fetchone()
        return dict(result) if result else None


def delete_transaction_db(transaction_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))


def get_category_name_by_id(category_id, family_id=None):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM categories WHERE id = ?", (category_id,))
        result = cursor.fetchone()
        return dict(result) if result else None


def get_category_full_name(category_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT c.*, p.name as parent_name
            FROM categories c
            LEFT JOIN categories p ON c.parent_id = p.id
            WHERE c.id = ?
        """,
            (category_id,),
        )
        result = cursor.fetchone()
        if not result:
            return "Удалённая категория"
        if result["parent_name"]:
            return f"{result['parent_name']} → {result['name']}"
        return result["name"]


def get_category_emoji(category_name):
    emoji_map = {
        "Продукты": "🛒",
        "Авто": "🚗",
        "Транспорт": "🚗",
        "Здоровье": "💊",
        "Отдых": "🎉",
        "Платежи": "💳",
        "Зарплата": "💰",
        "Подработка": "💼",
        "Подарки": "🎁",
        "Другое": "📦",
    }
    for key, emoji in emoji_map.items():
        if key in category_name:
            return emoji
    return "📌"


def get_budget_limits_db(family_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT bl.category_id, bl.limit_amount, c.name as category_name
            FROM budget_limits bl
            JOIN categories c ON bl.category_id = c.id
            WHERE bl.family_id = ?
        """,
            (family_id,),
        )
        return {
            row["category_id"]: {
                "limit": row["limit_amount"],
                "name": row["category_name"],
            }
            for row in cursor.fetchall()
        }


def set_budget_limit_db(family_id, category_id, limit):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO budget_limits (family_id, category_id, limit_amount) VALUES (?, ?, ?)",
            (family_id, category_id, limit),
        )


def delete_budget_limit_db(family_id, category_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM budget_limits WHERE family_id = ? AND category_id = ?",
            (family_id, category_id),
        )


def delete_budget_limits_by_category_db(category_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM budget_limits WHERE category_id = ?", (category_id,)
        )


def get_category_id_by_name(name, family_id=None):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM categories WHERE name = ? AND (family_id = ? OR (family_id IS NULL AND is_standard = 1))",
            (name, family_id),
        )
        result = cursor.fetchone()
        return result["id"] if result else None


def get_or_create_standard_category(name, cat_type):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM categories WHERE name = ? AND is_standard = 1",
            (name,),
        )
        result = cursor.fetchone()
        if result:
            return result["id"]
        cursor.execute(
            "INSERT INTO categories (family_id, name, type, is_standard) VALUES (NULL, ?, ?, 1)",
            (name, cat_type),
        )
        return cursor.lastrowid


def save_user_state_db(user_id, state_data):
    global USER_STATES
    USER_STATES[user_id] = state_data
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO user_states (user_id, state_data) VALUES (?, ?)",
            (user_id, json.dumps(state_data, default=str)),
        )


def get_user_state_db(user_id):
    global USER_STATES
    if user_id in USER_STATES:
        return USER_STATES[user_id]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT state_data FROM user_states WHERE user_id = ?", (user_id,)
        )
        result = cursor.fetchone()
        state = json.loads(result["state_data"]) if result else {}
        USER_STATES[user_id] = state
        return state


def delete_user_state_db(user_id):
    global USER_STATES
    if user_id in USER_STATES:
        del USER_STATES[user_id]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))


def add_reminder_db(family_id, user_id, reminder):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO reminders (family_id, user_id, title, amount, category, type, frequency, day, next_due_date, notify_days_before) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                family_id,
                user_id,
                reminder["title"],
                reminder["amount"],
                reminder["category"],
                reminder["type"],
                reminder["frequency"],
                reminder["day"],
                reminder["next_due_date"],
                json.dumps(reminder["notify_days_before"]),
            ),
        )
        reminder["id"] = cursor.lastrowid
        return reminder


def get_reminders_db(family_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM reminders WHERE family_id = ?", (family_id,))
        reminders = []
        for row in cursor.fetchall():
            r = dict(row)
            r["notify_days_before"] = json.loads(r["notify_days_before"])
            reminders.append(r)
        return reminders


def delete_reminder_db(reminder_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))


def calculate_next_date(frequency, day):
    now = datetime.now()
    if frequency == "monthly":
        year, month = now.year, now.month
        if day < now.day:
            month += 1
            if month > 12:
                month, year = 1, year + 1
        if month == 12:
            last_day = 31
        else:
            last_day = (datetime(year, month + 1, 1) - timedelta(days=1)).day
        actual_day = min(day, last_day)
        return datetime(year, month, actual_day, 12, 0, 0)
    else:
        days_ahead = day - now.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        return now + timedelta(days=days_ahead)


def load_data_to_memory():
    global USER_FAMILIES, FAMILY_MEMBERS, TRANSACTIONS, REMINDERS, BUDGET_LIMITS, USER_STATES
    (
        USER_FAMILIES,
        FAMILY_MEMBERS,
        TRANSACTIONS,
        REMINDERS,
        BUDGET_LIMITS,
        USER_STATES,
    ) = ({}, {}, [], {}, {}, {})
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, family_id, first_name FROM users")
        for row in cursor.fetchall():
            USER_FAMILIES[row["user_id"]] = row["family_id"]
            if row["family_id"] not in FAMILY_MEMBERS:
                FAMILY_MEMBERS[row["family_id"]] = []
            FAMILY_MEMBERS[row["family_id"]].append(row["user_id"])
        cursor.execute("SELECT * FROM transactions ORDER BY date")
        for row in cursor.fetchall():
            TRANSACTIONS.append(dict(row))
        cursor.execute("SELECT * FROM reminders")
        for row in cursor.fetchall():
            family_id = row["family_id"]
            if family_id not in REMINDERS:
                REMINDERS[family_id] = []
            r = dict(row)
            r["notify_days_before"] = json.loads(r["notify_days_before"])
            REMINDERS[family_id].append(r)
        cursor.execute("SELECT * FROM budget_limits")
        for row in cursor.fetchall():
            family_id = row["family_id"]
            if family_id not in BUDGET_LIMITS:
                BUDGET_LIMITS[family_id] = {}
            BUDGET_LIMITS[family_id][row["category_id"]] = {
                "limit": row["limit_amount"],
                "name": "",
            }
        cursor.execute("SELECT user_id, state_data FROM user_states")
        for row in cursor.fetchall():
            USER_STATES[row["user_id"]] = json.loads(row["state_data"])
    print(
        f"📦 Данные загружены: {len(USER_FAMILIES)} пользователей, {len(TRANSACTIONS)} транзакций"
    )


# --- ФУНКЦИИ ДЛЯ РАБОТЫ С КАТЕГОРИЯМИ ---
def add_category_db(family_id, name, category_type, parent_id=None):
    with get_db() as conn:
        cursor = conn.cursor()
        if parent_id is None:
            cursor.execute(
                "SELECT 1 FROM categories WHERE family_id = ? AND name = ? AND type = ? AND parent_id IS NULL",
                (family_id, name, category_type),
            )
        else:
            cursor.execute(
                "SELECT 1 FROM categories WHERE family_id = ? AND name = ? AND type = ? AND parent_id = ?",
                (family_id, name, category_type, parent_id),
            )
        if cursor.fetchone():
            return False
        cursor.execute(
            "INSERT INTO categories (family_id, name, type, parent_id) VALUES (?, ?, ?, ?)",
            (family_id, name, category_type, parent_id),
        )
        return True


def get_categories_db(family_id, category_type=None, parent_id=None):
    with get_db() as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM categories WHERE (family_id = ? OR is_standard = 1)"
        params = [family_id]
        if category_type:
            query += " AND type = ?"
            params.append(category_type)
        if parent_id is not None:
            query += " AND parent_id = ?"
            params.append(parent_id)
        else:
            query += " AND parent_id IS NULL"
        query += " ORDER BY name"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def get_category_by_id(category_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM categories WHERE id = ?", (category_id,))
        result = cursor.fetchone()
        return dict(result) if result else None


def delete_category_db(category_id, family_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count FROM transactions WHERE category_id = ?",
            (category_id,),
        )
        result = cursor.fetchone()
        has_transactions = result["count"] > 0

        cursor.execute(
            "SELECT COUNT(*) as count FROM categories WHERE parent_id = ?",
            (category_id,),
        )
        result = cursor.fetchone()
        has_subcategories = result["count"] > 0

        delete_budget_limits_by_category_db(category_id)

        cursor.execute(
            "DELETE FROM categories WHERE parent_id = ? AND family_id = ?",
            (category_id, family_id),
        )

        cursor.execute(
            "DELETE FROM categories WHERE id = ? AND family_id = ?",
            (category_id, family_id),
        )

        return has_transactions, has_subcategories


# --- КНОПКИ МЕНЮ ---
def get_main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn1 = types.KeyboardButton("📉 Добавить Расход")
    btn2 = types.KeyboardButton("📈 Добавить Доход")
    btn3 = types.KeyboardButton("📊 Отчеты")
    btn4 = types.KeyboardButton("📊 История и Баланс")
    btn5 = types.KeyboardButton("💰 Бюджетные лимиты")
    btn6 = types.KeyboardButton("⏰ Напоминания о платежах")
    btn7 = types.KeyboardButton("🏷️ Категории")
    btn8 = types.KeyboardButton("↩️ Отменить операцию")
    markup.add(btn1, btn2)
    markup.add(btn3, btn4)
    markup.add(btn5, btn6)
    markup.add(btn7, btn8)
    return markup


def get_auth_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    btn1 = types.KeyboardButton("🏠 Создать новую семью")
    btn2 = types.KeyboardButton("🔑 Войти по ID семьи")
    markup.add(btn1, btn2)
    return markup


def get_reminders_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    btn1 = types.KeyboardButton("➕ Добавить напоминание")
    btn2 = types.KeyboardButton("📋 Список напоминаний")
    btn3 = types.KeyboardButton("🗑 Удалить напоминание")
    btn4 = types.KeyboardButton("🔙 Назад в главное меню")
    markup.add(btn1, btn2, btn3, btn4)
    return markup


def get_report_type_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    btn1 = types.KeyboardButton("🏠 Семейный отчет")
    btn2 = types.KeyboardButton("👤 Личный отчет")
    btn3 = types.KeyboardButton("🔙 Назад в главное меню")
    markup.add(btn1, btn2, btn3)
    return markup


def get_period_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    btn1 = types.KeyboardButton("📅 Текущий месяц")
    btn2 = types.KeyboardButton("🗓️ Произвольный период")
    btn3 = types.KeyboardButton("🔙 Назад")
    markup.add(btn1, btn2, btn3)
    return markup


def get_limits_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    btn1 = types.KeyboardButton("📝 Установить лимит")
    btn2 = types.KeyboardButton("📊 Просмотреть лимиты")
    btn3 = types.KeyboardButton("🗑 Удалить лимит")
    btn4 = types.KeyboardButton("🔙 Назад в главное меню")
    markup.add(btn1, btn2, btn3, btn4)
    return markup


def get_categories_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    btn1 = types.KeyboardButton("➕ Добавить категорию")
    btn2 = types.KeyboardButton("📂 Добавить подкатегорию")
    btn3 = types.KeyboardButton("🗑️ Удалить категорию")
    btn4 = types.KeyboardButton("📋 Список категорий")
    btn5 = types.KeyboardButton("🔙 Назад в главное меню")
    markup.add(btn1, btn2, btn3, btn4, btn5)
    return markup


def get_cancel_confirmation_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(types.KeyboardButton("❌ Удалить"), types.KeyboardButton("🔙 Отмена"))
    return markup


# --- ОСНОВНЫЕ ОБРАБОТЧИКИ СООБЩЕНИЙ ---
@bot.message_handler(commands=["start"])
def start_message(message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name
    family_id = get_user_family_db(user_id)
    if family_id:
        bot.send_message(
            message.chat.id,
            f"Привет, {first_name}! 👋\nВы в семейной группе №`{family_id}`.",
            parse_mode="Markdown",
            reply_markup=get_main_menu(),
        )
    else:
        bot.send_message(
            message.chat.id,
            f"Привет, {first_name}! 👋\nДля ведения совместного бюджета нужно создать семейную группу или подключиться к существующей:",
            reply_markup=get_auth_menu(),
        )


@bot.message_handler(func=lambda message: message.text == "🏠 Создать новую семью")
def create_family(message):
    user_id = message.from_user.id
    while True:
        new_family_id = random.randint(10000, 99999)
        if not get_user_family_db(user_id):
            break
    create_family_db(new_family_id)
    add_user_to_family_db(user_id, new_family_id, message.from_user.first_name)
    load_data_to_memory()
    bot.send_message(
        message.chat.id,
        f"🎉 Семейная группа создана! ID: `{new_family_id}`\n\n⚠️ **Важно:** Поделитесь этим ID с членами семьи.",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )


@bot.message_handler(func=lambda message: message.text == "🔑 Войти по ID семьи")
def ask_family_id(message):
    msg = bot.send_message(
        message.chat.id,
        "Введите 5-значный ID семьи:",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, save_family_id)


def save_family_id(message):
    user_id = message.from_user.id
    input_id = message.text.strip()
    if not input_id.isdigit() or len(input_id) != 5:
        msg = bot.send_message(
            message.chat.id, "❌ Ошибка! Нужно 5 цифр. Попробуйте еще раз:"
        )
        bot.register_next_step_handler(msg, save_family_id)
        return
    family_id = int(input_id)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM families WHERE family_id = ?", (family_id,))
        if not cursor.fetchone():
            bot.send_message(
                message.chat.id,
                "❌ Семья с таким ID не найдена!",
                reply_markup=get_auth_menu(),
            )
            return
    add_user_to_family_db(user_id, family_id, message.from_user.first_name)
    load_data_to_memory()
    bot.send_message(
        message.chat.id,
        f"🤝 Добро пожаловать в семью №`{family_id}`!\nТеперь вы можете видеть общие доходы и расходы.",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )


# --- УПРАВЛЕНИЕ КАТЕГОРИЯМИ ---
@bot.message_handler(func=lambda message: message.text == "🏷️ Категории")
def categories_handler(message):
    user_id = message.from_user.id
    if not get_user_family_db(user_id):
        bot.send_message(
            message.chat.id, "Сначала войдите в семью!", reply_markup=get_auth_menu()
        )
        return
    bot.send_message(
        message.chat.id,
        "🏷️ **Управление категориями**\n\n"
        "Здесь вы можете добавлять и удалять свои категории расходов и доходов.\n"
        "Они будут видны всем участникам вашей семьи.\n\n"
        "➕ Добавить категорию — создать новую родительскую категорию.\n"
        "📂 Добавить подкатегорию — добавить подкатегорию к существующей категории.\n"
        "🗑️ Удалить категорию — удалить категорию или подкатегорию.\n"
        "📋 Список категорий — посмотреть все категории семьи.",
        parse_mode="Markdown",
        reply_markup=get_categories_menu(),
    )


@bot.message_handler(func=lambda message: message.text == "➕ Добавить категорию")
def add_category_start(message):
    user_id = message.from_user.id
    save_user_state_db(user_id, {"action": "add_category"})
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(types.KeyboardButton("💰 Расход"), types.KeyboardButton("📈 Доход"))
    markup.add(types.KeyboardButton("❌ Отмена"))
    bot.send_message(
        message.chat.id,
        "Выберите тип категории:",
        reply_markup=markup,
    )


@bot.message_handler(
    func=lambda message: message.text in ["💰 Расход", "📈 Доход"]
    and get_user_state_db(message.from_user.id).get("action") == "add_category"
)
def add_category_type(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    category_type = "expense" if message.text == "💰 Расход" else "income"
    state["category_type"] = category_type
    save_user_state_db(user_id, state)
    msg = bot.send_message(
        message.chat.id,
        "Введите название категории:\n\n"
        "Например: «🚕 Такси», «💻 Подписки»\n"
        "❌ Отмена",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, add_category_name)


def add_category_name(message):
    user_id = message.from_user.id
    if message.text == "❌ Отмена":
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
        )
        return
    state = get_user_state_db(user_id)
    if not state or state.get("action") != "add_category":
        bot.send_message(
            message.chat.id, "Ошибка. Начните заново.", reply_markup=get_main_menu()
        )
        return
    name = message.text.strip()
    if not name:
        msg = bot.send_message(
            message.chat.id, "❌ Название не может быть пустым. Введите название:"
        )
        bot.register_next_step_handler(msg, add_category_name)
        return
    family_id = get_user_family_db(user_id)
    category_type = state["category_type"]
    if add_category_db(family_id, name, category_type):
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id,
            f"✅ Категория «{name}» добавлена!",
            reply_markup=get_main_menu(),
        )
    else:
        msg = bot.send_message(
            message.chat.id,
            f"❌ Категория «{name}» уже существует. Введите другое название:",
        )
        bot.register_next_step_handler(msg, add_category_name)


@bot.message_handler(func=lambda message: message.text == "📂 Добавить подкатегорию")
def add_subcategory_start(message):
    user_id = message.from_user.id
    family_id = get_user_family_db(user_id)
    if not family_id:
        return

    parent_categories = get_categories_db(family_id)

    if not parent_categories:
        bot.send_message(
            message.chat.id,
            "Нет доступных родительских категорий. Сначала создайте категорию.",
            reply_markup=get_categories_menu(),
        )
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for cat in parent_categories[:20]:
        markup.add(types.KeyboardButton(cat["name"]))
    markup.add(types.KeyboardButton("❌ Отмена"))

    save_user_state_db(
        user_id,
        {
            "action": "add_subcategory",
            "parent_categories": parent_categories,
        },
    )
    bot.send_message(
        message.chat.id,
        "Выберите родительскую категорию для подкатегории:",
        reply_markup=markup,
    )


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "add_subcategory"
    and message.text != "❌ Отмена"
)
def add_subcategory_parent(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    parent_name = message.text.strip()

    parent_categories = state.get("parent_categories", [])
    parent_cat = next((c for c in parent_categories if c["name"] == parent_name), None)
    if not parent_cat:
        bot.send_message(
            message.chat.id,
            "❌ Категория не найдена. Попробуйте еще раз.",
            reply_markup=get_categories_menu(),
        )
        delete_user_state_db(user_id)
        return

    state["parent_id"] = parent_cat["id"]
    state["parent_name"] = parent_name
    state["category_type"] = parent_cat["type"]
    save_user_state_db(user_id, state)

    msg = bot.send_message(
        message.chat.id,
        f"Введите название подкатегории для «{parent_name}»:",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, add_subcategory_name)


def add_subcategory_name(message):
    user_id = message.from_user.id
    if message.text == "❌ Отмена":
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
        )
        return

    state = get_user_state_db(user_id)
    if not state or state.get("action") != "add_subcategory":
        bot.send_message(
            message.chat.id, "Ошибка. Начните заново.", reply_markup=get_main_menu()
        )
        return

    name = message.text.strip()
    if not name:
        msg = bot.send_message(
            message.chat.id, "❌ Название не может быть пустым. Введите название:"
        )
        bot.register_next_step_handler(msg, add_subcategory_name)
        return

    family_id = get_user_family_db(user_id)
    parent_id = state["parent_id"]
    category_type = state.get("category_type")

    if add_category_db(family_id, name, category_type, parent_id):
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id,
            f"✅ Подкатегория «{name}» добавлена к «{state['parent_name']}»!",
            reply_markup=get_main_menu(),
        )
    else:
        msg = bot.send_message(
            message.chat.id,
            f"❌ Подкатегория «{name}» уже существует. Введите другое название:",
        )
        bot.register_next_step_handler(msg, add_subcategory_name)


@bot.message_handler(func=lambda message: message.text == "🗑️ Удалить категорию")
def delete_category_start(message):
    user_id = message.from_user.id
    family_id = get_user_family_db(user_id)
    if not family_id:
        return

    user_categories = get_categories_db(family_id)

    if not user_categories:
        bot.send_message(
            message.chat.id,
            "Нет пользовательских категорий для удаления.",
            reply_markup=get_categories_menu(),
        )
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for cat in user_categories[:20]:
        label = cat["name"]
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) as count FROM categories WHERE parent_id = ?",
                (cat["id"],),
            )
            result = cursor.fetchone()
            if result["count"] > 0:
                label += " 📂"
        markup.add(types.KeyboardButton(label))
    markup.add(types.KeyboardButton("❌ Отмена"))

    save_user_state_db(
        user_id,
        {
            "action": "delete_category",
            "user_categories": user_categories,
        },
    )
    bot.send_message(
        message.chat.id,
        "Выберите категорию для удаления:\n\n"
        "⚠️ При удалении родительской категории все её подкатегории тоже будут удалены.\n"
        "Старые транзакции сохранятся.",
        reply_markup=markup,
    )


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "delete_category"
    and message.text != "❌ Отмена"
)
def delete_category_confirm(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    selected_name = message.text.replace(" 📂", "").strip()

    user_categories = state.get("user_categories", [])
    category = next((c for c in user_categories if c["name"] == selected_name), None)
    if not category:
        bot.send_message(
            message.chat.id,
            "❌ Категория не найдена. Попробуйте еще раз.",
            reply_markup=get_categories_menu(),
        )
        delete_user_state_db(user_id)
        return

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count FROM transactions WHERE category_id = ?",
            (category["id"],),
        )
        result = cursor.fetchone()
        has_transactions = result["count"] > 0

    family_id = get_user_family_db(user_id)
    has_subcategories = False

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count FROM categories WHERE parent_id = ?",
            (category["id"],),
        )
        result = cursor.fetchone()
        has_subcategories = result["count"] > 0

    warning = ""
    if has_transactions:
        warning += "\n⚠️ Есть транзакции с этой категорией. Они сохранятся, но категория исчезнет."
    if has_subcategories:
        warning += "\n⚠️ Есть подкатегории. Они тоже будут удалены."

    if warning:
        bot.send_message(
            message.chat.id,
            f"⚠️ **Внимание!** Категория «{selected_name}» будет удалена.{warning}\n\n"
            f"Удалить? (да / нет)",
            parse_mode="Markdown",
        )
        save_user_state_db(
            user_id,
            {
                "action": "confirm_delete",
                "category_id": category["id"],
                "category_name": selected_name,
            },
        )
    else:
        delete_category_db(category["id"], family_id)
        load_data_to_memory()
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id,
            f"✅ Категория «{selected_name}» удалена.",
            reply_markup=get_main_menu(),
        )


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "confirm_delete"
    and message.text in ["да", "нет"]
)
def confirm_delete_category(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)

    if message.text == "нет":
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id,
            "Удаление отменено.",
            reply_markup=get_main_menu(),
        )
        return

    category_id = state["category_id"]
    family_id = get_user_family_db(user_id)
    category_name = state["category_name"]

    delete_category_db(category_id, family_id)
    load_data_to_memory()
    delete_user_state_db(user_id)
    bot.send_message(
        message.chat.id,
        f"✅ Категория «{category_name}» удалена.",
        reply_markup=get_main_menu(),
    )


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "confirm_delete"
    and message.text not in ["да", "нет"]
)
def confirm_delete_category_invalid(message):
    bot.send_message(
        message.chat.id,
        "Пожалуйста, ответьте «да» или «нет».",
    )


@bot.message_handler(func=lambda message: message.text == "📋 Список категорий")
def list_categories(message):
    user_id = message.from_user.id
    family_id = get_user_family_db(user_id)
    if not family_id:
        return

    response = "📋 **Категории вашей семьи**\n\n"

    response += "💰 **Расходы:**\n"
    expense_cats = get_categories_db(family_id, "expense")
    for cat in expense_cats:
        if cat["is_standard"]:
            response += f"• {cat['name']} (стандартная)\n"
        else:
            response += f"• {cat['name']} (ваша)\n"
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM categories WHERE parent_id = ? AND (family_id = ? OR is_standard = 1)",
                (cat["id"], family_id),
            )
            subcats = [dict(row) for row in cursor.fetchall()]
            for sub in subcats:
                response += f"  ↳ {sub['name']}\n"

    response += "\n📈 **Доходы:**\n"
    income_cats = get_categories_db(family_id, "income")
    for cat in income_cats:
        if cat["is_standard"]:
            response += f"• {cat['name']} (стандартная)\n"
        else:
            response += f"• {cat['name']} (ваша)\n"
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM categories WHERE parent_id = ? AND (family_id = ? OR is_standard = 1)",
                (cat["id"], family_id),
            )
            subcats = [dict(row) for row in cursor.fetchall()]
            for sub in subcats:
                response += f"  ↳ {sub['name']}\n"

    bot.send_message(message.chat.id, response, parse_mode="Markdown")


# --- УЧЕТ РАСХОДОВ И ДОХОДОВ ---
@bot.message_handler(
    func=lambda message: message.text in ["📉 Добавить Расход", "📈 Добавить Доход"]
)
def handle_menu(message):
    user_id = message.from_user.id
    family_id = get_user_family_db(user_id)

    if not family_id:
        bot.send_message(
            message.chat.id, "Сначала войдите в семью!", reply_markup=get_auth_menu()
        )
        return

    trans_type = "expense" if message.text == "📉 Добавить Расход" else "income"
    save_user_state_db(user_id, {"action": "add_transaction", "type": trans_type})

    categories = get_categories_db(family_id, trans_type)

    if not categories:
        bot.send_message(
            message.chat.id,
            "Нет доступных категорий. Создайте через меню «🏷️ Категории».",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for cat in categories[:20]:
        markup.add(types.KeyboardButton(cat["name"]))
    markup.add(types.KeyboardButton("❌ Отмена"))

    save_user_state_db(
        user_id,
        {
            "action": "add_transaction",
            "type": trans_type,
            "categories": categories,
        },
    )

    action_text = "расхода" if trans_type == "expense" else "дохода"
    bot.send_message(
        message.chat.id,
        f"Выберите категорию для {action_text}:",
        reply_markup=markup,
    )


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "add_transaction"
    and message.text != "❌ Отмена"
)
def handle_category_selection(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    selected_label = message.text.strip()

    family_id = get_user_family_db(user_id)

    logging.info(f"🔍 Выбрана категория: {selected_label}")

    # ПРОВЕРКА: Если в состоянии уже есть selected_category_id,
    # значит это выбор подкатегории
    if "selected_category_id" in state:
        logging.info("📌 Это выбор ПОДКАТЕГОРИИ, перенаправляем...")
        handle_subcategory_selection(message)
        return

    categories = state.get("categories", [])
    category = None

    # Ищем категорию по точному совпадению
    for c in categories:
        if c["name"] == selected_label:
            category = c
            break

    # Если не нашли, пробуем без эмодзи
    if not category:
        clean_label = "".join(
            ch for ch in selected_label if ch.isalnum() or ch.isspace()
        ).strip()
        for c in categories:
            clean_cat = "".join(
                ch for ch in c["name"] if ch.isalnum() or ch.isspace()
            ).strip()
            if clean_cat == clean_label:
                category = c
                break

    if not category:
        logging.error(f"❌ Категория не найдена: {selected_label}")
        bot.send_message(
            message.chat.id,
            "❌ Категория не найдена. Попробуйте еще раз.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    logging.info(f"✅ Найдена категория: {category['name']} (id={category['id']})")

    # Получаем подкатегории для этой категории
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT id, name, type, family_id, parent_id, is_standard 
               FROM categories 
               WHERE parent_id = ? 
               AND family_id = ?""",
            (category["id"], family_id),
        )
        subcategories = [dict(row) for row in cursor.fetchall()]

    logging.info(f"📂 Найдено подкатегорий: {len(subcategories)}")
    for sub in subcategories:
        logging.info(
            f"  - {sub['name']} (id={sub['id']}, parent_id={sub['parent_id']})"
        )

    if subcategories:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add(types.KeyboardButton("➡️ Без подкатегории"))
        for sub in subcategories:
            markup.add(types.KeyboardButton(sub["name"]))
        markup.add(types.KeyboardButton("❌ Отмена"))

        # СОХРАНЯЕМ ВСЁ в состоянии
        state["selected_category_id"] = category["id"]
        state["selected_category_name"] = category["name"]
        state["subcategories"] = subcategories  # Полные объекты
        state["action"] = "add_transaction"
        save_user_state_db(user_id, state)

        logging.info(
            f"💾 Сохранено состояние: parent_id={category['id']}, подкатегорий={len(subcategories)}"
        )

        bot.send_message(
            message.chat.id,
            f"Вы выбрали «{category['name']}».\n\nВыберите подкатегорию или «Без подкатегории»:",
            reply_markup=markup,
        )
    else:
        # Нет подкатегорий - сразу просим сумму
        state["category_id"] = category["id"]
        state["category_name"] = category["name"]
        state["subcategory"] = None
        state["action"] = "add_transaction"
        save_user_state_db(user_id, state)

        msg = bot.send_message(
            message.chat.id,
            f"Вы выбрали: *{category['name']}*.\nВведите сумму цифрами:",
            parse_mode="Markdown",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        bot.register_next_step_handler(msg, handle_amount)


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "add_transaction"
    and message.text in ["➡️ Без подкатегории", "❌ Отмена"]
)
def handle_subcategory_special(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)

    logging.info(f"🔍 СПЕЦИАЛЬНАЯ КНОПКА: {message.text}")

    if message.text == "❌ Отмена":
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
        )
        return

    if message.text == "➡️ Без подкатегории":
        if "selected_category_id" not in state or "selected_category_name" not in state:
            bot.send_message(
                message.chat.id,
                "❌ Ошибка: данные потеряны. Начните заново.",
                reply_markup=get_main_menu(),
            )
            delete_user_state_db(user_id)
            return

        state["category_id"] = state["selected_category_id"]
        state["category_name"] = state["selected_category_name"]
        state["subcategory"] = None
        state["action"] = "add_transaction"
        save_user_state_db(user_id, state)

        msg = bot.send_message(
            message.chat.id,
            f"Вы выбрали: *{state['category_name']}*.\nВведите сумму цифрами:",
            parse_mode="Markdown",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        bot.register_next_step_handler(msg, handle_amount)


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "add_transaction"
    and message.text not in ["➡️ Без подкатегории", "❌ Отмена"]
)
def handle_subcategory_selection(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    selected_subcategory = message.text

    logging.info("=" * 50)
    logging.info(f"🔍 Выбрана подкатегория: '{selected_subcategory}'")

    # Проверяем состояние
    if "subcategories" not in state:
        logging.error("❌ В СОСТОЯНИИ НЕТ subcategories!")
        bot.send_message(
            message.chat.id,
            "❌ Ошибка: список подкатегорий потерян. Начните заново.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    subcategories = state.get("subcategories", [])
    logging.info(f"📂 Доступно подкатегорий: {len(subcategories)}")
    for sub in subcategories:
        logging.info(f"  - '{sub['name']}' (id={sub.get('id')})")

    if not subcategories:
        logging.error("❌ СПИСОК ПОДКАТЕГОРИЙ ПУСТ!")
        bot.send_message(
            message.chat.id,
            "❌ Ошибка: нет доступных подкатегорий. Начните заново.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    # Ищем подкатегорию
    selected_sub = None

    # 1. Точное совпадение
    for sub in subcategories:
        if sub["name"] == selected_subcategory:
            selected_sub = sub
            logging.info(f"✅ Найдено по точному совпадению: {sub['name']}")
            break

    # 2. Без эмодзи
    if not selected_sub:
        clean_label = "".join(
            ch for ch in selected_subcategory if ch.isalnum() or ch.isspace()
        ).strip()
        logging.info(f"🔍 Ищем без эмодзи: '{clean_label}'")
        for sub in subcategories:
            clean_sub = "".join(
                ch for ch in sub["name"] if ch.isalnum() or ch.isspace()
            ).strip()
            if clean_sub == clean_label:
                selected_sub = sub
                logging.info(f"✅ Найдено без эмодзи: {sub['name']}")
                break

    if not selected_sub:
        logging.error(f"❌ ПОДКАТЕГОРИЯ НЕ НАЙДЕНА: '{selected_subcategory}'")
        available = [sub["name"] for sub in subcategories]
        logging.error(f"   ДОСТУПНО: {available}")
        bot.send_message(
            message.chat.id,
            f"❌ Подкатегория не найдена. Пожалуйста, выберите из списка.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    logging.info(
        f"✅ НАЙДЕНА ПОДКАТЕГОРИЯ: {selected_sub['name']} (id={selected_sub['id']})"
    )

    # Сохраняем выбранную подкатегорию
    parent_name = state.get("selected_category_name", "Категория")
    state["category_id"] = selected_sub["id"]
    state["category_name"] = f"{parent_name} → {selected_sub['name']}"
    state["subcategory"] = selected_sub["name"]
    state["action"] = "add_transaction"
    save_user_state_db(user_id, state)

    msg = bot.send_message(
        message.chat.id,
        f"Вы выбрали: *{state['category_name']}*.\nВведите сумму цифрами:",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, handle_amount)


@bot.message_handler(func=lambda message: message.text == "❌ Отмена")
def cancel_operation(message):
    delete_user_state_db(message.from_user.id)
    bot.send_message(
        message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
    )


def handle_amount(message):
    user_id = message.from_user.id
    input_text = message.text.replace(",", ".")
    state = get_user_state_db(user_id)

    if not state or state.get("action") != "add_transaction":
        bot.send_message(
            message.chat.id,
            "Ошибка сессии. Начните заново.",
            reply_markup=get_main_menu(),
        )
        return

    try:
        amount = float(input_text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        msg = bot.send_message(
            message.chat.id, "❌ Введите положительное число цифрами:"
        )
        bot.register_next_step_handler(msg, handle_amount)
        return

    trans_type = state["type"]
    category_id = state["category_id"]
    category_name = state["category_name"]
    family_id = get_user_family_db(user_id)
    user_name = message.from_user.first_name

    trans_type_text = "Расход" if trans_type == "expense" else "Доход"
    add_transaction_db(
        family_id,
        user_id,
        user_name,
        trans_type_text,
        category_id,
        amount,
        datetime.now(),
    )
    delete_user_state_db(user_id)
    load_data_to_memory()

    response = f"✅ Записано!\n• {trans_type_text}: {amount:.2f} руб.\n• Категория: {category_name}"

    if trans_type == "expense":
        limits = get_budget_limits_db(family_id)
        if category_id in limits:
            now = datetime.now()
            start_date = datetime(now.year, now.month, 1)
            if now.month == 12:
                end_date = datetime(now.year + 1, 1, 1) - timedelta(days=1)
            else:
                end_date = datetime(now.year, now.month + 1, 1) - timedelta(days=1)

            month_trans = get_transactions_db(
                family_id, start_date=start_date, end_date=end_date
            )
            current_expense = sum(
                t["amount"]
                for t in month_trans
                if t["type"] == "Расход" and t["category_id"] == category_id
            )
            limit = limits[category_id]["limit"]
            percentage = (current_expense / limit) * 100 if limit > 0 else 0

            if percentage >= 100:
                response += f"\n\n⚠️ **ПРЕВЫШЕНИЕ БЮДЖЕТА!** ⚠️\nЛимит: {limit:.2f} руб.\nПревышение: {current_expense - limit:.2f} руб."
            elif percentage >= 80:
                response += f"\n\n⚠️ **ВНИМАНИЕ! Близки к лимиту!**\nЛимит: {limit:.2f} руб.\nОстаток: {limit - current_expense:.2f} руб."

    bot.send_message(
        message.chat.id,
        response,
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )


# --- ОТМЕНА ОПЕРАЦИИ С ПОДТВЕРЖДЕНИЕМ ---
@bot.message_handler(func=lambda message: message.text == "↩️ Отменить операцию")
def cancel_last_transaction(message):
    user_id = message.from_user.id
    family_id = get_user_family_db(user_id)
    if not family_id:
        bot.send_message(
            message.chat.id, "Сначала войдите в семью!", reply_markup=get_auth_menu()
        )
        return

    last_trans = get_last_user_transaction_db(family_id, user_id)
    if not last_trans:
        bot.send_message(
            message.chat.id,
            "Вы еще не вносили никаких операций, отменять нечего! 🤷‍♂️",
            reply_markup=get_main_menu(),
        )
        return

    save_user_state_db(
        user_id,
        {
            "action": "confirm_cancel",
            "transaction_id": last_trans["id"],
            "transaction_info": f"{last_trans['type']} на сумму {last_trans['amount']:.2f} руб. ({last_trans['category_name']})",
        },
    )

    bot.send_message(
        message.chat.id,
        f"⚠️ **Подтверждение отмены**\n\nВы действительно хотите отменить последнюю операцию?\n\n📌 {last_trans['type']} на сумму `{last_trans['amount']:.2f} руб.`\n📅 {last_trans['date'].strftime('%d.%m.%Y %H:%M')}\n🏷️ {last_trans['category_name']}",
        parse_mode="Markdown",
        reply_markup=get_cancel_confirmation_menu(),
    )


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "confirm_cancel"
    and message.text in ["❌ Удалить", "🔙 Отмена"]
)
def handle_cancel_confirmation(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)

    if message.text == "🔙 Отмена":
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id,
            "Операция отменена.",
            reply_markup=get_main_menu(),
        )
        return

    transaction_id = state["transaction_id"]
    delete_transaction_db(transaction_id)
    load_data_to_memory()
    delete_user_state_db(user_id)

    bot.send_message(
        message.chat.id,
        "✅ Операция успешно удалена!",
        reply_markup=get_main_menu(),
    )


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "confirm_cancel"
    and message.text not in ["❌ Удалить", "🔙 Отмена"]
)
def handle_cancel_confirmation_invalid(message):
    bot.send_message(
        message.chat.id,
        "Пожалуйста, выберите один из предложенных вариантов: «❌ Удалить» или «🔙 Отмена».",
        reply_markup=get_cancel_confirmation_menu(),
    )


# --- ИСТОРИЯ И БАЛАНС ---
@bot.message_handler(func=lambda message: message.text == "📊 История и Баланс")
def show_balance_and_history(message):
    user_id = message.from_user.id
    family_id = get_user_family_db(user_id)
    if not family_id:
        bot.send_message(
            message.chat.id, "Сначала войдите в семью!", reply_markup=get_auth_menu()
        )
        return

    now = datetime.now()
    start_of_month = datetime(now.year, now.month, 1)
    seven_days_ago = now - timedelta(days=7)

    transactions = get_transactions_db(
        family_id, start_date=seven_days_ago, end_date=now
    )

    month_transactions = get_transactions_db(
        family_id, start_date=start_of_month, end_date=now
    )

    total_income = sum(t["amount"] for t in month_transactions if t["type"] == "Доход")
    total_expense = sum(
        t["amount"] for t in month_transactions if t["type"] == "Расход"
    )
    balance = total_income - total_expense

    days_in_month = (now - start_of_month).days + 1
    avg_spent_per_day = total_expense / days_in_month if days_in_month > 0 else 0

    response = f"💰 **БАЛАНС СЕМЬИ:** {balance:+.2f} руб.\n"
    response += f"   📥 Доходы: {total_income:.2f} руб.\n"
    response += f"   📤 Расходы: {total_expense:.2f} руб.\n\n"

    response += f"📊 **СТАТИСТИКА С НАЧАЛА МЕСЯЦА:**\n"
    response += f"   📥 Доходы: {total_income:.2f} руб.\n"
    response += f"   📤 Расходы: {total_expense:.2f} руб.\n"
    response += f"   💰 Остаток: {balance:+.2f} руб.\n"
    response += f"   📅 Дней: {days_in_month}\n"
    response += f"   📈 Средние траты в день: {avg_spent_per_day:.2f} руб.\n\n"

    if not transactions:
        response += "📋 **За последние 7 дней операций не было.**"
    else:
        response += "📋 **ПОСЛЕДНИЕ 7 ДНЕЙ:**\n\n"

        current_date = None
        for t in transactions:
            t_date = (
                t["date"].date()
                if isinstance(t["date"], datetime)
                else datetime.strptime(t["date"], "%Y-%m-%d %H:%M:%S.%f").date()
            )
            if current_date != t_date:
                current_date = t_date
                if current_date == now.date():
                    day_label = "Сегодня"
                elif current_date == (now - timedelta(days=1)).date():
                    day_label = "Вчера"
                else:
                    day_label = current_date.strftime("%d.%m")
                response += f"**{day_label} ({current_date.strftime('%d.%m')}):**\n"

            sign = "-" if t["type"] == "Расход" else "+"
            time_str = (
                t["date"].strftime("%H:%M")
                if isinstance(t["date"], datetime)
                else datetime.strptime(t["date"], "%Y-%m-%d %H:%M:%S.%f").strftime(
                    "%H:%M"
                )
            )
            category_display = get_category_full_name(t["category_id"])
            emoji = get_category_emoji(category_display)
            response += f"   {emoji} {category_display:<20} {sign}{t['amount']:.2f} р.  ({t['user_name']}, {time_str})\n"

    bot.send_message(message.chat.id, response, parse_mode="Markdown")


# --- ОТЧЕТЫ ---
@bot.message_handler(func=lambda message: message.text == "📊 Отчеты")
def reports_handler(message):
    user_id = message.from_user.id
    if not get_user_family_db(user_id):
        bot.send_message(
            message.chat.id, "Сначала войдите в семью!", reply_markup=get_auth_menu()
        )
        return
    bot.send_message(
        message.chat.id,
        "📊 **Выберите тип отчета:**\n\n• Семейный отчет - общая статистика всей семьи\n• Личный отчет - только ваши доходы и расходы",
        parse_mode="Markdown",
        reply_markup=get_report_type_menu(),
    )


@bot.message_handler(
    func=lambda message: message.text in ["🏠 Семейный отчет", "👤 Личный отчет"]
)
def ask_period(message):
    user_id = message.from_user.id
    report_type = "family" if message.text == "🏠 Семейный отчет" else "personal"
    save_user_state_db(user_id, {"action": "select_period", "report_type": report_type})
    bot.send_message(
        message.chat.id,
        f"📊 **{message.text}**\n\nВыберите период:",
        parse_mode="Markdown",
        reply_markup=get_period_menu(),
    )


@bot.message_handler(func=lambda message: message.text == "📅 Текущий месяц")
def current_month_report(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    if not state or state.get("action") != "select_period":
        bot.send_message(
            message.chat.id,
            "Начните заново через меню отчетов.",
            reply_markup=get_main_menu(),
        )
        return
    report_type, now = state["report_type"], datetime.now()
    start_date = datetime(now.year, now.month, 1)
    if now.month == 12:
        end_date = datetime(now.year + 1, 1, 1) - timedelta(days=1)
    else:
        end_date = datetime(now.year, now.month + 1, 1) - timedelta(days=1)
    generate_period_report(message.chat.id, user_id, report_type, start_date, end_date)
    delete_user_state_db(user_id)


@bot.message_handler(func=lambda message: message.text == "🗓️ Произвольный период")
def ask_start_date(message):
    user_id = message.from_user.id
    if not get_user_state_db(user_id).get("action") == "select_period":
        bot.send_message(
            message.chat.id,
            "Начните заново через меню отчетов.",
            reply_markup=get_main_menu(),
        )
        return
    msg = bot.send_message(
        message.chat.id,
        "📅 **Введите начальную дату**\nФормат: ДД.ММ.ГГГГ\nНапример: 01.01.2024",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, process_start_date)


def process_start_date(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    if not state:
        return
    try:
        start_date = datetime.strptime(message.text.strip(), "%d.%m.%Y")
        state["start_date"] = start_date.isoformat()
        save_user_state_db(user_id, state)
        msg = bot.send_message(
            message.chat.id,
            "📅 **Введите конечную дату**\nФормат: ДД.ММ.ГГГГ",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, process_end_date)
    except ValueError:
        msg = bot.send_message(
            message.chat.id, "❌ Неверный формат! Введите дату в формате ДД.ММ.ГГГГ"
        )
        bot.register_next_step_handler(msg, process_start_date)


def process_end_date(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    if not state:
        return
    try:
        end_date = datetime.strptime(message.text.strip(), "%d.%m.%Y")
        start_date = datetime.fromisoformat(state["start_date"])
        if end_date < start_date:
            msg = bot.send_message(
                message.chat.id,
                "❌ Конечная дата не может быть раньше начальной!\nВведите конечную дату заново:",
            )
            bot.register_next_step_handler(msg, process_end_date)
            return
        if (end_date - start_date).days > 365:
            bot.send_message(
                message.chat.id,
                "❌ Выбранный период превышает 1 год (365 дней)!",
                reply_markup=get_main_menu(),
            )
            delete_user_state_db(user_id)
            return
        generate_period_report(
            message.chat.id, user_id, state["report_type"], start_date, end_date
        )
        delete_user_state_db(user_id)
    except ValueError:
        msg = bot.send_message(
            message.chat.id, "❌ Неверный формат! Введите дату в формате ДД.ММ.ГГГГ"
        )
        bot.register_next_step_handler(msg, process_end_date)


def generate_period_report(chat_id, user_id, report_type, start_date, end_date):
    family_id = get_user_family_db(user_id)
    days_in_period = (end_date - start_date).days + 1

    if report_type == "family":
        transactions = get_transactions_db(
            family_id, start_date=start_date, end_date=end_date
        )
        title = "🏠 **СЕМЕЙНЫЙ ОТЧЕТ**"
    else:
        transactions = get_transactions_db(
            family_id, user_id=user_id, start_date=start_date, end_date=end_date
        )
        title = "👤 **ЛИЧНЫЙ ОТЧЕТ**"

    if not transactions:
        bot.send_message(
            chat_id,
            f"{title}\n\n📅 {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}\n\n❌ За этот период нет операций.",
            parse_mode="Markdown",
            reply_markup=get_main_menu(),
        )
        return

    total_income = sum(t["amount"] for t in transactions if t["type"] == "Доход")
    total_expense = sum(t["amount"] for t in transactions if t["type"] == "Расход")
    balance = total_income - total_expense

    response = f"**{title}**\n"
    response += f"📅 {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')} ({days_in_period} дн.)\n\n"

    response += f"💰 **БАЛАНС:** {balance:+.2f} руб.\n"
    response += f"   📥 Доходы: {total_income:.2f} руб.\n"
    response += f"   📤 Расходы: {total_expense:.2f} руб.\n\n"

    avg_spent = total_expense / days_in_period if days_in_period > 0 else 0
    avg_income = total_income / days_in_period if days_in_period > 0 else 0
    response += f"📊 Средние траты в день: {avg_spent:.2f} руб.\n"
    response += f"📈 Средние доходы в день: {avg_income:.2f} руб.\n\n"

    if total_expense > 0:
        response += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        response += "📊 **РАСХОДЫ ПО КАТЕГОРИЯМ:**\n\n"

        expense_by_category = {}
        for t in transactions:
            if t["type"] == "Расход":
                cat_name = get_category_full_name(t["category_id"])
                if cat_name not in expense_by_category:
                    expense_by_category[cat_name] = 0
                expense_by_category[cat_name] += t["amount"]

        sorted_cats = sorted(
            expense_by_category.items(), key=lambda x: x[1], reverse=True
        )

        for cat_name, amount in sorted_cats:
            percentage = (amount / total_expense) * 100
            bar_length = int((amount / sorted_cats[0][1]) * 20) if sorted_cats else 0
            bar = "█" * bar_length + "░" * (20 - bar_length)

            response += f"{get_category_emoji(cat_name)} {cat_name}: {amount:.2f} руб. ({percentage:.1f}%)\n"
            response += f"   `{bar}`\n"

    previous_end = start_date - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days_in_period - 1)

    if report_type == "family":
        prev_transactions = get_transactions_db(
            family_id, start_date=previous_start, end_date=previous_end
        )
    else:
        prev_transactions = get_transactions_db(
            family_id, user_id=user_id, start_date=previous_start, end_date=previous_end
        )

    if prev_transactions:
        prev_income = sum(
            t["amount"] for t in prev_transactions if t["type"] == "Доход"
        )
        prev_expense = sum(
            t["amount"] for t in prev_transactions if t["type"] == "Расход"
        )
        prev_balance = prev_income - prev_expense

        response += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        response += "📈 **СРАВНЕНИЕ С ПРЕДЫДУЩИМ ПЕРИОДОМ:**\n\n"

        if prev_income > 0:
            change = ((total_income - prev_income) / prev_income) * 100
            arrow = "📈" if change > 0 else "📉" if change < 0 else "➡️"
            response += f"📥 Доходы: {total_income:.2f} → {prev_income:.2f} ({change:+.1f}%) {arrow}\n"
        if prev_expense > 0:
            change = ((total_expense - prev_expense) / prev_expense) * 100
            arrow = "📈" if change > 0 else "📉" if change < 0 else "➡️"
            response += f"📤 Расходы: {total_expense:.2f} → {prev_expense:.2f} ({change:+.1f}%) {arrow}\n"
        if prev_balance != 0:
            change = (
                ((balance - prev_balance) / abs(prev_balance)) * 100
                if prev_balance != 0
                else 0
            )
            arrow = "📈" if change > 0 else "📉" if change < 0 else "➡️"
            response += f"💰 Остаток: {balance:.2f} → {prev_balance:.2f} ({change:+.1f}%) {arrow}\n"

    expenses = [t for t in transactions if t["type"] == "Расход"]
    if expenses:
        expenses.sort(key=lambda x: x["amount"], reverse=True)
        response += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        response += "🏆 **ТОП-5 САМЫХ КРУПНЫХ ТРАТ:**\n\n"
        for i, exp in enumerate(expenses[:5], 1):
            cat_name = get_category_full_name(exp["category_id"])
            date_str = (
                exp["date"].strftime("%d.%m")
                if isinstance(exp["date"], datetime)
                else datetime.strptime(exp["date"], "%Y-%m-%d %H:%M:%S.%f").strftime(
                    "%d.%m"
                )
            )
            user_name = exp["user_name"]
            response += f"{i}. {get_category_emoji(cat_name)} {cat_name}: {exp['amount']:.2f} руб. ({date_str}, {user_name})\n"

    bot.send_message(
        chat_id, response, parse_mode="Markdown", reply_markup=get_main_menu()
    )


# --- БЮДЖЕТНЫЕ ЛИМИТЫ ---
@bot.message_handler(func=lambda message: message.text == "💰 Бюджетные лимиты")
def budget_limits_handler(message):
    user_id = message.from_user.id
    if not get_user_family_db(user_id):
        bot.send_message(
            message.chat.id, "Сначала войдите в семью!", reply_markup=get_auth_menu()
        )
        return
    bot.send_message(
        message.chat.id,
        "💰 **Управление бюджетными лимитами**\n\nЛимиты общие для всей семьи. Бот предупредит при 80% и 100% использования.",
        parse_mode="Markdown",
        reply_markup=get_limits_menu(),
    )


@bot.message_handler(func=lambda message: message.text == "📝 Установить лимит")
def set_limit_category(message):
    user_id = message.from_user.id
    family_id = get_user_family_db(user_id)
    if not family_id:
        return

    categories = get_categories_db(family_id, "expense")
    if not categories:
        bot.send_message(
            message.chat.id,
            "Нет категорий для установки лимита. Сначала создайте категорию.",
            reply_markup=get_main_menu(),
        )
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for cat in categories:
        markup.add(types.KeyboardButton(cat["name"]))
    markup.add(types.KeyboardButton("❌ Отмена"))

    save_user_state_db(user_id, {"action": "set_limit", "categories": categories})
    bot.send_message(
        message.chat.id, "Выберите категорию для установки лимита:", reply_markup=markup
    )


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "set_limit"
    and message.text != "❌ Отмена"
)
def set_limit_amount(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    selected_category = message.text.strip()

    categories = state.get("categories", [])
    category = None
    for c in categories:
        if c["name"] == selected_category:
            category = c
            break

    if not category:
        clean_sel = "".join(
            ch for ch in selected_category if ch.isalnum() or ch.isspace()
        ).strip()
        for c in categories:
            clean_cat = "".join(
                ch for ch in c["name"] if ch.isalnum() or ch.isspace()
            ).strip()
            if clean_cat == clean_sel:
                category = c
                break

    if not category:
        bot.send_message(
            message.chat.id,
            "❌ Категория не найдена. Попробуйте еще раз.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    state["category_id"] = category["id"]
    state["category_name"] = category["name"]
    save_user_state_db(user_id, state)

    msg = bot.send_message(
        message.chat.id,
        f"Введите лимит для категории *{category['name']}* (в рублях):\nНапример: 15000",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, save_limit_amount)


def save_limit_amount(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    if not state or state.get("action") != "set_limit":
        bot.send_message(
            message.chat.id, "Ошибка. Начните заново.", reply_markup=get_main_menu()
        )
        return

    try:
        limit = float(message.text.replace(",", "."))
        if limit <= 0:
            raise ValueError
    except ValueError:
        msg = bot.send_message(message.chat.id, "❌ Введите положительное число:")
        bot.register_next_step_handler(msg, save_limit_amount)
        return

    family_id = get_user_family_db(user_id)
    category_id = state["category_id"]
    category_name = state["category_name"]

    set_budget_limit_db(family_id, category_id, limit)
    load_data_to_memory()
    delete_user_state_db(user_id)

    bot.send_message(
        message.chat.id,
        f"✅ **Лимит установлен!**\n\n📌 Категория: {category_name}\n💰 Лимит: {limit:.2f} руб.",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "set_limit"
    and message.text == "❌ Отмена"
)
def set_limit_cancel(message):
    delete_user_state_db(message.from_user.id)
    bot.send_message(
        message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
    )


@bot.message_handler(func=lambda message: message.text == "📊 Просмотреть лимиты")
def view_limits(message):
    user_id = message.from_user.id
    family_id = get_user_family_db(user_id)
    if not family_id:
        return

    limits = get_budget_limits_db(family_id)
    if not limits:
        bot.send_message(
            message.chat.id,
            "📊 **Бюджетные лимиты**\n\nЛимиты пока не установлены.",
            parse_mode="Markdown",
            reply_markup=get_limits_menu(),
        )
        return

    response = "📊 **Бюджетные лимиты:**\n\n"
    for category_id, data in limits.items():
        category_name = data["name"]
        limit = data["limit"]
        now = datetime.now()
        start_date = datetime(now.year, now.month, 1)
        if now.month == 12:
            end_date = datetime(now.year + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = datetime(now.year, now.month + 1, 1) - timedelta(days=1)

        month_trans = get_transactions_db(
            family_id, start_date=start_date, end_date=end_date
        )
        current_expense = sum(
            t["amount"]
            for t in month_trans
            if t["type"] == "Расход" and t["category_id"] == category_id
        )
        percentage = (current_expense / limit) * 100 if limit > 0 else 0

        if percentage >= 100:
            status = "🔴 **ПРЕВЫШЕН**"
        elif percentage >= 80:
            status = "🟡 **ПРЕДУПРЕЖДЕНИЕ**"
        else:
            status = "🟢 Норма"

        response += f"• *{category_name}*\n"
        response += f"  Лимит: {limit:.2f} руб.\n"
        response += f"  Потрачено: {current_expense:.2f} руб. ({percentage:.1f}%)\n"
        response += f"  Статус: {status}\n\n"

    bot.send_message(
        message.chat.id, response, parse_mode="Markdown", reply_markup=get_limits_menu()
    )


@bot.message_handler(func=lambda message: message.text == "🗑 Удалить лимит")
def delete_limit_category(message):
    user_id = message.from_user.id
    family_id = get_user_family_db(user_id)
    if not family_id:
        return

    limits = get_budget_limits_db(family_id)
    if not limits:
        bot.send_message(
            message.chat.id,
            "Нет установленных лимитов.",
            reply_markup=get_limits_menu(),
        )
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for category_id, data in limits.items():
        markup.add(types.KeyboardButton(data["name"]))
    markup.add(types.KeyboardButton("❌ Отмена"))

    save_user_state_db(user_id, {"action": "delete_limit", "limits": limits})
    bot.send_message(
        message.chat.id, "Выберите категорию для удаления лимита:", reply_markup=markup
    )


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "delete_limit"
    and message.text != "❌ Отмена"
)
def confirm_delete_limit(message):
    user_id = message.from_user.id
    category_name = message.text

    limits = get_user_state_db(user_id).get("limits", {})
    category_id = None
    for cid, data in limits.items():
        if data["name"] == category_name:
            category_id = cid
            break

    if not category_id:
        bot.send_message(
            message.chat.id,
            "❌ Категория не найдена.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    family_id = get_user_family_db(user_id)
    delete_budget_limit_db(family_id, category_id)
    load_data_to_memory()
    delete_user_state_db(user_id)

    bot.send_message(
        message.chat.id,
        f"✅ Лимит для категории '{category_name}' удален.",
        reply_markup=get_main_menu(),
    )


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "delete_limit"
    and message.text == "❌ Отмена"
)
def delete_limit_cancel(message):
    delete_user_state_db(message.from_user.id)
    bot.send_message(
        message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
    )


# --- НАПОМИНАНИЯ ---
@bot.message_handler(func=lambda message: message.text == "⏰ Напоминания о платежах")
def reminders_handler(message):
    user_id = message.from_user.id
    if not get_user_family_db(user_id):
        bot.send_message(
            message.chat.id, "Сначала войдите в семью!", reply_markup=get_auth_menu()
        )
        return
    bot.send_message(
        message.chat.id,
        "📌 **Управление напоминаниями о платежах**\n\nЗдесь вы можете настроить автоматические напоминания о регулярных платежах.",
        parse_mode="Markdown",
        reply_markup=get_reminders_menu(),
    )


@bot.message_handler(func=lambda message: message.text == "➕ Добавить напоминание")
def add_reminder_start(message):
    save_user_state_db(message.from_user.id, {"action": "add_reminder"})
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(
        types.KeyboardButton("💰 Расход"),
        types.KeyboardButton("📈 Доход"),
        types.KeyboardButton("❌ Отмена"),
    )
    bot.send_message(message.chat.id, "Выберите тип платежа:", reply_markup=markup)


@bot.message_handler(
    func=lambda message: message.text in ["💰 Расход", "📈 Доход"]
    and get_user_state_db(message.from_user.id).get("action") == "add_reminder"
)
def add_reminder_type(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    trans_type = "expense" if message.text == "💰 Расход" else "income"
    state["reminder_type"] = trans_type
    save_user_state_db(user_id, state)

    categories = get_categories_db(get_user_family_db(user_id), trans_type)
    if not categories:
        bot.send_message(
            message.chat.id,
            "Нет доступных категорий. Создайте через меню «🏷️ Категории».",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for cat in categories:
        markup.add(types.KeyboardButton(cat["name"]))
    markup.add(types.KeyboardButton("❌ Отмена"))

    save_user_state_db(
        user_id, {"action": "add_reminder_category", "categories": categories}
    )
    bot.send_message(
        message.chat.id, "Выберите категорию платежа:", reply_markup=markup
    )


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "add_reminder_category"
    and message.text != "❌ Отмена"
)
def add_reminder_category(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    selected_category = message.text.strip()

    categories = state.get("categories", [])
    category = None
    for c in categories:
        if c["name"] == selected_category:
            category = c
            break

    if not category:
        clean_sel = "".join(
            ch for ch in selected_category if ch.isalnum() or ch.isspace()
        ).strip()
        for c in categories:
            clean_cat = "".join(
                ch for ch in c["name"] if ch.isalnum() or ch.isspace()
            ).strip()
            if clean_cat == clean_sel:
                category = c
                break

    if not category:
        bot.send_message(
            message.chat.id,
            "❌ Категория не найдена.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    state["reminder_category"] = category["name"]
    state["action"] = "add_reminder"
    save_user_state_db(user_id, state)

    msg = bot.send_message(
        message.chat.id,
        "Введите название платежа (например: 'Квартплата', 'Netflix'):",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, add_reminder_title)


@bot.message_handler(
    func=lambda message: message.text
    and get_user_state_db(message.from_user.id).get("action") == "add_reminder_category"
    and message.text == "❌ Отмена"
)
def add_reminder_category_cancel(message):
    delete_user_state_db(message.from_user.id)
    bot.send_message(
        message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
    )


def add_reminder_title(message):
    user_id = message.from_user.id
    if message.text == "❌ Отмена":
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
        )
        return
    state = get_user_state_db(user_id)
    if not state or state.get("action") != "add_reminder":
        bot.send_message(
            message.chat.id, "Ошибка. Начните заново.", reply_markup=get_main_menu()
        )
        return
    state["reminder_title"] = message.text
    save_user_state_db(user_id, state)
    msg = bot.send_message(message.chat.id, "Введите сумму платежа (цифрами):")
    bot.register_next_step_handler(msg, add_reminder_amount)


def add_reminder_amount(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    if not state or state.get("action") != "add_reminder":
        bot.send_message(
            message.chat.id, "Ошибка. Начните заново.", reply_markup=get_main_menu()
        )
        return
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        msg = bot.send_message(message.chat.id, "❌ Введите положительное число:")
        bot.register_next_step_handler(msg, add_reminder_amount)
        return
    state["reminder_amount"] = amount
    save_user_state_db(user_id, state)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(
        types.KeyboardButton("📅 Ежемесячно"),
        types.KeyboardButton("📆 Еженедельно"),
        types.KeyboardButton("❌ Отмена"),
    )
    bot.send_message(message.chat.id, "Выберите периодичность:", reply_markup=markup)


@bot.message_handler(
    func=lambda message: message.text in ["📅 Ежемесячно", "📆 Еженедельно"]
    and get_user_state_db(message.from_user.id).get("action") == "add_reminder"
)
def add_reminder_frequency(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    if not state or state.get("action") != "add_reminder":
        bot.send_message(
            message.chat.id, "Ошибка. Начните заново.", reply_markup=get_main_menu()
        )
        return
    frequency = "monthly" if message.text == "📅 Ежемесячно" else "weekly"
    state["reminder_frequency"] = frequency
    save_user_state_db(user_id, state)
    if frequency == "monthly":
        msg = bot.send_message(
            message.chat.id,
            "Введите число месяца для списания (1-31):",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        bot.register_next_step_handler(msg, add_reminder_day_of_month)
    else:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        for day in [
            "Понедельник",
            "Вторник",
            "Среда",
            "Четверг",
            "Пятница",
            "Суббота",
            "Воскресенье",
        ]:
            markup.add(types.KeyboardButton(day))
        markup.add(types.KeyboardButton("❌ Отмена"))
        bot.send_message(message.chat.id, "Выберите день недели:", reply_markup=markup)


def add_reminder_day_of_month(message):
    user_id = message.from_user.id
    try:
        day = int(message.text)
        if day < 1 or day > 31:
            raise ValueError
    except ValueError:
        msg = bot.send_message(message.chat.id, "❌ Введите число от 1 до 31:")
        bot.register_next_step_handler(msg, add_reminder_day_of_month)
        return
    state = get_user_state_db(user_id)
    if not state or state.get("action") != "add_reminder":
        bot.send_message(
            message.chat.id, "Ошибка. Начните заново.", reply_markup=get_main_menu()
        )
        return
    state["reminder_day"] = day
    save_user_state_db(user_id, state)
    ask_notify_days(message.chat.id, user_id)


@bot.message_handler(
    func=lambda message: message.text
    in [
        "Понедельник",
        "Вторник",
        "Среда",
        "Четверг",
        "Пятница",
        "Суббота",
        "Воскресенье",
    ]
    and get_user_state_db(message.from_user.id).get("action") == "add_reminder"
)
def add_reminder_day_of_week(message):
    user_id = message.from_user.id
    days_map = {
        "Понедельник": 0,
        "Вторник": 1,
        "Среда": 2,
        "Четверг": 3,
        "Пятница": 4,
        "Суббота": 5,
        "Воскресенье": 6,
    }
    state = get_user_state_db(user_id)
    if not state or state.get("action") != "add_reminder":
        bot.send_message(
            message.chat.id, "Ошибка. Начните заново.", reply_markup=get_main_menu()
        )
        return
    state["reminder_day"] = days_map[message.text]
    save_user_state_db(user_id, state)
    ask_notify_days(message.chat.id, user_id)


def ask_notify_days(chat_id, user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(
        types.KeyboardButton("1 день"),
        types.KeyboardButton("2 дня"),
        types.KeyboardButton("3 дня"),
        types.KeyboardButton("Не напоминать"),
        types.KeyboardButton("❌ Отмена"),
    )
    msg = bot.send_message(chat_id, "За сколько дней напоминать?", reply_markup=markup)
    bot.register_next_step_handler(msg, add_reminder_notify_days)


def add_reminder_notify_days(message):
    user_id = message.from_user.id
    if message.text == "❌ Отмена":
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
        )
        return
    days_map = {"1 день": [1], "2 дня": [2], "3 дня": [3], "Не напоминать": []}
    notify_days = days_map.get(message.text, [1])
    state = get_user_state_db(user_id)
    if not state or state.get("action") != "add_reminder":
        bot.send_message(
            message.chat.id, "Ошибка. Начните заново.", reply_markup=get_main_menu()
        )
        return
    family_id = get_user_family_db(user_id)
    next_date = calculate_next_date(state["reminder_frequency"], state["reminder_day"])
    reminder = {
        "title": state["reminder_title"],
        "amount": state["reminder_amount"],
        "category": state["reminder_category"],
        "type": state["reminder_type"],
        "frequency": state["reminder_frequency"],
        "day": state["reminder_day"],
        "next_due_date": next_date,
        "notify_days_before": notify_days,
    }
    add_reminder_db(family_id, user_id, reminder)
    load_data_to_memory()
    delete_user_state_db(user_id)
    bot.send_message(
        message.chat.id,
        f"✅ **Напоминание создано!**\n\n📌 {reminder['title']}\n💰 {reminder['amount']:.2f} руб.\n📅 Следующий платеж: {next_date.strftime('%d.%m.%Y')}",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )


@bot.message_handler(func=lambda message: message.text == "📋 Список напоминаний")
def list_reminders(message):
    user_id = message.from_user.id
    family_id = get_user_family_db(user_id)
    if not family_id:
        return
    reminders = get_reminders_db(family_id)
    if not reminders:
        bot.send_message(
            message.chat.id,
            "У вас пока нет активных напоминаний.",
            reply_markup=get_reminders_menu(),
        )
        return
    response = "📋 **Ваши напоминания:**\n\n"
    for i, r in enumerate(reminders, 1):
        freq = "Ежемесячно" if r["frequency"] == "monthly" else "Еженедельно"
        response += f"{i}. *{r['title']}*\n   💰 {r['amount']:.2f} руб. | {freq}\n"
    bot.send_message(
        message.chat.id,
        response,
        parse_mode="Markdown",
        reply_markup=get_reminders_menu(),
    )


@bot.message_handler(func=lambda message: message.text == "🗑 Удалить напоминание")
def delete_reminder_start(message):
    user_id = message.from_user.id
    family_id = get_user_family_db(user_id)
    if not family_id:
        return
    reminders = get_reminders_db(family_id)
    if not reminders:
        bot.send_message(
            message.chat.id,
            "Нет активных напоминаний для удаления.",
            reply_markup=get_reminders_menu(),
        )
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for i, r in enumerate(reminders, 1):
        markup.add(types.KeyboardButton(f"{i}. {r['title']}"))
    markup.add(types.KeyboardButton("❌ Отмена"))
    msg = bot.send_message(
        message.chat.id, "Выберите напоминание для удаления:", reply_markup=markup
    )
    bot.register_next_step_handler(msg, delete_reminder_confirm)


def delete_reminder_confirm(message):
    if message.text == "❌ Отмена":
        bot.send_message(
            message.chat.id, "Удаление отменено.", reply_markup=get_main_menu()
        )
        return
    user_id = message.from_user.id
    family_id = get_user_family_db(user_id)
    reminders = get_reminders_db(family_id)
    try:
        index = int(message.text.split(".")[0]) - 1
        delete_reminder_db(reminders[index]["id"])
        load_data_to_memory()
        bot.send_message(
            message.chat.id,
            f"✅ Напоминание '{reminders[index]['title']}' удалено.",
            reply_markup=get_main_menu(),
        )
    except:
        bot.send_message(
            message.chat.id, "Ошибка при удалении.", reply_markup=get_main_menu()
        )


@bot.message_handler(func=lambda message: message.text == "🔙 Назад в главное меню")
def back_to_main(message):
    bot.send_message(
        message.chat.id, "Возвращаемся в главное меню.", reply_markup=get_main_menu()
    )


# --- ФОННЫЙ ПОТОК ДЛЯ ПРОВЕРКИ НАПОМИНАНИЙ ---
def check_reminders():
    while True:
        try:
            current_time = datetime.now()
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM reminders WHERE date(next_due_date) <= date(?)",
                    (current_time,),
                )
                reminders = cursor.fetchall()
                for reminder in reminders:
                    try:
                        bot.send_message(
                            reminder["user_id"],
                            f"⏰ **НАПОМИНАНИЕ О ПЛАТЕЖЕ!**\n\n📌 {reminder['title']}\n💰 Сумма: {reminder['amount']:.2f} руб.\n📅 Дата платежа: сегодня!\n🏷 Категория: {reminder['category']}\n\nНе забудьте добавить расход через меню бота.",
                            parse_mode="Markdown",
                        )
                        next_date = calculate_next_date(
                            reminder["frequency"], reminder["day"]
                        )
                        cursor.execute(
                            "UPDATE reminders SET next_due_date = ? WHERE id = ?",
                            (next_date, reminder["id"]),
                        )
                        conn.commit()
                    except Exception as e:
                        logging.error(f"Ошибка отправки уведомления: {e}")
        except Exception as e:
            logging.error(f"Ошибка в проверке напоминаний: {e}")
        time.sleep(1800)


def run_reminder_checker():
    reminder_thread = threading.Thread(target=check_reminders, daemon=True)
    reminder_thread.start()


# --- ЗАПУСК БОТА ---
if __name__ == "__main__":
    print("🤖 Инициализация бота...")
    init_db()
    load_data_to_memory()

    try:
        bot.delete_webhook()
        print("✅ Webhook удалён (если был)")
    except:
        pass

    print("🤖 Бот запущен и готов к работе на Bothost!")
    print("💰 Семейный бюджет бот активен")
    print(f"💾 Данные сохраняются в {DB_NAME}")

    run_reminder_checker()
    print("✅ Планировщик напоминаний запущен")

    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен пользователем")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
