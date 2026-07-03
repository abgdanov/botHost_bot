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
import re
from contextlib import contextmanager

from emoji_dict import get_emoji

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    logger.error("❌ Токен не найден! Установите переменную TELEGRAM_BOT_TOKEN")
    sys.exit(1)

bot = telebot.TeleBot(TOKEN)

USER_FAMILIES = {}
FAMILY_MEMBERS = {}
USER_STATES = {}
TRANSACTIONS = []
REMINDERS = {}
BUDGET_LIMITS = {}

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

        logger.info("✅ База данных инициализирована")


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
        return cursor.rowcount > 0


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
    if not category_name:
        return "📌"

    first_char = category_name[0]
    emoji_ranges = [
        (0x1F600, 0x1F64F),
        (0x1F300, 0x1F5FF),
        (0x1F680, 0x1F6FF),
        (0x2600, 0x26FF),
        (0x2700, 0x27BF),
    ]
    code = ord(first_char)
    for start, end in emoji_ranges:
        if start <= code <= end:
            return first_char

    return get_emoji(category_name)


def add_emoji_to_category_name(name):
    if not name:
        return name

    first_char = name[0]
    emoji_ranges = [
        (0x1F600, 0x1F64F),
        (0x1F300, 0x1F5FF),
        (0x1F680, 0x1F6FF),
        (0x2600, 0x26FF),
        (0x2700, 0x27BF),
    ]
    code = ord(first_char)
    for start, end in emoji_ranges:
        if start <= code <= end:
            return name

    emoji = get_category_emoji(name)
    if emoji and emoji != "📌":
        if not name.startswith(emoji):
            return f"{emoji} {name}"

    return name


def extract_emoji_from_name(name):
    """Извлекает эмодзи из начала названия"""
    if not name:
        return None, name

    first_char = name[0]
    emoji_ranges = [
        (0x1F600, 0x1F64F),
        (0x1F300, 0x1F5FF),
        (0x1F680, 0x1F6FF),
        (0x2600, 0x26FF),
        (0x2700, 0x27BF),
    ]
    code = ord(first_char)
    for start, end in emoji_ranges:
        if start <= code <= end:
            return first_char, name[1:].strip()

    return None, name


def get_category_display_clean(category_id):
    """Возвращает название категории с одним эмодзи (без дублирования)"""
    full_name = get_category_full_name(category_id)
    if not full_name:
        return "❓ Без категории"

    parts = full_name.split(" → ")
    result_parts = []

    for part in parts:
        emoji, clean_name = extract_emoji_from_name(part)
        if emoji:
            # Уже есть эмодзи - оставляем как есть
            result_parts.append(part)
        else:
            # Нет эмодзи - добавляем
            new_emoji = get_category_emoji(part)
            if new_emoji and new_emoji != "📌":
                result_parts.append(f"{new_emoji} {clean_name}")
            else:
                result_parts.append(part)

    return " → ".join(result_parts)


def get_category_display_name(category_name):
    """Возвращает имя категории с эмодзи для отображения"""
    emoji, clean_name = extract_emoji_from_name(category_name)
    if emoji:
        return f"{emoji} {clean_name}"
    else:
        return f"📁 {category_name}"


def get_parent_display_name(category_name):
    """Возвращает имя для кнопки 'На всю категорию'"""
    emoji, clean_name = extract_emoji_from_name(category_name)
    if emoji:
        return f"➡️ На всю категорию {emoji} {clean_name}"
    else:
        return f"➡️ На всю категорию 📁 {category_name}"


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
    logger.info(
        f"📦 Данные загружены: {len(USER_FAMILIES)} пользователей, {len(TRANSACTIONS)} транзакций"
    )


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
        query += " ORDER BY CASE WHEN family_id IS NOT NULL THEN 0 ELSE 1 END, name"
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


def get_all_category_ids_for_limit(family_id, category_id):
    category_info = get_category_by_id(category_id)
    if not category_info:
        return [category_id]

    if category_info["parent_id"] is not None:
        return [category_id]

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM categories WHERE parent_id = ? AND family_id = ?",
            (category_id, family_id),
        )
        subcategories = [row["id"] for row in cursor.fetchall()]

    return [category_id] + subcategories


def check_single_limit(family_id, limit_category_id, limit_amount):
    now = datetime.now()
    start_date = datetime(now.year, now.month, 1)
    if now.month == 12:
        end_date = datetime(now.year + 1, 1, 1) - timedelta(days=1)
    else:
        end_date = datetime(now.year, now.month + 1, 1) - timedelta(days=1)

    month_trans = get_transactions_db(
        family_id, start_date=start_date, end_date=end_date
    )

    category_ids = get_all_category_ids_for_limit(family_id, limit_category_id)

    current_expense = sum(
        t["amount"]
        for t in month_trans
        if t["type"] == "Расход" and t["category_id"] in category_ids
    )

    percentage = (current_expense / limit_amount) * 100 if limit_amount > 0 else 0
    category_name = get_category_display_clean(limit_category_id)

    if percentage >= 100:
        return f"🔴 {category_name}: {percentage:.0f}% ({current_expense:.0f}/{limit_amount:.0f} руб.) ⚠️ ПРЕВЫШЕНИЕ!"

    return None


def check_budget_limits_for_report(family_id):
    limits = get_budget_limits_db(family_id)
    if not limits:
        return []

    warnings = []
    for category_id, data in limits.items():
        warning = check_single_limit(family_id, category_id, data["limit"])
        if warning:
            warnings.append(warning)

    return warnings


def format_amount(amount):
    if amount == int(amount):
        return f"{amount:.1f}"
    return f"{amount:.2f}"


def create_percentage_bar(percent, length=10):
    filled = int(percent / 100 * length)
    empty = length - filled
    return "█" * filled + "░" * empty


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
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn1 = types.InlineKeyboardButton(
        "❌ Да, удалить", callback_data="cancel_confirm_yes"
    )
    btn2 = types.InlineKeyboardButton(
        "🔙 Нет, вернуться", callback_data="cancel_confirm_no"
    )
    markup.add(btn1, btn2)
    return markup


# ============================================
# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============
# ============================================


def format_transaction_date(date_value):
    try:
        if isinstance(date_value, datetime):
            return date_value.strftime("%d.%m.%Y %H:%M")
        elif isinstance(date_value, str):
            for fmt in ["%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(date_value, fmt)
                    return dt.strftime("%d.%m.%Y %H:%M")
                except ValueError:
                    continue
            return date_value
        else:
            return str(date_value)
    except Exception as e:
        logger.error(f"Ошибка форматирования даты: {e}")
        return str(date_value)


def clear_state_and_check_family(message):
    user_id = message.from_user.id
    delete_user_state_db(user_id)

    family_id = get_user_family_db(user_id)
    if not family_id:
        bot.send_message(
            message.chat.id, "Сначала войдите в семью!", reply_markup=get_auth_menu()
        )
        return None
    return family_id


# ============================================
# ========== ХЕНДЛЕРЫ КОМАНД ============
# ============================================


@bot.message_handler(commands=["start"])
def start_message(message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name
    logger.info(f"🔄 Команда /start от пользователя {user_id} ({first_name})")

    delete_user_state_db(user_id)

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


# ============================================
# ========== АВТОРИЗАЦИЯ ============
# ============================================


@bot.message_handler(func=lambda message: message.text == "🏠 Создать новую семью")
def create_family(message):
    user_id = message.from_user.id
    logger.info(f"🏠 Создание семьи от пользователя {user_id}")

    delete_user_state_db(user_id)

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
    user_id = message.from_user.id
    logger.info(f"🔑 Вход по ID семьи от пользователя {user_id}")

    delete_user_state_db(user_id)

    msg = bot.send_message(
        message.chat.id,
        "Введите 5-значный ID семьи:",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, save_family_id)


def save_family_id(message):
    user_id = message.from_user.id
    input_id = message.text.strip()
    logger.info(f"🔍 Проверка ID семьи: {input_id} от пользователя {user_id}")

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


# ============================================
# ========== КАТЕГОРИИ ============
# ============================================


@bot.message_handler(func=lambda message: message.text == "🏷️ Категории")
def categories_handler(message):
    user_id = message.from_user.id
    logger.info(f"🏷️ Категории от пользователя {user_id}")

    family_id = clear_state_and_check_family(message)
    if not family_id:
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
    logger.info(f"➕ Добавить категорию от пользователя {user_id}")

    family_id = clear_state_and_check_family(message)
    if not family_id:
        return

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
    func=lambda message: (
        message.text in ["💰 Расход", "📈 Доход"]
        and get_user_state_db(message.from_user.id).get("action") == "add_category"
    )
)
def add_category_type(message):
    user_id = message.from_user.id
    logger.info(f"📌 Выбран тип категории: {message.text} от пользователя {user_id}")

    state = get_user_state_db(user_id)
    state["category_type"] = "expense" if message.text == "💰 Расход" else "income"
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
        logger.info(f"❌ Отмена добавления категории от пользователя {user_id}")
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
        )
        return

    state = get_user_state_db(user_id)
    if not state or state.get("action") != "add_category":
        logger.warning(f"⚠️ Неверное состояние при добавлении категории: {state}")
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

    name_with_emoji = add_emoji_to_category_name(name)
    if name_with_emoji != name:
        logger.info(f"✨ Добавлен эмодзи: '{name}' → '{name_with_emoji}'")

    family_id = get_user_family_db(user_id)
    category_type = state["category_type"]

    if add_category_db(family_id, name_with_emoji, category_type):
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id,
            f"✅ Категория «{name_with_emoji}» добавлена!",
            reply_markup=get_main_menu(),
        )
    else:
        msg = bot.send_message(
            message.chat.id,
            f"❌ Категория «{name_with_emoji}» уже существует. Введите другое название:",
        )
        bot.register_next_step_handler(msg, add_category_name)


