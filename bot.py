import os
import telebot
import requests
import time
from collections import Counter
from slotmap import get_slot_combination
import datetime
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- PostgreSQL Setup (Supabase) ---
import psycopg2
from psycopg2 import sql, extras

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@host:5432/dbname")

# Подключение к БД
conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True  # Автокоммит для простых запросов

# --- Helper: Execute SQL safely ---
def execute_query(query, params=None, fetch=False):
    """Выполняет SQL-запрос и возвращает результат (если fetch=True)"""
    with conn.cursor() as cur:
        try:
            cur.execute(query, params)
            if fetch:
                return cur.fetchall()
        except Exception as e:
            print(f"[DB ERROR] {e}")
            conn.rollback()
            raise

# --- Helper: Get or Create User ---
def get_or_create_user(user_id, username=None):
    """Получает или создаёт пользователя в БД"""
    # Проверяем, существует ли пользователь
    query = "SELECT balance, username FROM users WHERE user_id = %s"
    result = execute_query(query, (user_id,), fetch=True)
    
    if result:
        return result[0]  # (balance, username)
    
    # Создаём нового пользователя
    query = """
        INSERT INTO users (user_id, username, balance)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE
        SET username = EXCLUDED.username
        RETURNING balance, username
    """
    execute_query(query, (user_id, username, 100))
    return (100, username)  # Default balance

# --- Helper: Update User Balance ---
def update_balance(user_id, new_balance):
    """Обновляет баланс пользователя"""
    query = "UPDATE users SET balance = %s WHERE user_id = %s"
    execute_query(query, (new_balance, user_id))

# --- Helper: Get Roll Cooldown ---
def get_roll_cooldown(chat_id):
    """Получает кулдаун для группы"""
    query = "SELECT cooldown_seconds FROM roll_cooldowns WHERE chat_id = %s"
    result = execute_query(query, (chat_id,), fetch=True)
    return result[0][0] if result else 60  # Default: 60 seconds

# --- Helper: Set Roll Cooldown ---
def set_roll_cooldown(chat_id, seconds):
    """Устанавливает кулдаун для группы"""
    query = """
        INSERT INTO roll_cooldowns (chat_id, cooldown_seconds)
        VALUES (%s, %s)
        ON CONFLICT (chat_id) DO UPDATE
        SET cooldown_seconds = EXCLUDED.cooldown_seconds
    """
    execute_query(query, (chat_id, seconds))

# --- Helper: Update Last Roll Time ---
def update_last_roll(user_id, timestamp):
    """Обновляет время последнего ролла"""
    query = "UPDATE users SET last_roll = %s WHERE user_id = %s"
    execute_query(query, (timestamp, user_id))

# --- Helper: Get Last Roll Time ---
def get_last_roll(user_id):
    """Получает время последнего ролла"""
    query = "SELECT last_roll FROM users WHERE user_id = %s"
    result = execute_query(query, (user_id,), fetch=True)
    return result[0][0] if result else 0

# --- Helper: Check Bankruptcy ---
def check_bankruptcy(user_id):
    """Проверяет, забирал ли пользователь банкротство сегодня"""
    query = "SELECT last_bankruptcy FROM users WHERE user_id = %s"
    result = execute_query(query, (user_id,), fetch=True)
    last_date = result[0][0] if result else None
    today = datetime.date.today().isoformat()
    return last_date == today

# --- Helper: Set Bankruptcy ---
def set_bankruptcy(user_id):
    """Устанавливает дату банкротства"""
    query = "UPDATE users SET last_bankruptcy = %s WHERE user_id = %s"
    execute_query(query, (datetime.date.today().isoformat(), user_id))

# --- Helper: Get User Balance ---
def get_balance(user_id):
    """Получает баланс пользователя"""
    query = "SELECT balance FROM users WHERE user_id = %s"
    result = execute_query(query, (user_id,), fetch=True)
    return result[0][0] if result else 100  # Default balance

