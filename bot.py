import os
import telebot
token = os.getenv("BOT_TOKEN")
if not token:
    raise SystemExit("Нет BOT_TOKEN!")
bot = telebot.TeleBot(token, parse_mode='HTML')
import requests
import time
from collections import Counter
from slotmap import get_slot_combination
import datetime
import json
import threading
lock = threading.Lock()

BANK_FILE = 'bank.json'
ROLL_FILE = 'roll_config.json'

bank = {}
USED_IN = {}   # user_id → chat_id где играл

if os.path.exists(BANK_FILE):
    with open(BANK_FILE, 'r', encoding='utf-8') as f:
        bank = json.load(f)
    bank = {int(k): v for k, v in bank.items()}

ROLL = {'cooldowns': {}, 'last_roll': {}}
if os.path.exists(ROLL_FILE):
    with open(ROLL_FILE, 'r', encoding='utf-8') as f:
        ROLL.update(json.load(f))
        ROLL.setdefault('cooldowns', {})
        ROLL.setdefault('last_roll', {})
        ROLL['cooldowns'] = {int(k): v for k, v in ROLL['cooldowns'].items()}
        ROLL['last_roll'] = {int(k): v for k, v in ROLL['last_roll'].items()}

def save_roll():
    with open(ROLL_FILE, 'w', encoding='utf-8') as f:
        json.dump(ROLL, f, ensure_ascii=False, indent=2)

MY_SYMBOLS = {
    'bar': '➖',
    'cherry': '🍒',
    'lemon': '🍋',
    'seven': 's'
}
users_unames = {}

START_BALANCE = 100
MIN_BET = 10
MULTIPLIERS = {0: -1, 1: 0.5, 2: 1, 3: 3, 4: 10}

def save_bank():
    with open(BANK_FILE, 'w', encoding='utf-8') as f:
        json.dump(bank, f)

bankrupt_log = {}

def get_balance(user_id):
    return bank.get(user_id, START_BALANCE)

ADMIN_CACHE = {}

def is_group_admin(chat_id, user_id):
    key = (chat_id, user_id)
    if key in ADMIN_CACHE:
        return ADMIN_CACHE[key]
    try:
        member = bot.get_chat_member(chat_id, user_id)
        ok = member.status in ('administrator', 'creator')
    except Exception:
        ok = False
    ADMIN_CACHE[key] = ok
    return ok

# ── Общая функция регистрации (уровень файла) ─────────
def _try_register(m):
    global users_unames
    uid = m.from_user.id
    cid = m.chat.id
    uname = (m.from_user.username or '').lower()
    with lock:
        users_unames[uid] = uname or (m.from_user.first_name or f"id{uid}")
        if USED_IN.get(uid) != cid:
            USED_IN[uid] = cid
            save_bank()

@bot.message_handler(commands=['start'])
def start(m):
    global users_unames
    uid = m.from_user.id
    uname = (m.from_user.username or '').lower()
    with lock:
        if uid not in bank:
            bank[uid] = START_BALANCE
            save_bank()
            bot.reply_to(m, f"🎰 Банк открыт! У тебя {START_BALANCE} 🪙. Играй в группе: /slot")
        else:
            bot.reply_to(m, f"Банк уже есть: {bank[uid]} 🪙.")
        users_unames[uid] = uname

# Timer
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

    with lock:
        ROLL['cooldowns'][cid] = secs
        save_roll()
    bot.reply_to(m, f"Таймер для этой группы: {num} {unit} ⏱️")

# Slot
@bot.message_handler(commands=['slot'])
def slot(m):
    _try_register(m)
    uid = m.from_user.id

    if uid not in bank:
        bot.reply_to(m, "Ты ещё не в игре. Пропиши /start в ЛС бота, чтобы зарегистрироваться 🎰")
        return

    cid = m.chat.id
    if cid in ROLL['cooldowns']:
        with lock:
            now = time.time()
            last = ROLL['last_roll'].get(uid, 0)
            wait = ROLL['cooldowns'][cid] - (now - last)
            if wait > 0:
                bot.reply_to(m, f"Подожди ещё {int(wait)}с ⏳")
                return
            ROLL['last_roll'][uid] = now
            save_roll()

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

    with lock:
        new_balance = bank.get(uid, 0) + change
        if new_balance < 0:
            new_balance = 0
        bank[uid] = new_balance
        save_bank()

    names = {0: "все разные 😞", 1: "повтор", 2: "суперповтор ⚡️",
             3: "ДЖЕКПОТ 🎉", 4: "СУПЕРДЖЕКПОТ 🚀"}
    bot.reply_to(m, f"{names[jackpot]}\n"
                    f"Ставка: {bet}\n"
                    f"{'+' if change >= 0 else ''}{change} 🪙\n"
                    f"Баланс: {new_balance}")

# Банкрот
@bot.message_handler(commands=['bankrupt'])
def bankrupt(m):
    uid = m.from_user.id

    if uid not in bank:
        bot.reply_to(m, "Ты ещё не в игре. Пропиши /start в ЛС бота, чтобы зарегистрироваться 🎰")
        return

    with lock:
        if bank[uid] != 0:
            bot.reply_to(m, "Банкротство — только при нуле на балансе. У тебя есть 🪙, крути!")
            return

        today = datetime.date.today().isoformat()
        last = bankrupt_log.get(uid)

        if last == today:
            bot.reply_to(m, "Ты уже забирал сегодня. Приходи завтра 😉")
            return

        bankrupt_log[uid] = today
        bank[uid] = 50
        save_bank()

    bot.reply_to(m, "🧨 Банкрот! Держи 50 🪙. Не потеряй только сразу")