@bot.message_handler(func=lambda message: message.text == "📂 Добавить подкатегорию")
def add_subcategory_start(message):
    user_id = message.from_user.id
    logger.info(f"📂 Добавить подкатегорию от пользователя {user_id}")

    family_id = clear_state_and_check_family(message)
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
    func=lambda message: (
        message.text
        and get_user_state_db(message.from_user.id).get("action") == "add_subcategory"
        and message.text != "❌ Отмена"
    )
)
def add_subcategory_parent(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    parent_name = message.text.strip()
    logger.info(
        f"📌 Выбрана родительская категория: {parent_name} от пользователя {user_id}"
    )

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
        logger.info(f"❌ Отмена добавления подкатегории от пользователя {user_id}")
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
        )
        return

    state = get_user_state_db(user_id)
    if not state or state.get("action") != "add_subcategory":
        logger.warning(f"⚠️ Неверное состояние при добавлении подкатегории: {state}")
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

    name_with_emoji = add_emoji_to_category_name(name)
    if name_with_emoji != name:
        logger.info(
            f"✨ Добавлен эмодзи для подкатегории: '{name}' → '{name_with_emoji}'"
        )

    family_id = get_user_family_db(user_id)
    parent_id = state["parent_id"]
    category_type = state.get("category_type")

    if add_category_db(family_id, name_with_emoji, category_type, parent_id):
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id,
            f"✅ Подкатегория «{name_with_emoji}» добавлена к «{state['parent_name']}»!",
            reply_markup=get_main_menu(),
        )
    else:
        msg = bot.send_message(
            message.chat.id,
            f"❌ Подкатегория «{name_with_emoji}» уже существует. Введите другое название:",
        )
        bot.register_next_step_handler(msg, add_subcategory_name)


@bot.message_handler(func=lambda message: message.text == "🗑️ Удалить категорию")
def delete_category_start(message):
    user_id = message.from_user.id
    logger.info(f"🗑️ Удалить категорию от пользователя {user_id}")

    family_id = clear_state_and_check_family(message)
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
    func=lambda message: (
        message.text
        and get_user_state_db(message.from_user.id).get("action") == "delete_category"
        and message.text != "❌ Отмена"
    )
)
def delete_category_confirm(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    selected_name = message.text.replace(" 📂", "").strip()
    logger.info(f"🗑️ Удаление категории: {selected_name} от пользователя {user_id}")

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
    func=lambda message: (
        message.text in ["да", "нет"]
        and get_user_state_db(message.from_user.id).get("action") == "confirm_delete"
    )
)
def confirm_delete_category(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    logger.info(f"✅ Подтверждение удаления: {message.text} от пользователя {user_id}")

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


@bot.message_handler(func=lambda message: message.text == "📋 Список категорий")
def list_categories(message):
    user_id = message.from_user.id
    logger.info(f"📋 Список категорий от пользователя {user_id}")

    family_id = clear_state_and_check_family(message)
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


# ============================================
# ========== УЧЕТ РАСХОДОВ И ДОХОДОВ ============
# ============================================


@bot.message_handler(
    func=lambda message: message.text in ["📉 Добавить Расход", "📈 Добавить Доход"]
)
def handle_menu(message):
    user_id = message.from_user.id
    logger.info(f"📊 {message.text} от пользователя {user_id}")

    delete_user_state_db(user_id)

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
    func=lambda message: (
        message.text
        and get_user_state_db(message.from_user.id).get("action") == "add_transaction"
    )
)
def handle_transaction_messages(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    text = message.text

    logger.info(
        f"🔍 Обработка сообщения: '{text}' от пользователя {user_id}, waiting_for={state.get('waiting_for')}"
    )

    if state.get("waiting_for") == "subcategory":
        if text == "❌ Отмена":
            logger.info(f"❌ Отмена выбора подкатегории от пользователя {user_id}")
            delete_user_state_db(user_id)
            bot.send_message(
                message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
            )
            return

        if text == "➡️ Без подкатегории":
            logger.info(f"➡️ Выбрано 'Без подкатегории' от пользователя {user_id}")
            if (
                "selected_category_id" not in state
                or "selected_category_name" not in state
            ):
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
            if "waiting_for" in state:
                del state["waiting_for"]
            save_user_state_db(user_id, state)

            msg = bot.send_message(
                message.chat.id,
                f"Вы выбрали: *{state['category_name']}*.\nВведите сумму цифрами:",
                parse_mode="Markdown",
                reply_markup=types.ReplyKeyboardRemove(),
            )
            bot.register_next_step_handler(msg, handle_amount)
            return

        handle_subcategory_selection(message)
        return

    if text == "❌ Отмена":
        logger.info(f"❌ Отмена выбора категории от пользователя {user_id}")
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
        )
        return

    handle_category_selection(message)