# --- Helper: Is Group Admin (with cache) ---
def is_group_admin(chat_id, user_id):
    """Проверяет, является ли пользователь админом в группе (с кэшированием)"""
    # Проверяем кэш в БД
    query = "SELECT is_admin FROM admin_cache WHERE chat_id = %s AND user_id = %s"
    result = execute_query(query, (chat_id, user_id), fetch=True)
    
    if result:
        return result[0][0]
    
    # Если нет в кэше — проверяем через Telegram API
    try:
        member = bot.get_chat_member(chat_id, user_id)
        is_admin = member.status in ('administrator', 'creator')
    except Exception:
        is_admin = False
    
    # Сохраняем в кэш
    query = """
        INSERT INTO admin_cache (chat_id, user_id, is_admin)
        VALUES (%s, %s, %s)
        ON CONFLICT (chat_id, user_id) DO UPDATE
        SET is_admin = EXCLUDED.is_admin
    """
    execute_query(query, (chat_id, user_id, is_admin))
    return is_admin

# --- Helper: Register User in Group ---
def register_user_in_group(user_id, chat_id, username):
    """Регистрирует пользователя в группе (для tops)"""
    # Проверяем, есть ли пользователь
    get_or_create_user(user_id, username)
    
    # Обновляем last_roll (если нужно)
    query = """
        INSERT INTO users (user_id, username, last_roll)
        VALUES (%s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE
        SET username = EXCLUDED.username, last_roll = NOW()
    """
    execute_query(query, (user_id, username))

# --- Health Check Server ---
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
    def log_message(self, *args):
        pass

def _run_server():
    port = int(os.getenv("PORT", 8000))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()

threading.Thread(target=_run_server, daemon=True).start()

# --- Constants ---
token = os.getenv("BOT_TOKEN")
if not token:
    raise SystemExit("Нет BOT_TOKEN!")

bot = telebot.TeleBot(token, parse_mode='HTML')

MY_SYMBOLS = {
    'bar': '➖',
    'cherry': '🍒',
    'lemon': '🍋',
    'seven': 's'
}

START_BALANCE = 100
MIN_BET = 10
MULTIPLIERS = {0: -1, 1: 0.5, 2: 1, 3: 3, 4: 10}

DEVELOPER_ID = 5264334667
TESTERS = {5963485167, 6452920163}

# --- Command: /start ---
@bot.message_handler(commands=['start'])
def start(m):
    uid = m.from_user.id
    uname = (m.from_user.username or '').lower()
    
    # Создаём или получаем пользователя
    balance, _ = get_or_create_user(uid, uname)
    
    if balance == START_BALANCE:
        bot.reply_to(m, f"🎰 Банк открыт! У тебя {START_BALANCE} 🪙. Играй в группе: /slot")
    else:
        bot.reply_to(m, f"Банк уже есть: {balance} 🪙.")

# --- Command: /rolltime ---
@bot.message_handler(commands=['rolltime'])
def rolltime(m):
    uid = m.from_user.id
    cid = m.chat.id

    if m.chat.type not in ('group', 'supergroup'):
        bot.reply_to(m, "Эта команда работает только в группах 🤷")
        return
    if not is_group_admin(cid, uid):
        bot.reply_to(m, "Только администраторы группы могут задавать таймер ✋")
        return

    parts = m.text.split()
    if len(parts) != 3:
        bot.reply_to(m, "Формат: /rolltime <число> <с|мин>")
        return
    try:
        num = int(parts[1])
    except ValueError:
        bot.reply_to(m, "Число должно быть целым.")
        return
    if num <= 0:
        bot.reply_to(m, "Таймер должен быть больше нуля.")
        return

    unit = parts[2].lower()
    if unit in ('с', 'сек', 's', 'sec', 'seconds'):
        secs = num
    elif unit in ('мин', 'min', 'm', 'minutes'):
        secs = num * 60
    else:
        bot.reply_to(m, "Укажи с (секунды) или мин (минуты).")
        return

    set_roll_cooldown(cid, secs)
    bot.reply_to(m, f"Таймер для этой группы: {num} {unit} ⏱️")

