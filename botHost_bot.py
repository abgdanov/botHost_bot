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

# --- КАТЕГОРИИ ---
EXPENSE_CATEGORIES = [
    "🛒 Продукты",
    "🏠 ЖКХ / Аренда",
    "🚗 Авто / Транспорт",
    "☕️ Кафе / Отдых",
    "💊 Здоровье",
    "💳 Кредиты",
    "📦 Другое",
]
INCOME_CATEGORIES = ["💰 Зарплата", "💼 Подработка", "🎁 Подарки"]

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
            "CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, family_id INTEGER, user_id INTEGER, user_name TEXT, type TEXT, category TEXT, amount REAL, date TIMESTAMP, FOREIGN KEY (family_id) REFERENCES families (family_id), FOREIGN KEY (user_id) REFERENCES users (user_id))"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, family_id INTEGER, user_id INTEGER, title TEXT, amount REAL, category TEXT, type TEXT, frequency TEXT, day INTEGER, next_due_date TIMESTAMP, notify_days_before TEXT, FOREIGN KEY (family_id) REFERENCES families (family_id), FOREIGN KEY (user_id) REFERENCES users (user_id))"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS budget_limits (family_id INTEGER, category TEXT, limit_amount REAL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (family_id, category), FOREIGN KEY (family_id) REFERENCES families (family_id))"
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
            "CREATE INDEX IF NOT EXISTS idx_reminders_family ON reminders(family_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(user_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_next_due ON reminders(next_due_date)"
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
    family_id, user_id, user_name, trans_type, category, amount, date_obj
):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO transactions (family_id, user_id, user_name, type, category, amount, date) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (family_id, user_id, user_name, trans_type, category, amount, date_obj),
        )


def get_transactions_db(family_id, user_id=None, start_date=None, end_date=None):
    with get_db() as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM transactions WHERE family_id = ?"
        params = [family_id]
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date DESC"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def get_last_user_transaction_db(family_id, user_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM transactions WHERE family_id = ? AND user_id = ? ORDER BY date DESC LIMIT 1",
            (family_id, user_id),
        )
        result = cursor.fetchone()
        return dict(result) if result else None


def delete_transaction_db(transaction_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))


def get_budget_limits_db(family_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT category, limit_amount FROM budget_limits WHERE family_id = ?",
            (family_id,),
        )
        return {row["category"]: row["limit_amount"] for row in cursor.fetchall()}


def set_budget_limit_db(family_id, category, limit):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO budget_limits (family_id, category, limit_amount) VALUES (?, ?, ?)",
            (family_id, category, limit),
        )


def delete_budget_limit_db(family_id, category):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM budget_limits WHERE family_id = ? AND category = ?",
            (family_id, category),
        )


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
        cursor.execute("SELECT family_id, category, limit_amount FROM budget_limits")
        for row in cursor.fetchall():
            family_id = row["family_id"]
            if family_id not in BUDGET_LIMITS:
                BUDGET_LIMITS[family_id] = {}
            BUDGET_LIMITS[family_id][row["category"]] = row["limit_amount"]
        cursor.execute("SELECT user_id, state_data FROM user_states")
        for row in cursor.fetchall():
            USER_STATES[row["user_id"]] = json.loads(row["state_data"])
    print(
        f"📦 Данные загружены: {len(USER_FAMILIES)} пользователей, {len(TRANSACTIONS)} транзакций"
    )


# --- КНОПКИ МЕНЮ ---
def get_main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn1 = types.KeyboardButton("📉 Добавить Расход")
    btn2 = types.KeyboardButton("📈 Добавить Доход")
    btn3 = types.KeyboardButton("📊 Отчеты")
    btn4 = types.KeyboardButton("📊 История и Баланс")
    btn5 = types.KeyboardButton("💰 Бюджетные лимиты")
    btn6 = types.KeyboardButton("⏰ Напоминания о платежах")
    btn7 = types.KeyboardButton("↩️ Отменить операцию")
    markup.add(btn1, btn2)
    markup.add(btn3, btn4)
    markup.add(btn5, btn6)
    markup.add(btn7)
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


@bot.message_handler(
    func=lambda message: message.text in ["📉 Добавить Расход", "📈 Добавить Доход"]
)
def handle_menu(message):
    user_id = message.from_user.id
    if not get_user_family_db(user_id):
        bot.send_message(
            message.chat.id, "Сначала войдите в семью!", reply_markup=get_auth_menu()
        )
        return
    trans_type = "expense" if message.text == "📉 Добавить Расход" else "income"
    save_user_state_db(user_id, {"action": "add_transaction", "type": trans_type})
    categories = EXPENSE_CATEGORIES if trans_type == "expense" else INCOME_CATEGORIES
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for cat in categories:
        markup.add(types.KeyboardButton(cat))
    markup.add(types.KeyboardButton("❌ Отмена"))
    action_text = "расхода" if trans_type == "expense" else "дохода"
    bot.send_message(
        message.chat.id, f"Выберите категорию для {action_text}:", reply_markup=markup
    )