def handle_category_selection(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    selected_label = message.text.strip()
    logger.info(f"🔍 Выбрана категория: {selected_label} от пользователя {user_id}")

    family_id = get_user_family_db(user_id)

    categories = state.get("categories", [])
    category = None

    for c in categories:
        if c["name"] == selected_label:
            category = c
            break

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
        logger.error(f"❌ Категория не найдена: {selected_label}")
        bot.send_message(
            message.chat.id,
            "❌ Категория не найдена. Попробуйте еще раз.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    logger.info(f"✅ Найдена категория: {category['name']} (id={category['id']})")

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

    logger.info(f"📂 Найдено подкатегорий: {len(subcategories)}")

    if subcategories:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add(types.KeyboardButton("➡️ Без подкатегории"))
        for sub in subcategories:
            markup.add(types.KeyboardButton(sub["name"]))
        markup.add(types.KeyboardButton("❌ Отмена"))

        state["selected_category_id"] = category["id"]
        state["selected_category_name"] = category["name"]
        state["subcategories"] = subcategories
        state["action"] = "add_transaction"
        state["waiting_for"] = "subcategory"
        save_user_state_db(user_id, state)

        bot.send_message(
            message.chat.id,
            f"Вы выбрали «{category['name']}».\n\nВыберите подкатегорию или «Без подкатегории»:",
            reply_markup=markup,
        )
    else:
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


def handle_subcategory_selection(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    selected_subcategory = message.text

    logger.info(
        f"🔍 Выбрана подкатегория: '{selected_subcategory}' от пользователя {user_id}"
    )

    if "subcategories" not in state:
        logger.error("❌ В СОСТОЯНИИ НЕТ subcategories!")
        bot.send_message(
            message.chat.id,
            "❌ Ошибка: список подкатегорий потерян. Начните заново.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    subcategories = state.get("subcategories", [])

    if not subcategories:
        logger.error("❌ СПИСОК ПОДКАТЕГОРИЙ ПУСТ!")
        bot.send_message(
            message.chat.id,
            "❌ Ошибка: нет доступных подкатегорий. Начните заново.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    selected_sub = None

    for sub in subcategories:
        if sub["name"] == selected_subcategory:
            selected_sub = sub
            logger.info(f"✅ Найдено по точному совпадению: {sub['name']}")
            break

    if not selected_sub:
        clean_label = "".join(
            ch for ch in selected_subcategory if ch.isalnum() or ch.isspace()
        ).strip()
        logger.info(f"🔍 Ищем без эмодзи: '{clean_label}'")
        for sub in subcategories:
            clean_sub = "".join(
                ch for ch in sub["name"] if ch.isalnum() or ch.isspace()
            ).strip()
            if clean_sub == clean_label:
                selected_sub = sub
                logger.info(f"✅ Найдено без эмодзи: {sub['name']}")
                break

    if not selected_sub:
        logger.error(f"❌ ПОДКАТЕГОРИЯ НЕ НАЙДЕНА: '{selected_subcategory}'")
        available = [sub["name"] for sub in subcategories]
        logger.error(f"   ДОСТУПНО: {available}")
        bot.send_message(
            message.chat.id,
            f"❌ Подкатегория не найдена. Пожалуйста, выберите из списка.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    logger.info(
        f"✅ НАЙДЕНА ПОДКАТЕГОРИЯ: {selected_sub['name']} (id={selected_sub['id']})"
    )

    parent_name = state.get("selected_category_name", "Категория")
    state["category_id"] = selected_sub["id"]
    state["category_name"] = f"{parent_name} → {selected_sub['name']}"
    state["subcategory"] = selected_sub["name"]
    state["action"] = "add_transaction"
    if "waiting_for" in state:
        del state["waiting_for"]
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
    user_id = message.from_user.id
    logger.info(f"❌ Отмена операции от пользователя {user_id}")
    delete_user_state_db(user_id)
    bot.send_message(
        message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
    )


def handle_amount(message):
    user_id = message.from_user.id
    input_text = message.text.replace(",", ".")
    state = get_user_state_db(user_id)
    logger.info(f"💰 Ввод суммы: {input_text} от пользователя {user_id}")

    if not state or state.get("action") != "add_transaction":
        logger.warning(f"⚠️ Неверное состояние при вводе суммы: {state}")
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

    warnings = check_budget_limits_for_report(family_id)
    if warnings:
        response += "\n\n⚠️ **ПРЕВЫШЕНИЯ ЛИМИТОВ:**\n" + "\n".join(warnings)

    bot.send_message(
        message.chat.id,
        response,
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )


# ============================================
# ========== ОТМЕНА ОПЕРАЦИИ ============
# ============================================


@bot.message_handler(func=lambda message: message.text == "↩️ Отменить операцию")
def cancel_last_transaction(message):
    user_id = message.from_user.id
    logger.info(f"↩️ Отменить операцию от пользователя {user_id}")

    family_id = get_user_family_db(user_id)
    if not family_id:
        bot.send_message(
            message.chat.id, "Сначала войдите в семью!", reply_markup=get_auth_menu()
        )
        return

    delete_user_state_db(user_id)

    last_trans = get_last_user_transaction_db(family_id, user_id)
    if not last_trans:
        bot.send_message(
            message.chat.id,
            "❌ Вы еще не вносили никаких операций, отменять нечего! 🤷‍♂️",
            reply_markup=get_main_menu(),
        )
        return

    date_formatted = format_transaction_date(last_trans["date"])
    category_display = get_category_display_clean(last_trans["category_id"])

    message_text = (
        f"⚠️ **Вы уверены, что хотите удалить последнюю операцию?**\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 **Тип:** {last_trans['type']}\n"
        f"💰 **Сумма:** `{last_trans['amount']:.2f} руб.`\n"
        f"📅 **Дата:** {date_formatted}\n"
        f"🏷️ **Категория:** {category_display}\n"
        f"👤 **Кто:** {last_trans['user_name']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Выберите действие:"
    )

    save_user_state_db(
        user_id,
        {
            "action": "confirm_cancel",
            "transaction_id": last_trans["id"],
            "transaction_data": {
                "type": last_trans["type"],
                "amount": last_trans["amount"],
                "date": date_formatted,
                "category": category_display,
                "user": last_trans["user_name"],
            },
        },
    )

    bot.send_message(
        message.chat.id,
        message_text,
        parse_mode="Markdown",
        reply_markup=get_cancel_confirmation_menu(),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_confirm_"))
def handle_cancel_callback(call):
    user_id = call.from_user.id
    logger.info(f"🔍 Callback отмены: {call.data} от пользователя {user_id}")

    state = get_user_state_db(user_id)
    if state.get("action") != "confirm_cancel":
        logger.warning(f"⚠️ Пользователь {user_id} не в состоянии подтверждения")
        bot.answer_callback_query(call.id, "Нет активной операции для отмены")
        return

    try:
        if call.data == "cancel_confirm_no":
            logger.info(f"❌ Отмена операции отклонена пользователем {user_id}")
            delete_user_state_db(user_id)

            bot.edit_message_text(
                "✅ Операция сохранена. Возврат в главное меню.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )

            bot.send_message(
                call.message.chat.id, "Главное меню:", reply_markup=get_main_menu()
            )
            bot.answer_callback_query(call.id)
            return

        if call.data == "cancel_confirm_yes":
            transaction_id = state.get("transaction_id")

            if not transaction_id:
                logger.error(f"❌ Не найден ID транзакции для пользователя {user_id}")
                delete_user_state_db(user_id)
                bot.edit_message_text(
                    "❌ Ошибка: операция не найдена.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                )
                bot.send_message(
                    call.message.chat.id, "Главное меню:", reply_markup=get_main_menu()
                )
                bot.answer_callback_query(call.id, "Произошла ошибка")
                return

            try:
                success = delete_transaction_db(transaction_id)
                load_data_to_memory()

                if success:
                    logger.info(
                        f"✅ Транзакция {transaction_id} удалена пользователем {user_id}"
                    )
                    trans_data = state.get("transaction_data", {})
                    type_emoji = "📉" if trans_data.get("type") == "Расход" else "📈"

                    success_text = (
                        f"✅ **Операция успешно удалена!**\n\n"
                        f"{type_emoji} {trans_data.get('type', '')}\n"
                        f"💰 Сумма: `{trans_data.get('amount', 0):.2f} руб.`\n"
                        f"🏷️ Категория: {trans_data.get('category', '')}\n"
                        f"📅 Дата: {trans_data.get('date', '')}\n"
                    )

                    bot.edit_message_text(
                        success_text,
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        parse_mode="Markdown",
                    )
                else:
                    bot.edit_message_text(
                        "❌ Операция не найдена в базе данных.",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                    )

                delete_user_state_db(user_id)
                bot.send_message(
                    call.message.chat.id,
                    "Возвращаемся в главное меню:",
                    reply_markup=get_main_menu(),
                )

            except Exception as e:
                logger.error(f"❌ Ошибка при удалении транзакции: {e}", exc_info=True)
                bot.edit_message_text(
                    "❌ Произошла ошибка при удалении. Попробуйте позже.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                )
                bot.send_message(
                    call.message.chat.id, "Главное меню:", reply_markup=get_main_menu()
                )
                delete_user_state_db(user_id)

            bot.answer_callback_query(call.id)
            return

    except Exception as e:
        logger.error(
            f"❌ Критическая ошибка в handle_cancel_callback: {e}", exc_info=True
        )
        bot.answer_callback_query(call.id, "Произошла ошибка, попробуйте снова")
        delete_user_state_db(user_id)
        bot.send_message(
            call.message.chat.id,
            "Произошла ошибка. Возврат в главное меню.",
            reply_markup=get_main_menu(),
        )


# ============================================
# ========== ИСТОРИЯ И БАЛАНС ============
# ============================================


@bot.message_handler(func=lambda message: message.text == "📊 История и Баланс")
def show_balance_and_history(message):
    user_id = message.from_user.id
    logger.info(f"📊 История и Баланс от пользователя {user_id}")

    delete_user_state_db(user_id)

    family_id = get_user_family_db(user_id)
    if not family_id:
        bot.send_message(
            message.chat.id, "Сначала войдите в семью!", reply_markup=get_auth_menu()
        )
        return

    now = datetime.now()
    start_of_month = datetime(now.year, now.month, 1)
    seven_days_ago = now - timedelta(days=7)

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
    avg_income_per_day = total_income / days_in_month if days_in_month > 0 else 0

    response = "💰 **БАЛАНС СЕМЬИ**\n"
    response += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    response += f"📥 Доходы (месяц):   {total_income:>10.2f} руб.\n"
    response += f"📤 Расходы (месяц):  {total_expense:>10.2f} руб.\n"
    response += f"💰 Остаток:          {balance:>+10.2f} руб.\n"
    response += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    response += "📊 **СТАТИСТИКА ЗА МЕСЯЦ:**\n"
    response += f"├─ 📅 Дней: {days_in_month}\n"
    response += f"├─ 📊 Средние траты: {avg_spent_per_day:.2f} руб./день\n"
    response += f"└─ 📈 Средние доходы: {avg_income_per_day:.2f} руб./день\n\n"

    warnings = check_budget_limits_for_report(family_id)
    if warnings:
        response += "⚠️ **ПРЕВЫШЕНИЯ ЛИМИТОВ:**\n"
        response += "\n".join(warnings)
        response += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    else:
        limits = get_budget_limits_db(family_id)
        if limits:
            response += "✅ **Превышений лимитов нет** (все в норме)\n"
            response += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    response += "📋 **ПОСЛЕДНИЕ ОПЕРАЦИИ (7 дней):**\n\n"

    transactions = get_transactions_db(
        family_id, start_date=seven_days_ago, end_date=now
    )

    if not transactions:
        response += "❌ За последние 7 дней операций не было."
    else:
        grouped = {}
        for t in transactions:
            t_date = (
                t["date"].date()
                if isinstance(t["date"], datetime)
                else datetime.strptime(t["date"], "%Y-%m-%d %H:%M:%S.%f").date()
            )
            if t_date not in grouped:
                grouped[t_date] = []
            grouped[t_date].append(t)

        sorted_dates = sorted(grouped.keys(), reverse=True)

        for date_key in sorted_dates[:7]:
            day_trans = grouped[date_key]
            day_total = sum(
                t["amount"] if t["type"] == "Доход" else -t["amount"] for t in day_trans
            )

            response += f"📅 **{date_key.strftime('%d.%m')}**      Итого: {day_total:+.2f} руб.\n"

            for i, t in enumerate(day_trans):
                is_last = i == len(day_trans) - 1
                prefix = "└─ " if is_last else "├─ "

                category_display = get_category_display_clean(t["category_id"])
                sign = "+" if t["type"] == "Доход" else "-"
                time_str = (
                    t["date"].strftime("%H:%M")
                    if isinstance(t["date"], datetime)
                    else datetime.strptime(t["date"], "%Y-%m-%d %H:%M:%S.%f").strftime(
                        "%H:%M"
                    )
                )

                response += f"{prefix} {category_display:<20} {sign}{t['amount']:>8.2f}    {t['user_name']:<8} {time_str}\n"

            response += "\n"

    bot.send_message(message.chat.id, response, parse_mode="Markdown")


# ============================================
# ========== ОТЧЕТЫ ============
# ============================================


@bot.message_handler(func=lambda message: message.text == "📊 Отчеты")
def reports_handler(message):
    user_id = message.from_user.id
    logger.info(f"📊 Отчеты от пользователя {user_id}")

    delete_user_state_db(user_id)

    family_id = get_user_family_db(user_id)
    if not family_id:
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
    logger.info(f"📊 Выбран отчет: {message.text} от пользователя {user_id}")

    delete_user_state_db(user_id)
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
    logger.info(f"📅 Текущий месяц от пользователя {user_id}")

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
    logger.info(f"🗓️ Произвольный период от пользователя {user_id}")

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
    response += f"📅 {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')} ({days_in_period} дн.)\n"
    response += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    response += f"💰 **БАЛАНС:** `{balance:+.2f} руб.`\n"
    response += f"├─ 📥 Доходы:    {total_income:>10.2f} руб.\n"
    response += f"└─ 📤 Расходы:   {total_expense:>10.2f} руб.\n\n"

    avg_spent = total_expense / days_in_period if days_in_period > 0 else 0
    avg_income = total_income / days_in_period if days_in_period > 0 else 0

    response += "📈 **СТАТИСТИКА:**\n"
    response += f"├─ 📅 Дней в периоде: {days_in_period}\n"
    response += f"├─ 📊 Средние траты: {avg_spent:.2f} руб./день\n"
    response += f"└─ 📈 Средние доходы: {avg_income:.2f} руб./день\n\n"

    warnings = check_budget_limits_for_report(family_id)
    if warnings:
        response += "⚠️ **ПРЕВЫШЕНИЯ ЛИМИТОВ:**\n"
        response += "\n".join(warnings)
        response += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    else:
        limits = get_budget_limits_db(family_id)
        if limits:
            response += "✅ **Превышений лимитов нет** (все в норме)\n"
            response += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    response += "📊 **РАСХОДЫ ПО КАТЕГОРИЯМ:**\n"
    response += "┌────────────────────────────────┬──────────┬──────────┐\n"
    response += "│ Категория                      │ Сумма    │ %        │\n"
    response += "├────────────────────────────────┼──────────┼──────────┤\n"

    expense_trans = [t for t in transactions if t["type"] == "Расход"]

    if expense_trans:
        category_tree = {}
        for t in expense_trans:
            cat_id = t["category_id"]
            cat_info = get_category_by_id(cat_id)
            if not cat_info:
                continue

            parent_id = (
                cat_info["parent_id"] if cat_info["parent_id"] is not None else cat_id
            )

            if parent_id not in category_tree:
                category_tree[parent_id] = {
                    "id": parent_id,
                    "name": get_category_full_name(parent_id),
                    "total": 0,
                    "subcategories": {},
                }

            if cat_info["parent_id"] is not None:
                sub_id = cat_id
                if sub_id not in category_tree[parent_id]["subcategories"]:
                    category_tree[parent_id]["subcategories"][sub_id] = {
                        "id": sub_id,
                        "name": cat_info["name"],
                        "amount": 0,
                    }
                category_tree[parent_id]["subcategories"][sub_id]["amount"] += t[
                    "amount"
                ]
            else:
                category_tree[parent_id]["direct_amount"] = (
                    category_tree[parent_id].get("direct_amount", 0) + t["amount"]
                )

            category_tree[parent_id]["total"] += t["amount"]

        sorted_parents = sorted(
            category_tree.values(), key=lambda x: x["total"], reverse=True
        )

        for parent in sorted_parents:
            if parent["total"] == 0:
                continue

            parent_percent = (
                (parent["total"] / total_expense) * 100 if total_expense > 0 else 0
            )
            parent_display = get_category_display_clean(parent["id"])

            response += f"│ {parent_display:<30} │ {parent['total']:>8.2f} │ {create_percentage_bar(parent_percent)} {parent_percent:>3.0f}% │\n"

            sub_list = sorted(
                parent["subcategories"].values(),
                key=lambda x: x["amount"],
                reverse=True,
            )

            for i, sub in enumerate(sub_list):
                is_last_sub = i == len(sub_list) - 1
                sub_percent = (
                    (sub["amount"] / parent["total"]) * 100
                    if parent["total"] > 0
                    else 0
                )
                sub_display = get_category_display_clean(sub["id"])
                prefix = "  └─ " if is_last_sub else "  ├─ "

                response += f"│ {prefix}{sub_display:<27} │ {sub['amount']:>8.2f} │ {create_percentage_bar(sub_percent)} {sub_percent:>3.0f}% │\n"

    response += "└────────────────────────────────┴──────────┴──────────┘\n\n"

    expense_sorted = sorted(expense_trans, key=lambda x: x["amount"], reverse=True)
    top_5 = expense_sorted[:5]

    if top_5:
        response += "🏆 **ТОП-5 КРУПНЫХ ТРАТ:**\n"
        for i, exp in enumerate(top_5, 1):
            cat_display = get_category_display_clean(exp["category_id"])
            date_str = (
                exp["date"].strftime("%d.%m")
                if isinstance(exp["date"], datetime)
                else datetime.strptime(exp["date"], "%Y-%m-%d %H:%M:%S.%f").strftime(
                    "%d.%m"
                )
            )
            response += f"{i}. {cat_display:<25} {exp['amount']:>8.2f} руб.  {date_str}  {exp['user_name']}\n"
        response += "\n"

    response += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    response += "📈 **СРАВНЕНИЕ С ПРЕДЫДУЩИМ ПЕРИОДОМ:**\n"

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

        if prev_income > 0:
            change = ((total_income - prev_income) / prev_income) * 100
            arrow = "📈" if change > 0 else "📉" if change < 0 else "➡️"
            response += f"\n📥 Доходы: {total_income:.2f} → {prev_income:.2f} ({change:+.1f}%) {arrow}"
        if prev_expense > 0:
            change = ((total_expense - prev_expense) / prev_expense) * 100
            arrow = "📈" if change > 0 else "📉" if change < 0 else "➡️"
            response += f"\n📤 Расходы: {total_expense:.2f} → {prev_expense:.2f} ({change:+.1f}%) {arrow}"
        if prev_balance != 0:
            change = (
                ((balance - prev_balance) / abs(prev_balance)) * 100
                if prev_balance != 0
                else 0
            )
            arrow = "📈" if change > 0 else "📉" if change < 0 else "➡️"
            response += f"\n💰 Остаток: {balance:.2f} → {prev_balance:.2f} ({change:+.1f}%) {arrow}"

    bot.send_message(
        chat_id, response, parse_mode="Markdown", reply_markup=get_main_menu()
    )


# ============================================
# ========== БЮДЖЕТНЫЕ ЛИМИТЫ ============
# ============================================


@bot.message_handler(func=lambda message: message.text == "💰 Бюджетные лимиты")
def budget_limits_handler(message):
    user_id = message.from_user.id
    logger.info(f"💰 Бюджетные лимиты от пользователя {user_id}")

    delete_user_state_db(user_id)

    family_id = get_user_family_db(user_id)
    if not family_id:
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
def set_limit_start(message):
    user_id = message.from_user.id
    logger.info(f"📝 Установить лимит от пользователя {user_id}")

    family_id = clear_state_and_check_family(message)
    if not family_id:
        return

    parent_categories = get_categories_db(family_id, "expense", parent_id=None)

    if not parent_categories:
        bot.send_message(
            message.chat.id,
            "Нет категорий для установки лимита. Сначала создайте категорию расходов.",
            reply_markup=get_main_menu(),
        )
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for cat in parent_categories:
        display_name = get_category_display_name(cat["name"])
        markup.add(types.KeyboardButton(display_name))
    markup.add(types.KeyboardButton("❌ Отмена"))

    save_user_state_db(
        user_id,
        {
            "action": "set_limit",
            "stage": "selecting_parent",
            "parent_categories": parent_categories,
        },
    )

    bot.send_message(
        message.chat.id,
        "📌 **Выберите категорию для установки лимита:**\n\n"
        "Выберите родительскую категорию, затем укажете:\n"
        "• На всю категорию (с учётом всех подкатегорий)\n"
        "• Или на конкретную подкатегорию",
        reply_markup=markup,
    )


@bot.message_handler(
    func=lambda message: (
        message.text
        and get_user_state_db(message.from_user.id).get("action") == "set_limit"
        and get_user_state_db(message.from_user.id).get("stage") == "selecting_parent"
        and message.text != "❌ Отмена"
    )
)
def set_limit_parent_selected(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    selected_display = message.text.strip()
    logger.info(
        f"📌 Выбрана родительская категория: {selected_display} от пользователя {user_id}"
    )

    parent_categories = state.get("parent_categories", [])

    parent_category = None
    for cat in parent_categories:
        display_name = get_category_display_name(cat["name"])
        if display_name == selected_display:
            parent_category = cat
            break

    if not parent_category:
        _, clean_selected = extract_emoji_from_name(selected_display)
        for cat in parent_categories:
            _, clean_cat = extract_emoji_from_name(cat["name"])
            if clean_cat == clean_selected:
                parent_category = cat
                break

    if not parent_category:
        logger.error(f"❌ Родительская категория не найдена: {selected_display}")
        bot.send_message(
            message.chat.id,
            "❌ Категория не найдена. Попробуйте еще раз.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    logger.info(
        f"✅ Найдена родительская категория: {parent_category['name']} (id={parent_category['id']})"
    )

    family_id = get_user_family_db(user_id)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name FROM categories WHERE parent_id = ? AND family_id = ?",
            (parent_category["id"], family_id),
        )
        subcategories = [dict(row) for row in cursor.fetchall()]

    logger.info(f"📂 Найдено подкатегорий: {len(subcategories)}")

    if not subcategories:
        state["selected_category"] = {
            "id": parent_category["id"],
            "name": parent_category["name"],
            "is_parent": True,
        }
        state["stage"] = "entering_amount"
        state["action"] = "set_limit"
        save_user_state_db(user_id, state)

        display_name = get_category_display_name(parent_category["name"])
        msg = bot.send_message(
            message.chat.id,
            f"Введите лимит для категории {display_name} (в рублях):\n"
            f"Например: 15000",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        bot.register_next_step_handler(msg, set_limit_amount)
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)

    parent_display = get_parent_display_name(parent_category["name"])
    markup.add(types.KeyboardButton(parent_display))

    for sub in subcategories:
        display_name = get_category_display_name(sub["name"])
        markup.add(types.KeyboardButton(display_name))

    markup.add(types.KeyboardButton("❌ Отмена"))

    state["parent_category"] = parent_category
    state["subcategories"] = subcategories
    state["stage"] = "selecting_subcategory"
    state["action"] = "set_limit"
    save_user_state_db(user_id, state)

    bot.send_message(
        message.chat.id,
        f"📌 **Выберите, на что установить лимит для категории «{parent_category['name']}»:**\n\n"
        f"➡️ На всю категорию — лимит будет учитывать ВСЕ расходы по категории и её подкатегориям\n"
        f"📄 На конкретную подкатегорию — лимит только для неё",
        reply_markup=markup,
    )


@bot.message_handler(
    func=lambda message: (
        message.text
        and get_user_state_db(message.from_user.id).get("action") == "set_limit"
        and get_user_state_db(message.from_user.id).get("stage")
        == "selecting_subcategory"
        and message.text != "❌ Отмена"
    )
)
def set_limit_subcategory_selected(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    selected_text = message.text.strip()
    logger.info(f"📌 Выбрано: {selected_text} от пользователя {user_id}")

    parent_category = state.get("parent_category")
    subcategories = state.get("subcategories", [])

    selected_category = None

    parent_display = get_parent_display_name(parent_category["name"])
    if selected_text == parent_display:
        selected_category = {
            "id": parent_category["id"],
            "name": parent_category["name"],
            "is_parent": True,
        }
        logger.info(f"✅ Выбрана вся категория: {parent_category['name']}")
    else:
        for sub in subcategories:
            display_name = get_category_display_name(sub["name"])
            if display_name == selected_text:
                selected_category = {
                    "id": sub["id"],
                    "name": sub["name"],
                    "is_parent": False,
                    "parent_id": parent_category["id"],
                    "parent_name": parent_category["name"],
                }
                logger.info(f"✅ Выбрана подкатегория: {sub['name']}")
                break

        if not selected_category:
            _, clean_selected = extract_emoji_from_name(selected_text)
            for sub in subcategories:
                _, clean_sub = extract_emoji_from_name(sub["name"])
                if clean_sub == clean_selected:
                    selected_category = {
                        "id": sub["id"],
                        "name": sub["name"],
                        "is_parent": False,
                        "parent_id": parent_category["id"],
                        "parent_name": parent_category["name"],
                    }
                    logger.info(
                        f"✅ Найдена подкатегория по чистому имени: {sub['name']}"
                    )
                    break

    if not selected_category:
        logger.error(f"❌ Не удалось определить выбор: {selected_text}")
        bot.send_message(
            message.chat.id,
            "❌ Ошибка: не удалось определить выбор. Попробуйте еще раз.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    state["selected_category"] = selected_category
    state["stage"] = "entering_amount"
    state["action"] = "set_limit"
    save_user_state_db(user_id, state)

    if selected_category["is_parent"]:
        display_name = get_category_display_name(selected_category["name"])
        amount_prompt = (
            f"Введите лимит для категории {display_name} (включает все подкатегории):\n"
            f"Например: 15000"
        )
    else:
        display_name = get_category_display_name(selected_category["name"])
        parent_display = get_category_display_name(parent_category["name"])
        amount_prompt = (
            f"Введите лимит для подкатегории {display_name} (категория {parent_display}):\n"
            f"Например: 5000"
        )

    msg = bot.send_message(
        message.chat.id,
        amount_prompt,
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, set_limit_amount)


@bot.message_handler(
    func=lambda message: (
        message.text
        and get_user_state_db(message.from_user.id).get("action") == "set_limit"
        and get_user_state_db(message.from_user.id).get("stage")
        in ["selecting_parent", "selecting_subcategory"]
        and message.text == "❌ Отмена"
    )
)
def set_limit_cancel(message):
    user_id = message.from_user.id
    logger.info(f"❌ Отмена установки лимита от пользователя {user_id}")
    delete_user_state_db(user_id)
    bot.send_message(
        message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
    )


def set_limit_amount(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    logger.info(f"💰 Ввод лимита: {message.text} от пользователя {user_id}")

    if (
        not state
        or state.get("action") != "set_limit"
        or state.get("stage") != "entering_amount"
    ):
        bot.send_message(
            message.chat.id, "Ошибка. Начните заново.", reply_markup=get_main_menu()
        )
        delete_user_state_db(user_id)
        return

    try:
        limit = float(message.text.replace(",", "."))
        if limit <= 0:
            raise ValueError
    except ValueError:
        msg = bot.send_message(message.chat.id, "❌ Введите положительное число:")
        bot.register_next_step_handler(msg, set_limit_amount)
        return

    family_id = get_user_family_db(user_id)
    selected_category = state["selected_category"]
    category_id = selected_category["id"]
    category_name = selected_category["name"]
    is_parent = selected_category.get("is_parent", False)

    set_budget_limit_db(family_id, category_id, limit)
    load_data_to_memory()
    delete_user_state_db(user_id)

    display_name = get_category_display_name(category_name)
    if is_parent:
        subcats = get_categories_db(family_id, "expense", parent_id=category_id)
        subcat_names = ", ".join([sub["name"] for sub in subcats]) if subcats else "нет"
        success_msg = (
            f"✅ **Лимит установлен!**\n\n"
            f"📁 Категория: {display_name} (родительская)\n"
            f"💰 Лимит: {limit:.2f} руб.\n"
            f"📂 Включает подкатегории: {subcat_names}"
        )
    else:
        parent_name = selected_category.get("parent_name", "")
        parent_display = get_category_display_name(parent_name)
        success_msg = (
            f"✅ **Лимит установлен!**\n\n"
            f"📄 Подкатегория: {display_name}\n"
            f"📁 Родительская категория: {parent_display}\n"
            f"💰 Лимит: {limit:.2f} руб."
        )

    bot.send_message(
        message.chat.id,
        success_msg,
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )


@bot.message_handler(func=lambda message: message.text == "📊 Просмотреть лимиты")
def view_limits(message):
    user_id = message.from_user.id
    logger.info(f"📊 Просмотреть лимиты от пользователя {user_id}")

    family_id = clear_state_and_check_family(message)
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
    now = datetime.now()
    start_date = datetime(now.year, now.month, 1)
    if now.month == 12:
        end_date = datetime(now.year + 1, 1, 1) - timedelta(days=1)
    else:
        end_date = datetime(now.year, now.month + 1, 1) - timedelta(days=1)

    month_trans = get_transactions_db(
        family_id, start_date=start_date, end_date=end_date
    )

    for category_id, data in limits.items():
        category_name = data["name"]
        limit = data["limit"]

        category_ids = get_all_category_ids_for_limit(family_id, category_id)
        current_expense = sum(
            t["amount"]
            for t in month_trans
            if t["type"] == "Расход" and t["category_id"] in category_ids
        )
        percentage = (current_expense / limit) * 100 if limit > 0 else 0

        category_info = get_category_by_id(category_id)
        is_parent = category_info["parent_id"] is None if category_info else True

        display_name = get_category_display_name(category_name)

        if is_parent:
            subcats = get_categories_db(family_id, "expense", parent_id=category_id)
            subcat_names = (
                ", ".join([sub["name"] for sub in subcats]) if subcats else "нет"
            )
            response += f"📁 *{display_name}* (включает: {subcat_names})\n"
        else:
            parent = (
                get_category_by_id(category_info["parent_id"])
                if category_info
                else None
            )
            parent_name = parent["name"] if parent else ""
            parent_display = get_category_display_name(parent_name)
            response += f"📄 *{display_name}* (подкатегория {parent_display})\n"

        response += f"  💰 Лимит: {limit:.2f} руб.\n"
        response += f"  💸 Потрачено: {current_expense:.2f} руб. ({percentage:.1f}%)\n"

        if percentage >= 100:
            response += "  🔴 **ПРЕВЫШЕН**\n"
        elif percentage >= 80:
            response += "  🟡 **ПРЕДУПРЕЖДЕНИЕ**\n"
        else:
            response += "  🟢 Норма\n"
        response += "\n"

    bot.send_message(
        message.chat.id, response, parse_mode="Markdown", reply_markup=get_limits_menu()
    )


@bot.message_handler(func=lambda message: message.text == "🗑 Удалить лимит")
def delete_limit_start(message):
    user_id = message.from_user.id
    logger.info(f"🗑 Удалить лимит от пользователя {user_id}")

    family_id = clear_state_and_check_family(message)
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
        category_info = get_category_by_id(category_id)
        is_parent = category_info["parent_id"] is None if category_info else True
        display_name = get_category_display_name(data["name"])
        if is_parent:
            label = f"📁 {display_name}"
        else:
            label = f"📄 {display_name}"
        markup.add(types.KeyboardButton(label))
    markup.add(types.KeyboardButton("❌ Отмена"))

    save_user_state_db(user_id, {"action": "delete_limit", "limits": limits})
    bot.send_message(
        message.chat.id, "Выберите категорию для удаления лимита:", reply_markup=markup
    )


@bot.message_handler(
    func=lambda message: (
        message.text
        and get_user_state_db(message.from_user.id).get("action") == "delete_limit"
        and message.text != "❌ Отмена"
    )
)
def confirm_delete_limit(message):
    user_id = message.from_user.id
    selected_label = message.text
    selected_label = selected_label.replace("📁 ", "").replace("📄 ", "").strip()
    _, clean_name = extract_emoji_from_name(selected_label)

    logger.info(
        f"🗑 Подтверждение удаления лимита: {clean_name} от пользователя {user_id}"
    )

    limits = get_user_state_db(user_id).get("limits", {})
    category_id = None
    for cid, data in limits.items():
        _, clean_cat = extract_emoji_from_name(data["name"])
        if clean_cat == clean_name:
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
        f"✅ Лимит для категории удален.",
        reply_markup=get_main_menu(),
    )


@bot.message_handler(
    func=lambda message: (
        message.text
        and get_user_state_db(message.from_user.id).get("action") == "delete_limit"
        and message.text == "❌ Отмена"
    )
)
def delete_limit_cancel(message):
    user_id = message.from_user.id
    logger.info(f"❌ Отмена удаления лимита от пользователя {user_id}")
    delete_user_state_db(user_id)
    bot.send_message(
        message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
    )


# ============================================
# ========== НАПОМИНАНИЯ ============
# ============================================


@bot.message_handler(func=lambda message: message.text == "⏰ Напоминания о платежах")
def reminders_handler(message):
    user_id = message.from_user.id
    logger.info(f"⏰ Напоминания от пользователя {user_id}")

    delete_user_state_db(user_id)

    family_id = get_user_family_db(user_id)
    if not family_id:
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
    user_id = message.from_user.id
    logger.info(f"➕ Добавить напоминание от пользователя {user_id}")

    delete_user_state_db(user_id)

    family_id = get_user_family_db(user_id)
    if not family_id:
        bot.send_message(
            message.chat.id, "Сначала войдите в семью!", reply_markup=get_auth_menu()
        )
        return

    save_user_state_db(user_id, {"action": "add_reminder"})
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(
        types.KeyboardButton("💰 Расход"),
        types.KeyboardButton("📈 Доход"),
        types.KeyboardButton("❌ Отмена"),
    )
    bot.send_message(message.chat.id, "Выберите тип платежа:", reply_markup=markup)


@bot.message_handler(
    func=lambda message: (
        message.text in ["💰 Расход", "📈 Доход"]
        and get_user_state_db(message.from_user.id).get("action") == "add_reminder"
    )
)
def add_reminder_type(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    trans_type = "expense" if message.text == "💰 Расход" else "income"
    logger.info(f"📌 Выбран тип напоминания: {message.text} от пользователя {user_id}")

    state["reminder_type"] = trans_type
    state["action"] = "add_reminder"
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

    state["categories"] = categories
    state["action"] = "add_reminder_category"
    save_user_state_db(user_id, state)

    bot.send_message(
        message.chat.id, "Выберите категорию платежа:", reply_markup=markup
    )


@bot.message_handler(
    func=lambda message: (
        message.text
        and get_user_state_db(message.from_user.id).get("action")
        == "add_reminder_category"
        and message.text != "❌ Отмена"
    )
)
def add_reminder_category(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    selected_category = message.text.strip()
    logger.info(
        f"📌 Выбрана категория для напоминания: {selected_category} от пользователя {user_id}"
    )

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

    family_id = get_user_family_db(user_id)

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

    logger.info(
        f"📂 Найдено подкатегорий для '{category['name']}': {len(subcategories)}"
    )

    if subcategories:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add(types.KeyboardButton("➡️ Без подкатегории"))
        for sub in subcategories:
            markup.add(types.KeyboardButton(sub["name"]))
        markup.add(types.KeyboardButton("❌ Отмена"))

        state["selected_category_id"] = category["id"]
        state["selected_category_name"] = category["name"]
        state["subcategories"] = subcategories
        state["action"] = "add_reminder_subcategory"
        save_user_state_db(user_id, state)

        bot.send_message(
            message.chat.id,
            f"Вы выбрали «{category['name']}».\n\nВыберите подкатегорию или «Без подкатегории»:",
            reply_markup=markup,
        )
    else:
        state["reminder_category"] = category["name"]
        state["reminder_category_id"] = category["id"]
        state["action"] = "add_reminder"
        save_user_state_db(user_id, state)

        msg = bot.send_message(
            message.chat.id,
            "Введите название платежа (например: 'Квартплата', 'Netflix'):",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        bot.register_next_step_handler(msg, add_reminder_title)


@bot.message_handler(
    func=lambda message: (
        message.text
        and get_user_state_db(message.from_user.id).get("action")
        == "add_reminder_subcategory"
        and message.text in ["➡️ Без подкатегории", "❌ Отмена"]
    )
)
def add_reminder_subcategory_special(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    logger.info(
        f"🔍 СПЕЦИАЛЬНАЯ КНОПКА для подкатегории: {message.text} от пользователя {user_id}"
    )

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

        state["reminder_category"] = state["selected_category_name"]
        state["reminder_category_id"] = state["selected_category_id"]
        state["action"] = "add_reminder"
        save_user_state_db(user_id, state)

        msg = bot.send_message(
            message.chat.id,
            "Введите название платежа (например: 'Квартплата', 'Netflix'):",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        bot.register_next_step_handler(msg, add_reminder_title)


@bot.message_handler(
    func=lambda message: (
        message.text
        and get_user_state_db(message.from_user.id).get("action")
        == "add_reminder_subcategory"
        and message.text not in ["➡️ Без подкатегории", "❌ Отмена"]
    )
)
def add_reminder_subcategory(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    selected_subcategory = message.text
    logger.info(
        f"🔍 Выбрана подкатегория для напоминания: '{selected_subcategory}' от пользователя {user_id}"
    )

    if "subcategories" not in state:
        logger.error("❌ В СОСТОЯНИИ НЕТ subcategories!")
        bot.send_message(
            message.chat.id,
            "❌ Ошибка: список подкатегорий потерян. Начните заново.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    subcategories = state.get("subcategories", [])

    if not subcategories:
        logger.error("❌ СПИСОК ПОДКАТЕГОРИЙ ПУСТ!")
        bot.send_message(
            message.chat.id,
            "❌ Ошибка: нет доступных подкатегорий. Начните заново.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    selected_sub = None

    for sub in subcategories:
        if sub["name"] == selected_subcategory:
            selected_sub = sub
            logger.info(f"✅ Найдено по точному совпадению: {sub['name']}")
            break

    if not selected_sub:
        clean_label = "".join(
            ch for ch in selected_subcategory if ch.isalnum() or ch.isspace()
        ).strip()
        logger.info(f"🔍 Ищем без эмодзи: '{clean_label}'")
        for sub in subcategories:
            clean_sub = "".join(
                ch for ch in sub["name"] if ch.isalnum() or ch.isspace()
            ).strip()
            if clean_sub == clean_label:
                selected_sub = sub
                logger.info(f"✅ Найдено без эмодзи: {sub['name']}")
                break

    if not selected_sub:
        logger.error(f"❌ ПОДКАТЕГОРИЯ НЕ НАЙДЕНА: '{selected_subcategory}'")
        available = [sub["name"] for sub in subcategories]
        logger.error(f"   ДОСТУПНО: {available}")
        bot.send_message(
            message.chat.id,
            f"❌ Подкатегория не найдена. Пожалуйста, выберите из списка.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    logger.info(
        f"✅ НАЙДЕНА ПОДКАТЕГОРИЯ: {selected_sub['name']} (id={selected_sub['id']})"
    )

    parent_name = state.get("selected_category_name", "Категория")
    state["reminder_category"] = f"{parent_name} → {selected_sub['name']}"
    state["reminder_category_id"] = selected_sub["id"]
    state["action"] = "add_reminder"
    save_user_state_db(user_id, state)

    msg = bot.send_message(
        message.chat.id,
        "Введите название платежа (например: 'Квартплата', 'Netflix'):",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, add_reminder_title)


@bot.message_handler(
    func=lambda message: (
        message.text
        and get_user_state_db(message.from_user.id).get("action")
        == "add_reminder_category"
        and message.text == "❌ Отмена"
    )
)
def add_reminder_category_cancel(message):
    user_id = message.from_user.id
    logger.info(f"❌ Отмена добавления напоминания от пользователя {user_id}")
    delete_user_state_db(user_id)
    bot.send_message(
        message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
    )


def add_reminder_title(message):
    user_id = message.from_user.id

    if message.text == "❌ Отмена":
        logger.info(f"❌ Отмена добавления напоминания от пользователя {user_id}")
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
        )
        return

    state = get_user_state_db(user_id)
    if not state or state.get("action") != "add_reminder":
        logger.warning(f"⚠️ Неверное состояние при добавлении напоминания: {state}")
        bot.send_message(
            message.chat.id, "Ошибка. Начните заново.", reply_markup=get_main_menu()
        )
        delete_user_state_db(user_id)
        return

    state["reminder_title"] = message.text
    state["action"] = "add_reminder"
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
        delete_user_state_db(user_id)
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
    state["action"] = "add_reminder"
    save_user_state_db(user_id, state)

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(
        types.KeyboardButton("📅 Ежемесячно"),
        types.KeyboardButton("📆 Еженедельно"),
        types.KeyboardButton("❌ Отмена"),
    )
    bot.send_message(message.chat.id, "Выберите периодичность:", reply_markup=markup)


@bot.message_handler(
    func=lambda message: (
        message.text in ["📅 Ежемесячно", "📆 Еженедельно"]
        and get_user_state_db(message.from_user.id).get("action") == "add_reminder"
    )
)
def add_reminder_frequency(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    logger.info(f"📌 Выбрана периодичность: {message.text} от пользователя {user_id}")

    if not state or state.get("action") != "add_reminder":
        bot.send_message(
            message.chat.id, "Ошибка. Начните заново.", reply_markup=get_main_menu()
        )
        delete_user_state_db(user_id)
        return

    frequency = "monthly" if message.text == "📅 Ежемесячно" else "weekly"
    state["reminder_frequency"] = frequency
    state["action"] = "add_reminder"
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
        delete_user_state_db(user_id)
        return

    state["reminder_day"] = day
    state["action"] = "add_reminder"
    save_user_state_db(user_id, state)
    ask_notify_days(message)


@bot.message_handler(
    func=lambda message: (
        message.text
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
        delete_user_state_db(user_id)
        return

    state["reminder_day"] = days_map[message.text]
    state["action"] = "add_reminder"
    save_user_state_db(user_id, state)
    ask_notify_days(message)


def ask_notify_days(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    logger.info(f"🔔 Запрос дней напоминания от пользователя {user_id}")

    required_fields = [
        "reminder_type",
        "reminder_category",
        "reminder_title",
        "reminder_amount",
        "reminder_frequency",
        "reminder_day",
    ]
    missing_fields = [field for field in required_fields if field not in state]

    if missing_fields:
        logger.error(f"❌ Отсутствуют поля в состоянии: {missing_fields}")
        logger.error(f"📋 Текущее состояние: {state}")
        bot.send_message(
            message.chat.id,
            f"❌ Ошибка: потеряны данные напоминания. Начните заново.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    if not state or state.get("action") != "add_reminder":
        bot.send_message(
            message.chat.id,
            "Ошибка сессии. Начните добавление напоминания заново.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(
        types.KeyboardButton("1 день"),
        types.KeyboardButton("2 дня"),
        types.KeyboardButton("3 дня"),
    )
    markup.row(types.KeyboardButton("Не напоминать"), types.KeyboardButton("❌ Отмена"))

    state["waiting_for"] = "notify_days"
    state["action"] = "add_reminder"
    save_user_state_db(user_id, state)

    bot.send_message(
        message.chat.id, "За сколько дней напоминать?", reply_markup=markup
    )


@bot.message_handler(
    func=lambda message: (
        get_user_state_db(message.from_user.id).get("action") == "add_reminder"
        and get_user_state_db(message.from_user.id).get("waiting_for") == "notify_days"
        and message.text in ["1 день", "2 дня", "3 дня", "Не напоминать", "❌ Отмена"]
    )
)
def add_reminder_notify_days(message):
    user_id = message.from_user.id
    text = message.text
    logger.info(f"🔔 Выбор дней: {text} от пользователя {user_id}")

    if text == "❌ Отмена":
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
        )
        return

    days_map = {"1 день": [1], "2 дня": [2], "3 дня": [3], "Не напоминать": []}
    notify_days = days_map.get(text, [1])

    state = get_user_state_db(user_id)
    if not state or state.get("action") != "add_reminder":
        bot.send_message(
            message.chat.id, "Ошибка. Начните заново.", reply_markup=get_main_menu()
        )
        delete_user_state_db(user_id)
        return

    required_fields = [
        "reminder_type",
        "reminder_category",
        "reminder_title",
        "reminder_amount",
        "reminder_frequency",
        "reminder_day",
    ]
    missing_fields = [field for field in required_fields if field not in state]

    if missing_fields:
        logger.error(
            f"❌ Отсутствуют поля перед созданием напоминания: {missing_fields}"
        )
        logger.error(f"📋 Текущее состояние: {state}")
        bot.send_message(
            message.chat.id,
            f"❌ Ошибка: потеряны данные. Начните заново.",
            reply_markup=get_main_menu(),
        )
        delete_user_state_db(user_id)
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

    logger.info(f"📝 Создание напоминания: {reminder}")

    add_reminder_db(family_id, user_id, reminder)
    load_data_to_memory()
    delete_user_state_db(user_id)

    freq_text = "Ежемесячно" if reminder["frequency"] == "monthly" else "Еженедельно"
    notify_text = (
        ", ".join([f"{d} дн." for d in notify_days]) if notify_days else "Не напоминать"
    )

    type_emoji = "📉" if reminder["type"] == "expense" else "📈"

    confirmation_text = (
        f"✅ **Напоминание создано!**\n\n"
        f"{type_emoji} **Тип:** {'Расход' if reminder['type'] == 'expense' else 'Доход'}\n"
        f"📌 **Название:** {reminder['title']}\n"
        f"💰 **Сумма:** {reminder['amount']:.2f} руб.\n"
        f"🏷️ **Категория:** {reminder['category']}\n"
        f"📅 **Периодичность:** {freq_text}\n"
        f"📆 **Следующий платеж:** {next_date.strftime('%d.%m.%Y')}\n"
        f"🔔 **Напомнить за:** {notify_text}"
    )

    bot.send_message(
        message.chat.id,
        confirmation_text,
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
    )


@bot.message_handler(func=lambda message: message.text == "📋 Список напоминаний")
def list_reminders(message):
    user_id = message.from_user.id
    logger.info(f"📋 Список напоминаний от пользователя {user_id}")

    family_id = clear_state_and_check_family(message)
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
    logger.info(f"🗑 Удалить напоминание от пользователя {user_id}")

    family_id = clear_state_and_check_family(message)
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
    user_id = message.from_user.id

    if message.text == "❌ Отмена":
        logger.info(f"❌ Отмена удаления напоминания от пользователя {user_id}")
        bot.send_message(
            message.chat.id, "Удаление отменено.", reply_markup=get_main_menu()
        )
        return

    family_id = get_user_family_db(user_id)
    reminders = get_reminders_db(family_id)

    try:
        index = int(message.text.split(".")[0]) - 1
        logger.info(
            f"🗑 Удаление напоминания {reminders[index]['title']} от пользователя {user_id}"
        )
        delete_reminder_db(reminders[index]["id"])
        load_data_to_memory()
        bot.send_message(
            message.chat.id,
            f"✅ Напоминание '{reminders[index]['title']}' удалено.",
            reply_markup=get_main_menu(),
        )
    except Exception as e:
        logger.error(f"❌ Ошибка при удалении напоминания: {e}", exc_info=True)
        bot.send_message(
            message.chat.id, "Ошибка при удалении.", reply_markup=get_main_menu()
        )


@bot.message_handler(func=lambda message: message.text == "🔙 Назад в главное меню")
def back_to_main(message):
    user_id = message.from_user.id
    logger.info(f"🔙 Назад в главное меню от пользователя {user_id}")
    delete_user_state_db(user_id)
    bot.send_message(
        message.chat.id, "Возвращаемся в главное меню.", reply_markup=get_main_menu()
    )


# ============================================
# ========== ФОННЫЙ ПОТОК ДЛЯ ПРОВЕРКИ НАПОМИНАНИЙ ============
# ============================================


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
                        logger.error(f"❌ Ошибка отправки уведомления: {e}")
        except Exception as e:
            logger.error(f"❌ Ошибка в проверке напоминаний: {e}", exc_info=True)
        time.sleep(1800)


def run_reminder_checker():
    reminder_thread = threading.Thread(target=check_reminders, daemon=True)
    reminder_thread.start()
    logger.info("✅ Планировщик напоминаний запущен")


# ============================================
# ========== ЗАПУСК БОТА ============
# ============================================

if __name__ == "__main__":
    logger.info("🤖 Инициализация бота...")
    init_db()
    load_data_to_memory()

    try:
        bot.delete_webhook()
        logger.info("✅ Webhook удалён (если был)")
    except Exception as e:
        logger.warning(f"⚠️ Webhook удаление: {e}")

    logger.info("🤖 Бот запущен и готов к работе на Bothost!")
    logger.info("💰 Семейный бюджет бот активен")
    logger.info(f"💾 Данные сохраняются в {DB_NAME}")

    run_reminder_checker()

    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}", exc_info=True)