# --- Command: /slot ---
@bot.message_handler(commands=['slot'])
def slot(m):
    uid = m.from_user.id
    cid = m.chat.id
    
    # Регистрируем пользователя в группе
    uname = (m.from_user.username or '').lower()
    register_user_in_group(uid, cid, uname)
    
    # Проверяем баланс
    balance = get_balance(uid)
    if balance is None:  # Пользователь не зарегистрирован
        bot.reply_to(m, "Ты ещё не в игре. Пропиши /start в ЛС бота, чтобы зарегистрироваться 🎰")
        return

    # Проверяем кулдаун
    cooldown = get_roll_cooldown(cid)
    last_roll = get_last_roll(uid)
    
    if last_roll:
        now = time.time()
        wait = cooldown - (now - last_roll.timestamp() if isinstance(last_roll, datetime.datetime) else now - last_roll)
        if wait > 0:
            bot.reply_to(m, f"Подожди ещё {int(wait)}с ⏳")
            return
    
    # Обновляем время последнего ролла
    update_last_roll(uid, datetime.datetime.now(datetime.timezone.utc))

    parts = m.text.split()
    if len(parts) < 2:
        bot.reply_to(m, "Напиши ставку: /slot 50 (минимум 10)")
        return
    try:
        bet = int(parts[1])
    except ValueError:
        bot.reply_to(m, "Ставка должна быть числом.")
        return

    if bet < MIN_BET:
        bot.reply_to(m, f"Минимальная ставка — {MIN_BET}.")
        return

    if bet % 5 != 0:
        bot.reply_to(m, "Ставка должна быть кратной 5 (например, 10, 15, 20).")
        return

    d = bot.send_dice(m.chat.id, emoji="🎰")
    value = d.dice.value
    line = get_slot_combination(value, symbols=MY_SYMBOLS)
    print(line)

    time.sleep(1)

    counts = Counter(line.split())
    counts_list = sorted(counts.values(), reverse=True)
    syms = line.split()

    if counts_list == [3]:
        if syms[0] == 's':
            jackpot = 4
        else:
            jackpot = 3
    elif counts_list == [2, 1]:
        if 's' in syms and syms.count('s') == 2:
            jackpot = 2
        else:
            jackpot = 1
    else:
        jackpot = 0

    change = bet * MULTIPLIERS[jackpot]

    # Обновляем баланс
    new_balance = balance + change
    if new_balance < 0:
        new_balance = 0
    update_balance(uid, new_balance)

    names = {0: "все разные 😞", 1: "повтор", 2: "суперповтор ⚡️",
             3: "ДЖЕКПОТ 🎉", 4: "СУПЕРДЖЕКПОТ 🚀"}
    bot.reply_to(m, f"{names[jackpot]}\n"
                    f"Ставка: {bet}\n"
                    f"{'+' if change >= 0 else ''}{change} 🪙\n"
                    f"Баланс: {new_balance}")

# --- Command: /bankrupt ---
@bot.message_handler(commands=['bankrupt'])
def bankrupt(m):
    uid = m.from_user.id
    
    balance = get_balance(uid)
    if balance is None:
        bot.reply_to(m, "Ты ещё не в игре. Пропиши /start в ЛС бота, чтобы зарегистрироваться 🎰")
        return

    if balance != 0:
        bot.reply_to(m, "Банкротство — только при нуле на балансе. У тебя есть 🪙, крути!")
        return

    if check_bankruptcy(uid):
        bot.reply_to(m, "Ты уже забирал сегодня. Приходи завтра 😉")
        return

    set_bankruptcy(uid)
    update_balance(uid, 50)

    bot.reply_to(m, "🧨 Банкрот! Держи 50 🪙. Не потеряй только сразу")

# --- Command: /balance ---
@bot.message_handler(commands=['balance'])
def balance(m):
    uid = m.from_user.id
    
    bal = get_balance(uid)
    if bal is None:
        bot.reply_to(m, "Ты ещё не в игре. Пропиши /start в ЛС бота, чтобы зарегистрироваться 🎰")
        return
    
    bot.reply_to(m, f"💰 Баланс: {bal} 🪙.")