# BALANCE
@bot.message_handler(commands=['balance'])
def balance(m):
    uid = m.from_user.id

    if uid not in bank:
        bot.reply_to(m, "Ты ещё не в игре. Пропиши /start в ЛС бота, чтобы зарегистрироваться 🎰")
        return

    bot.reply_to(m, f"💰 Баланс: {bank[uid]} 🪙.")

#Команда give
@bot.message_handler(commands=['give'])
def give(m):
    if m.chat.type == 'private':
        bot.reply_to(m, "💸 /give работает только в группе.")
        return

    uid = m.from_user.id
    parts = m.text.split()

    # 1) Сначала сумма
    if len(parts) < 3 and not m.reply_to_message:
        bot.reply_to(m, "Формат: /give сумма username (или ответь на сообщение получателя)")
        return

    try:
        amount = float(parts[1])
    except (ValueError, IndexError):
        bot.reply_to(m, "Сумма должна быть числом.")
        return

    # 2) Лимиты — числовые проверки можно вне лока
    if amount < 10:
        bot.reply_to(m, "Минимум 10 очков.")
        return
    if amount % 5 != 0:
        bot.reply_to(m, "Сумма должна быть кратна 5.")
        return

    # 3) Получатель: из ответа ИЛИ из username
    target_id = None
    if m.reply_to_message and m.reply_to_message.from_user:
        target_id = m.reply_to_message.from_user.id
    else:
        target_name = parts[2].lstrip('@').lower()
        target_id = next((id_ for id_, un in users_unames.items() if un == target_name), None)
        if target_id is None:
            bot.reply_to(m, f"Не нашёл @{target_name} — пусть нажмёт /start.")
            return

    if target_id == uid:
        bot.reply_to(m, "Себе передать нельзя.")
        return
    if target_id not in bank:
        bot.reply_to(m, "Получатель ещё не играл.")
        return

    # ВСЯ работа с балансом — под одним локом
    with lock:
        if amount > bank.get(uid, 0):
            bot.reply_to(m, "Не хватает 🪙.")
            return
        bank[uid] = bank.get(uid, 0) - amount
        bank[target_id] = bank.get(target_id, 0) + amount
        save_bank()

    bot.reply_to(m, f"💸 {amount} 🪙 передано @{target_id}")

    # уведомление отправителю
    try:
        bot.send_message(uid, f"✅ Ты передал {amount} 🪙.\n"
                              f"Остаток: {bank[uid]} 🪙.")
    except Exception:
        pass

    # уведомление получателю
    try:
        bot.send_message(target_id, f"🎁 Тебе передали {amount} 🪙!")
    except Exception:
        pass

# СЕКРЕТНАЯ команда разработчика
DEVELOPER_ID = 5264334667

@bot.message_handler(commands=['gift'])
def gift(m):
    uid = m.from_user.id

    # проверка id
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

    # получатель из username
    target_name = parts[2].lstrip('@').lower()
    target_id = next((id_ for id_, un in users_unames.items() if un == target_name), None)
    if target_id is None:
        bot.reply_to(m, f"Не нашёл @{target_name} — пусть нажмёт /start.")
        return

    with lock:
        bank[target_id] = bank.get(target_id, 0) + amount
        save_bank()

    bot.reply_to(m, f"🎁 {amount} 🪙 выдано @{target_name} (Бактерия сегодня добрый!).")

# ── ТОП ──────────────────────────────────────────────
DEV_ID = 5264334667
TESTERS = {5963485167, 6452920163}

@bot.message_handler(commands=['top'])
def top(m):
    if m.chat.type not in ('group', 'supergroup'):
        bot.reply_to(m, "Команда работает только в группах 🤷")
        return
    _try_register(m)
    cid = m.chat.id

    with lock:
        members = [u for u, g in USED_IN.items() if g == cid]
        top = sorted(members, key=lambda u: bank.get(u, 0), reverse=True)[:10]

    if not top:
        bot.reply_to(m, "В этой группе ещё никто не играл 🤷")
        return

    out = []
    for i, uid in enumerate(top, 1):
        name = f'<a href="tg://user?id={uid}">{users_unames.get(uid, str(uid))}</a>'
        bal = bank.get(uid, 0)

        if uid == DEV_ID:
            icon = '⚒️'
        elif uid in TESTERS:
            icon = '🔩'
        elif i == 1:
            icon = '👑'
        else:
            icon = f'{i}. '

        out.append(f"{icon} {name} - {bal} 🪙")
    bot.reply_to(m, "\n".join(out), parse_mode='HTML')

@bot.message_handler(commands=['credits'])
def credits(m):
    def tag(uid, fallback):
        u = users_unames.get(str(uid)) or users_unames.get(uid) or f"id{uid}"
        return f'<a href="tg://user?id={uid}">{u}</a>'

    testers = ' | '.join(
        tag(t, f"Тестер {i+1}") for i, t in enumerate(TESTERS)
    )
    bot.reply_to(m,
        f"👨‍💻 Разработчик: {tag(DEV_ID, 'Разработчик')}\n"
        f"🧪 Бета-тестеры: {testers}")


bot.polling()