@bot.message_handler(func=lambda message: message.text == "❌ Отмена")
def cancel_operation(message):
    delete_user_state_db(message.from_user.id)
    bot.send_message(
        message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
    )


@bot.message_handler(
    func=lambda message: (
        message.text in EXPENSE_CATEGORIES or message.text in INCOME_CATEGORIES
    )
    and message.from_user.id in USER_STATES
    and USER_STATES[message.from_user.id].get("action") == "add_transaction"
)
def handle_category(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    state["category"] = message.text
    save_user_state_db(user_id, state)
    msg = bot.send_message(
        message.chat.id,
        f"Вы выбрали: *{message.text}*.\nВведите сумму цифрами:",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, handle_amount)


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
    trans_type, category, family_id, user_name = (
        state["type"],
        state["category"],
        get_user_family_db(user_id),
        message.from_user.first_name,
    )
    trans_type_text = "Расход" if trans_type == "expense" else "Доход"
    add_transaction_db(
        family_id, user_id, user_name, trans_type_text, category, amount, datetime.now()
    )
    delete_user_state_db(user_id)
    load_data_to_memory()
    bot.send_message(
        message.chat.id,
        f"✅ Записано!\n• {trans_type_text}: {amount:.2f} руб. ({category})",
        reply_markup=get_main_menu(),
    )


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
    if last_trans:
        delete_transaction_db(last_trans["id"])
        load_data_to_memory()
        bot.send_message(
            message.chat.id,
            f"↩️ **Последняя операция успешно отменена!**\n\nУдалено: {last_trans['type']} на сумму `{last_trans['amount']:.2f} руб.` ({last_trans['category']})",
            parse_mode="Markdown",
            reply_markup=get_main_menu(),
        )
    else:
        bot.send_message(
            message.chat.id,
            "Вы еще не вносили никаких операций, отменять нечего! 🤷‍♂️",
            reply_markup=get_main_menu(),
        )


@bot.message_handler(func=lambda message: message.text == "📊 История и Баланс")
def show_balance_and_history(message):
    user_id = message.from_user.id
    family_id = get_user_family_db(user_id)
    if not family_id:
        bot.send_message(
            message.chat.id, "Сначала войдите в семью!", reply_markup=get_auth_menu()
        )
        return
    transactions = get_transactions_db(family_id)
    total_income = sum(t["amount"] for t in transactions if t["type"] == "Доход")
    total_expense = sum(t["amount"] for t in transactions if t["type"] == "Расход")
    current_balance = total_income - total_expense
    response = f"💰 **Общий баланс семьи:** `{current_balance:.2f} руб.`\n(📥 Всего доходов: {total_income:.2f} | 📤 Всего расходов: {total_expense:.2f})\n\n"
    if not transactions:
        response += "История операций пока пуста."
    else:
        response += "📋 **Последние операции:**\n"
        for i, t in enumerate(transactions[:10], 1):
            sign = "-" if t["type"] == "Расход" else "+"
            date_str = (
                t["date"].strftime("%d.%m %H:%M")
                if isinstance(t["date"], datetime)
                else datetime.strptime(t["date"], "%Y-%m-%d %H:%M:%S.%f").strftime(
                    "%d.%m %H:%M"
                )
            )
            response += f"{i}. {t['user_name']} ({date_str}): {sign}{t['amount']:.2f} р. — {t['category']}\n"
    bot.send_message(message.chat.id, response, parse_mode="Markdown")


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
    if report_type == "family":
        period_trans = get_transactions_db(
            family_id, start_date=start_date, end_date=end_date
        )
        title = "🏠 **Семейный отчет**"
    else:
        period_trans = get_transactions_db(
            family_id, user_id=user_id, start_date=start_date, end_date=end_date
        )
        title = "👤 **Личный отчет**"
    if not period_trans:
        bot.send_message(
            chat_id,
            f"{title}\n\n📅 {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}\n\n❌ За этот период нет операций.",
            parse_mode="Markdown",
            reply_markup=get_main_menu(),
        )
        return
    period_income = sum(t["amount"] for t in period_trans if t["type"] == "Доход")
    period_expense = sum(t["amount"] for t in period_trans if t["type"] == "Расход")
    period_days = (end_date - start_date).days + 1
    response = f"{title}\n📅 {start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')} ({period_days} дн.)\n\n📥 Доходы: {period_income:.2f} руб.\n📤 Расходы: {period_expense:.2f} руб.\n💰 Остаток: {period_income - period_expense:.2f} руб.\n\n📊 Средние траты в день: {period_expense / period_days:.2f} руб."
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
    save_user_state_db(user_id, {"action": "set_limit"})
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for cat in EXPENSE_CATEGORIES:
        markup.add(types.KeyboardButton(cat))
    markup.add(types.KeyboardButton("❌ Отмена"))
    bot.send_message(
        message.chat.id, "Выберите категорию для установки лимита:", reply_markup=markup
    )


@bot.message_handler(
    func=lambda message: message.text in EXPENSE_CATEGORIES
    and get_user_state_db(message.from_user.id).get("action") == "set_limit"
)
def set_limit_amount(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    if not state or state.get("action") != "set_limit":
        bot.send_message(
            message.chat.id, "Начните заново через меню.", reply_markup=get_main_menu()
        )
        return
    state["limit_category"] = message.text
    save_user_state_db(user_id, state)
    msg = bot.send_message(
        message.chat.id,
        f"Введите лимит для категории *{message.text}* (в рублях):\nНапример: 15000",
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
    category, family_id = state["limit_category"], get_user_family_db(user_id)
    set_budget_limit_db(family_id, category, limit)
    load_data_to_memory()
    delete_user_state_db(user_id)
    bot.send_message(
        message.chat.id,
        f"✅ **Лимит установлен!**\n\n📌 Категория: {category}\n💰 Лимит: {limit:.2f} руб.",
        parse_mode="Markdown",
        reply_markup=get_main_menu(),
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
    response = "📊 **Бюджетные лимиты на текущий месяц:**\n\n"
    for category, limit in limits.items():
        response += f"• *{category}*: {limit:.2f} руб.\n"
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
    for cat in limits.keys():
        markup.add(types.KeyboardButton(cat))
    markup.add(types.KeyboardButton("❌ Отмена"))
    msg = bot.send_message(
        message.chat.id, "Выберите категорию для удаления лимита:", reply_markup=markup
    )
    bot.register_next_step_handler(msg, confirm_delete_limit)


def confirm_delete_limit(message):
    if message.text == "❌ Отмена":
        bot.send_message(
            message.chat.id, "Удаление отменено.", reply_markup=get_main_menu()
        )
        return
    category = message.text
    family_id = get_user_family_db(message.from_user.id)
    delete_budget_limit_db(family_id, category)
    load_data_to_memory()
    bot.send_message(
        message.chat.id,
        f"✅ Лимит для категории '{category}' удален.",
        reply_markup=get_main_menu(),
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
    categories = EXPENSE_CATEGORIES if trans_type == "expense" else INCOME_CATEGORIES
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for cat in categories:
        markup.add(types.KeyboardButton(cat))
    markup.add(types.KeyboardButton("❌ Отмена"))
    bot.send_message(
        message.chat.id, "Выберите категорию платежа:", reply_markup=markup
    )


@bot.message_handler(
    func=lambda message: (
        message.text in EXPENSE_CATEGORIES or message.text in INCOME_CATEGORIES
    )
    and get_user_state_db(message.from_user.id).get("action") == "add_reminder"
)
def add_reminder_category(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
    state["reminder_category"] = message.text
    save_user_state_db(user_id, state)
    msg = bot.send_message(
        message.chat.id,
        "Введите название платежа (например: 'Квартплата', 'Netflix'):",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, add_reminder_title)


def add_reminder_title(message):
    user_id = message.from_user.id
    if message.text == "❌ Отмена":
        delete_user_state_db(user_id)
        bot.send_message(
            message.chat.id, "Операция отменена.", reply_markup=get_main_menu()
        )
        return
    state = get_user_state_db(user_id)
    if not state:
        return
    state["reminder_title"] = message.text
    save_user_state_db(user_id, state)
    msg = bot.send_message(message.chat.id, "Введите сумму платежа (цифрами):")
    bot.register_next_step_handler(msg, add_reminder_amount)


def add_reminder_amount(message):
    user_id = message.from_user.id
    state = get_user_state_db(user_id)
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
    days_map = {
        "Понедельник": 0,
        "Вторник": 1,
        "Среда": 2,
        "Четверг": 3,
        "Пятница": 4,
        "Суббота": 5,
        "Воскресенье": 6,
    }
    state = get_user_state_db(message.from_user.id)
    state["reminder_day"] = days_map[message.text]
    save_user_state_db(message.from_user.id, state)
    ask_notify_days(message.chat.id, message.from_user.id)


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

    # Удаляем webhook на случай, если был активен
    try:
        bot.delete_webhook()
        print("✅ Webhook удалён (если был)")
    except:
        pass

    print("🤖 Бот запущен и готов к работе на Bothost!")
    print("💰 Семейный бюджет бот активен")
    print(f"💾 Данные сохраняются в {DB_NAME}")

    # Запускаем проверку напоминаний в фоне
    run_reminder_checker()
    print("✅ Планировщик напоминаний запущен")

    # Запускаем бота
    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен пользователем")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