# --- Command: /give ---
@bot.message_handler(commands=['give'])
def give(m):
    if m.chat.type == 'private':
        bot.reply_to(m, "💸 /give работает только в группе.")
        return

    uid = m.from_user.id
    parts = m.text.split()

    if len(parts) < 3 and not m.reply_to_message:
        bot.reply_to(m, "Формат: /give сумма username (или ответь на сообщение получателя)")
        return

    try:
        amount = float(parts[1])
    except (ValueError, IndexError):
        bot.reply_to(m, "Сумма должна быть числом.")
        return

    if amount < 10:
        bot.reply_to(m, "Минимум 10 очков.")
        return
    if amount % 5 != 0:
        bot.reply_to(m, "Сумма должна быть кратна 5.")
        return

    # Получаем target_id
    target_id = None
    if m.reply_to_message and m.reply_to_message.from_user:
        target_id = m.reply_to_message.from_user.id
    else:
        target_name = parts[2].lstrip('@').lower()
        query = "SELECT user_id FROM users WHERE LOWER(username) = LOWER(%s)"
        result = execute_query(query, (target_name,), fetch=True)
        target_id = result[0][0] if result else None
        
        if target_id is None:
            bot.reply_to(m, f"Не нашёл @{target_name} — пусть нажмёт /start.")
            return

    if target_id == uid:
        bot.reply_to(m, "Себе передать нельзя.")
        return
    
    target_balance = get_balance(target_id)
    if target_balance is None:
        bot.reply_to(m, "Получатель ещё не играл.")
        return

    # Проверяем баланс отправителя
    sender_balance = get_balance(uid)
    if sender_balance < amount:
        bot.reply_to(m, "Не хватает 🪙.")
        return

    # Обновляем балансы
    update_balance(uid, sender_balance - amount)
    update_balance(target_id, target_balance + amount)

    bot.reply_to(m, f"💸 {amount} 🪙 передано @{target_id}")

    # Уведомления
    try:
        bot.send_message(uid, f"✅ Ты передал {amount} 🪙.\n"
                              f"Остаток: {sender_balance - amount} 🪙.")
    except Exception:
        pass

    try:
        bot.send_message(target_id, f"🎁 Тебе передано {amount} 🪙!")
    except Exception:
        pass

# --- Command: /gift (DEV ONLY) ---
@bot.message_handler(commands=['gift'])
def gift(m):
    uid = m.from_user.id

    if uid != DEVELOPER_ID:
        bot.reply_to(m, "⛔️ Секретная команда. Доступ только для разработчика.")
        return

    parts = m.text.split()
    if len(parts) < 3:
        bot.reply_to(m, "Формат: /gift <очки> username")
        return

    try:
        amount = int(parts[1])
    except ValueError:
        bot.reply_to(m, "Сумма должна быть числом.")
        return

    if amount <= 0:
        bot.reply_to(m, "Сумма должна быть больше нуля.")
        return

    target_name = parts[2].lstrip('@').lower()
    query = "SELECT user_id FROM users WHERE LOWER(username) = LOWER(%s)"
    result = execute_query(query, (target_name,), fetch=True)
    
    if not result:
        bot.reply_to(m, f"Не нашёл @{target_name} — пусть нажмёт /start.")
        return
    
    target_id = result[0][0]
    target_balance = get_balance(target_id)
    update_balance(target_id, target_balance + amount)

    bot.reply_to(m, f"🎁 {amount} 🪙 выдано @{target_name} (Бактерия сегодня добрый!).")

# --- Command: /top ---
@bot.message_handler(commands=['top'])
def top(m):
    if m.chat.type not in ('group', 'supergroup'):
        bot.reply_to(m, "Команда работает только в группах 🤷")
        return

    cid = m.chat.id
    
    # Получаем пользователей в этой группе
    query = """
        SELECT user_id, balance, username
        FROM users
        WHERE user_id IN (
            SELECT DISTINCT user_id
            FROM users
            WHERE last_roll IS NOT NULL
        )
        ORDER BY balance DESC
        LIMIT 10
    """
    results = execute_query(query, fetch=True)
    
    if not results:
        bot.reply_to(m, "В этой группе ещё никто не играл 🤷")
        return

    out = []
    for i, (uid, bal, uname) in enumerate(results, 1):
        name = f'<a href="tg://user?id={uid}">{uname or str(uid)}</a>'
        
        if uid == DEVELOPER_ID:
            icon = '⚒️'
        elif uid in TESTERS:
            icon = '🔩'
        elif i == 1:
            icon = '👑'
        else:
            icon = f'{i}. '

        out.append(f"{icon} {name} - {bal} 🪙")
    
    bot.reply_to(m, "\n".join(out), parse_mode='HTML')

# --- Command: /credits ---
@bot.message_handler(commands=['credits'])
def credits(m):
    def tag(uid, fallback):
        query = "SELECT username FROM users WHERE user_id = %s"
        result = execute_query(query, (uid,), fetch=True)
        u = result[0][0] if result else fallback
        return f'<a href="tg://user?id={uid}">{u}</a>'

    testers = ' | '.join(
        tag(t, f"Тестер {i+1}") for i, t in enumerate(TESTERS)
    )
    bot.reply_to(m,
        f"👨‍💻 Разработчик: {tag(DEVELOPER_ID, 'Разработчик')}\n"
        f"🧪 Бета-тестеры: {testers}")

# --- Start Polling ---
bot.polling()
