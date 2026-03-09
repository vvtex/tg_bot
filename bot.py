import asyncio
import logging
from datetime import datetime, timedelta, date
from typing import Union

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
import sqlite3
import os

# ========== Configuration ==========
BOT_TOKEN = "YOUR_BOT_TOKEN"           # Замените на токен вашего бота
ADMIN_ID = 123456789                    # Ваш Telegram ID (владелец)
DATABASE = "barbershop.db"

# ========== Logging ==========
logging.basicConfig(level=logging.INFO)

# ========== Database setup ==========
def init_db():
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    # Таблица пользователей
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            phone TEXT,
            role TEXT DEFAULT 'visitor',
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Таблица услуг
    cur.execute('''
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            duration INTEGER DEFAULT 60,
            price INTEGER
        )
    ''')
    # Таблица записей (добавлено поле reminded)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            service_id INTEGER,
            appointment_date DATE,
            appointment_time TIME,
            status TEXT DEFAULT 'pending',
            reminded INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(service_id) REFERENCES services(id)
        )
    ''')
    # Таблица свободных слотов
    cur.execute('''
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_date DATE,
            slot_time TIME,
            is_available INTEGER DEFAULT 1
        )
    ''')
    # Добавим услуги по умолчанию
    cur.execute("SELECT COUNT(*) FROM services")
    if cur.fetchone()[0] == 0:
        services = [
            ("Мужская стрижка", 30, 800),
            ("Женская стрижка", 60, 1500),
            ("Стрижка машинкой", 20, 500),
            ("Укладка", 30, 600),
            ("Окрашивание", 120, 3000)
        ]
        cur.executemany("INSERT INTO services (name, duration, price) VALUES (?,?,?)", services)
    conn.commit()
    conn.close()

# Функции для работы с пользователями
def register_user(user_id, username, full_name):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?,?,?)",
                (user_id, username, full_name))
    if user_id == ADMIN_ID:
        cur.execute("UPDATE users SET role='owner' WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def get_user_role(user_id):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE user_id=?", (user_id,))
    result = cur.fetchone()
    conn.close()
    return result[0] if result else 'visitor'

def set_user_role(user_id, role):
    """Устанавливает роль пользователя (используется только владельцем)"""
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("UPDATE users SET role=? WHERE user_id=?", (role, user_id))
    conn.commit()
    conn.close()

def get_all_visitors():
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE role='visitor'")
    result = [row[0] for row in cur.fetchall()]
    conn.close()
    return result

def get_all_moderators():
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, full_name FROM users WHERE role='moderator'")
    mods = cur.fetchall()
    conn.close()
    return mods

# Функции для услуг
def get_services():
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT id, name, duration, price FROM services")
    services = cur.fetchall()
    conn.close()
    return services

def get_service(service_id):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT id, name, duration, price FROM services WHERE id=?", (service_id,))
    service = cur.fetchone()
    conn.close()
    return service

# Функции для слотов
def generate_slots(days_ahead=7):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    today = date.today()
    cur.execute("DELETE FROM slots WHERE slot_date < ?", (today.isoformat(),))
    start_hour = 9
    end_hour = 19
    for day_offset in range(days_ahead):
        current_date = today + timedelta(days=day_offset)
        date_str = current_date.isoformat()
        cur.execute("SELECT COUNT(*) FROM slots WHERE slot_date=?", (date_str,))
        if cur.fetchone()[0] == 0:
            for hour in range(start_hour, end_hour):
                time_str = f"{hour:02d}:00"
                cur.execute("INSERT INTO slots (slot_date, slot_time, is_available) VALUES (?,?,1)",
                            (date_str, time_str))
    conn.commit()
    conn.close()

def get_available_slots_for_date(date_str):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT slot_time FROM slots WHERE slot_date=? AND is_available=1 ORDER BY slot_time", (date_str,))
    slots = [row[0] for row in cur.fetchall()]
    conn.close()
    return slots

def book_slot(date_str, time_str):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("UPDATE slots SET is_available=0 WHERE slot_date=? AND slot_time=?", (date_str, time_str))
    conn.commit()
    conn.close()

def release_slot(date_str, time_str):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("UPDATE slots SET is_available=1 WHERE slot_date=? AND slot_time=?", (date_str, time_str))
    conn.commit()
    conn.close()

# Функции для записей
def create_appointment(user_id, service_id, date_str, time_str):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO appointments (user_id, service_id, appointment_date, appointment_time, status)
        VALUES (?,?,?,?, 'pending')
    ''', (user_id, service_id, date_str, time_str))
    appointment_id = cur.lastrowid
    conn.commit()
    conn.close()
    book_slot(date_str, time_str)
    return appointment_id

def get_user_appointments(user_id):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute('''
        SELECT a.id, s.name, a.appointment_date, a.appointment_time, a.status
        FROM appointments a
        JOIN services s ON a.service_id = s.id
        WHERE a.user_id=?
        ORDER BY a.appointment_date, a.appointment_time
    ''', (user_id,))
    apps = cur.fetchall()
    conn.close()
    return apps

def get_all_appointments():
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute('''
        SELECT a.id, u.full_name, u.username, s.name, a.appointment_date, a.appointment_time, a.status
        FROM appointments a
        JOIN users u ON a.user_id = u.user_id
        JOIN services s ON a.service_id = s.id
        ORDER BY a.appointment_date DESC, a.appointment_time DESC
    ''')
    apps = cur.fetchall()
    conn.close()
    return apps

def update_appointment_status(appointment_id, status):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("UPDATE appointments SET status=? WHERE id=?", (status, appointment_id))
    if status == 'cancelled':
        cur.execute("SELECT appointment_date, appointment_time FROM appointments WHERE id=?", (appointment_id,))
        row = cur.fetchone()
        if row:
            release_slot(row[0], row[1])
    conn.commit()
    conn.close()

# Функция для получения предстоящих подтверждённых записей, которые ещё не получили уведомление
def get_upcoming_confirmed_appointments():
    """Возвращает список записей (id, user_id, service_name, date, time), которые произойдут через 60±1 минуту"""
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    now = datetime.now()
    # Время через час (с запасом в 2 минуты, чтобы не пропустить из-за задержек)
    target_time = now + timedelta(minutes=60)
    # Ищем записи на сегодня, время которых совпадает с target_time с точностью до минуты
    # (упрощённо: appointment_date = today, appointment_time = target_time.strftime("%H:%M"))
    today_str = now.date().isoformat()
    target_time_str = target_time.strftime("%H:%M")
    cur.execute('''
        SELECT a.id, a.user_id, s.name, a.appointment_date, a.appointment_time
        FROM appointments a
        JOIN services s ON a.service_id = s.id
        WHERE a.status='confirmed' 
          AND a.reminded=0
          AND a.appointment_date = ?
          AND a.appointment_time = ?
    ''', (today_str, target_time_str))
    rows = cur.fetchall()
    conn.close()
    return rows

def mark_appointment_reminded(appointment_id):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("UPDATE appointments SET reminded=1 WHERE id=?", (appointment_id,))
    conn.commit()
    conn.close()

# ========== FSM States ==========
class AppointmentFSM(StatesGroup):
    choosing_service = State()
    choosing_date = State()
    choosing_time = State()
    confirming = State()

class BroadcastFSM(StatesGroup):
    waiting_for_message = State()

# ========== Проверки ролей ==========
def is_owner(user_id: int) -> bool:
    return get_user_role(user_id) == 'owner'

def is_moderator_or_owner(user_id: int) -> bool:
    role = get_user_role(user_id)
    return role in ('moderator', 'owner')

# ========== Keyboards ==========
def main_menu_keyboard(role):
    builder = ReplyKeyboardBuilder()
    if role == 'visitor':
        builder.add(KeyboardButton(text="📅 Записаться"))
        builder.add(KeyboardButton(text="📋 Мои записи"))
        builder.add(KeyboardButton(text="💇 Услуги и цены"))
        builder.add(KeyboardButton(text="📍 Контакты"))
        builder.add(KeyboardButton(text="🔥 Акции"))
    elif role == 'moderator':
        builder.add(KeyboardButton(text="📅 Записи на сегодня"))
        builder.add(KeyboardButton(text="📋 Все записи"))
        builder.add(KeyboardButton(text="✅ Подтвердить запись"))
        builder.add(KeyboardButton(text="❌ Отменить запись"))
        builder.add(KeyboardButton(text="➕ Добавить слот"))
        builder.add(KeyboardButton(text="📢 Рассылка"))
        builder.add(KeyboardButton(text="📋 Мои записи"))
    elif role == 'owner':
        builder.add(KeyboardButton(text="📅 Записи на сегодня"))
        builder.add(KeyboardButton(text="📋 Все записи"))
        builder.add(KeyboardButton(text="✅ Подтвердить запись"))
        builder.add(KeyboardButton(text="❌ Отменить запись"))
        builder.add(KeyboardButton(text="➕ Добавить слот"))
        builder.add(KeyboardButton(text="📢 Рассылка"))
        builder.add(KeyboardButton(text="👥 Управление модераторами"))
        builder.add(KeyboardButton(text="📊 Статистика"))
        builder.add(KeyboardButton(text="📋 Мои записи"))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

def cancel_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="❌ Отмена"))
    return builder.as_markup(resize_keyboard=True)

def services_inline_keyboard():
    services = get_services()
    builder = InlineKeyboardBuilder()
    for s in services:
        builder.button(text=f"{s[1]} - {s[3]} руб.", callback_data=f"service_{s[0]}")
    builder.adjust(1)
    return builder.as_markup()

def dates_inline_keyboard():
    today = date.today()
    builder = InlineKeyboardBuilder()
    for i in range(7):
        d = today + timedelta(days=i)
        label = d.strftime("%d.%m.%Y")
        callback = f"date_{d.isoformat()}"
        builder.button(text=label, callback_data=callback)
    builder.adjust(2)
    return builder.as_markup()

def times_inline_keyboard(date_str):
    slots = get_available_slots_for_date(date_str)
    builder = InlineKeyboardBuilder()
    for t in slots:
        builder.button(text=t, callback_data=f"time_{t}")
    builder.adjust(3)
    return builder.as_markup()

def confirm_inline_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data="confirm_yes")
    builder.button(text="❌ Отменить", callback_data="confirm_no")
    return builder.as_markup()

# ========== Handlers ==========
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    register_user(user.id, user.username, user.full_name)
    role = get_user_role(user.id)
    welcome_text = (
        f"👋 Добро пожаловать в парикмахерскую 'Стиль', {user.full_name}!\n\n"
        "Мы предлагаем профессиональные услуги по стрижке и укладке.\n"
        "Запишитесь сейчас и получите скидку 10% на первое посещение! 🎁"
    )
    await message.answer(welcome_text, reply_markup=main_menu_keyboard(role))

@dp.message(F.text == "📅 Записаться")
async def book_appointment(message: Message, state: FSMContext):
    user_id = message.from_user.id
    role = get_user_role(user_id)
    if role not in ['visitor', 'moderator', 'owner']:
        await message.answer("У вас нет прав для записи.")
        return
    await state.set_state(AppointmentFSM.choosing_service)
    await message.answer("Выберите услугу:", reply_markup=services_inline_keyboard())

@dp.callback_query(StateFilter(AppointmentFSM.choosing_service), F.data.startswith("service_"))
async def service_chosen(callback: CallbackQuery, state: FSMContext):
    service_id = int(callback.data.split("_")[1])
    await state.update_data(service_id=service_id)
    await callback.message.edit_text("Теперь выберите дату:")
    await state.set_state(AppointmentFSM.choosing_date)
    await callback.message.answer("Выберите дату:", reply_markup=dates_inline_keyboard())
    await callback.answer()

@dp.callback_query(StateFilter(AppointmentFSM.choosing_date), F.data.startswith("date_"))
async def date_chosen(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data.split("_")[1]
    await state.update_data(date=date_str)
    slots = get_available_slots_for_date(date_str)
    if not slots:
        await callback.message.edit_text("К сожалению, на эту дату нет свободных слотов. Выберите другую дату.")
        await callback.message.answer("Выберите дату:", reply_markup=dates_inline_keyboard())
        await callback.answer()
        return
    await callback.message.edit_text(f"Выбрана дата {date_str}. Теперь выберите время:")
    await state.set_state(AppointmentFSM.choosing_time)
    await callback.message.answer("Выберите время:", reply_markup=times_inline_keyboard(date_str))
    await callback.answer()

@dp.callback_query(StateFilter(AppointmentFSM.choosing_time), F.data.startswith("time_"))
async def time_chosen(callback: CallbackQuery, state: FSMContext):
    time_str = callback.data.split("_")[1]
    await state.update_data(time=time_str)
    data = await state.get_data()
    service = get_service(data['service_id'])
    text = (f"📌 Подтвердите запись:\n"
            f"Услуга: {service[1]}\n"
            f"Цена: {service[3]} руб.\n"
            f"Дата: {data['date']}\n"
            f"Время: {time_str}\n\n"
            f"Всё верно?")
    await callback.message.edit_text(text)
    await state.set_state(AppointmentFSM.confirming)
    await callback.message.answer("Подтвердите действие:", reply_markup=confirm_inline_keyboard())
    await callback.answer()

@dp.callback_query(StateFilter(AppointmentFSM.confirming), F.data == "confirm_yes")
async def confirm_yes(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    service_id = data['service_id']
    date_str = data['date']
    time_str = data['time']
    appointment_id = create_appointment(user_id, service_id, date_str, time_str)
    await callback.message.edit_text("✅ Запись подтверждена! Мы ждём вас.")
    await state.clear()
    await callback.message.answer("Хотите получать напоминания о записи и персональные скидки? Подпишитесь на рассылку (кнопка 'Рассылка' в меню).")
    await callback.answer()

@dp.callback_query(StateFilter(AppointmentFSM.confirming), F.data == "confirm_no")
async def confirm_no(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Запись отменена. Можете начать заново.")
    await state.clear()
    await callback.answer()

@dp.message(F.text == "📋 Мои записи")
async def my_appointments(message: Message):
    user_id = message.from_user.id
    apps = get_user_appointments(user_id)
    if not apps:
        await message.answer("У вас пока нет записей.")
        return
    text = "Ваши записи:\n"
    for app in apps:
        status_emoji = {'pending':'🕒','confirmed':'✅','cancelled':'❌','done':'✔️'}.get(app[4], '❓')
        text += f"{status_emoji} {app[1]} - {app[2]} в {app[3]} ({app[4]})\n"
    await message.answer(text)

@dp.message(F.text == "💇 Услуги и цены")
async def show_services(message: Message):
    services = get_services()
    text = "Наши услуги:\n"
    for s in services:
        text += f"• {s[1]} - {s[3]} руб. ({s[2]} мин.)\n"
    await message.answer(text)

@dp.message(F.text == "📍 Контакты")
async def show_contacts(message: Message):
    text = ("📍 Наш адрес: ул. Примерная, д. 1\n"
            "📞 Телефон: +7 (123) 456-78-90\n"
            "🕒 Часы работы: ежедневно с 9:00 до 20:00\n"
            "💬 Мы в Instagram: @barbershop_style")
    await message.answer(text)

@dp.message(F.text == "🔥 Акции")
async def show_promos(message: Message):
    text = ("🔥 Специальные предложения:\n"
            "• Скидка 10% на первое посещение\n"
            "• Приведи друга - получи скидку 15%\n"
            "• Стрижка + укладка = 2000 руб. вместо 2300\n\n"
            "Подпишитесь на рассылку, чтобы не пропустить новые акции!")
    await message.answer(text)

# --- Модератор и владелец ---
@dp.message(F.text == "📅 Записи на сегодня")
async def today_appointments(message: Message):
    if not is_moderator_or_owner(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    today_str = date.today().isoformat()
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute('''
        SELECT a.id, u.full_name, s.name, a.appointment_time, a.status
        FROM appointments a
        JOIN users u ON a.user_id = u.user_id
        JOIN services s ON a.service_id = s.id
        WHERE a.appointment_date=?
        ORDER BY a.appointment_time
    ''', (today_str,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await message.answer("На сегодня записей нет.")
        return
    text = "📅 Записи на сегодня:\n"
    for r in rows:
        status_emoji = {'pending':'🕒','confirmed':'✅','cancelled':'❌','done':'✔️'}.get(r[4], '❓')
        text += f"{r[0]}. {r[1]} - {r[2]} в {r[3]} {status_emoji}\n"
    await message.answer(text)

@dp.message(F.text == "📋 Все записи")
async def all_appointments(message: Message):
    if not is_moderator_or_owner(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    apps = get_all_appointments()
    if not apps:
        await message.answer("Записей нет.")
        return
    text = "Все записи:\n"
    for a in apps:
        status_emoji = {'pending':'🕒','confirmed':'✅','cancelled':'❌','done':'✔️'}.get(a[6], '❓')
        text += f"{a[0]}. {a[1]} (@{a[2]}) - {a[3]} {a[4]} {a[5]} {status_emoji}\n"
        if len(text) > 3000:
            await message.answer(text)
            text = ""
    if text:
        await message.answer(text)

@dp.message(F.text == "✅ Подтвердить запись")
async def confirm_appointment_prompt(message: Message, state: FSMContext):
    if not is_moderator_or_owner(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await message.answer("Введите ID записи для подтверждения:")
    await state.set_state("waiting_confirm_id")

@dp.message(StateFilter("waiting_confirm_id"))
async def confirm_appointment_by_id(message: Message, state: FSMContext):
    if not is_moderator_or_owner(message.from_user.id):
        await state.clear()
        return
    try:
        app_id = int(message.text)
    except ValueError:
        await message.answer("Введите число.")
        return
    update_appointment_status(app_id, 'confirmed')
    await message.answer(f"Запись {app_id} подтверждена.")
    await state.clear()

@dp.message(F.text == "❌ Отменить запись")
async def cancel_appointment_prompt(message: Message, state: FSMContext):
    if not is_moderator_or_owner(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await message.answer("Введите ID записи для отмены:")
    await state.set_state("waiting_cancel_id")

@dp.message(StateFilter("waiting_cancel_id"))
async def cancel_appointment_by_id(message: Message, state: FSMContext):
    if not is_moderator_or_owner(message.from_user.id):
        await state.clear()
        return
    try:
        app_id = int(message.text)
    except ValueError:
        await message.answer("Введите число.")
        return
    update_appointment_status(app_id, 'cancelled')
    await message.answer(f"Запись {app_id} отменена.")
    await state.clear()

@dp.message(F.text == "➕ Добавить слот")
async def add_slot_prompt(message: Message, state: FSMContext):
    if not is_moderator_or_owner(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await message.answer("Введите дату и время нового слота в формате ГГГГ-ММ-ДД ЧЧ:ММ (например, 2025-03-15 10:00):")
    await state.set_state("waiting_slot_data")

@dp.message(StateFilter("waiting_slot_data"))
async def add_slot(message: Message, state: FSMContext):
    if not is_moderator_or_owner(message.from_user.id):
        await state.clear()
        return
    try:
        date_str, time_str = message.text.strip().split()
        datetime.strptime(date_str, "%Y-%m-%d")
        datetime.strptime(time_str, "%H:%M")
    except Exception:
        await message.answer("Неверный формат. Попробуйте снова.")
        return
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT is_available FROM slots WHERE slot_date=? AND slot_time=?", (date_str, time_str))
    row = cur.fetchone()
    if row:
        if row[0] == 1:
            await message.answer("Этот слот уже свободен.")
        else:
            await message.answer("Этот слот занят. Сначала отмените запись.")
    else:
        cur.execute("INSERT INTO slots (slot_date, slot_time, is_available) VALUES (?,?,1)", (date_str, time_str))
        conn.commit()
        await message.answer(f"Слот {date_str} {time_str} добавлен как свободный.")
    conn.close()
    await state.clear()

@dp.message(F.text == "📢 Рассылка")
async def broadcast_prompt(message: Message, state: FSMContext):
    if not is_moderator_or_owner(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    await message.answer("Введите сообщение для рассылки всем посетителям:")
    await state.set_state(BroadcastFSM.waiting_for_message)

@dp.message(BroadcastFSM.waiting_for_message)
async def broadcast_message(message: Message, state: FSMContext):
    if not is_moderator_or_owner(message.from_user.id):
        await state.clear()
        return
    text = message.text
    visitors = get_all_visitors()
    success = 0
    fail = 0
    for uid in visitors:
        try:
            await bot.send_message(uid, f"📢 Рассылка:\n\n{text}")
            success += 1
        except Exception:
            fail += 1
    await message.answer(f"Рассылка завершена. Успешно: {success}, ошибок: {fail}")
    await state.clear()

# ========== Управление модераторами (только для владельца) ==========
@dp.message(F.text == "👥 Управление модераторами")
async def manage_moderators(message: Message):
    if not is_owner(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    mods = get_all_moderators()
    text = "👥 Текущие модераторы:\n"
    if mods:
        for m in mods:
            text += f"• {m[2]} (@{m[1]}) — ID: {m[0]}\n"
    else:
        text += "Список пуст.\n"
    text += "\nЧтобы добавить модератора, используйте команду:\n/add_moderator <ID>\nЧтобы удалить:\n/remove_moderator <ID>"
    await message.answer(text)

@dp.message(Command("add_moderator"))
async def add_moderator(message: Message):
    """Добавляет модератора (только владелец)"""
    if not is_owner(message.from_user.id):
        await message.answer("⛔ Только владелец может назначать модераторов.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /add_moderator user_id")
        return
    try:
        user_id = int(args[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO users (user_id, role) VALUES (?, 'moderator')", (user_id,))
        conn.commit()
        conn.close()
        await message.answer(f"✅ Пользователь {user_id} добавлен в базу и назначен модератором.")
    else:
        if row[0] == 'moderator':
            await message.answer(f"ℹ️ Пользователь {user_id} уже является модератором.")
        else:
            cur.execute("UPDATE users SET role='moderator' WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()
            await message.answer(f"✅ Пользователь {user_id} теперь модератор.")
    try:
        await bot.send_message(user_id, "🎉 Вам назначена роль модератора в боте парикмахерской. Теперь вам доступны дополнительные функции.")
    except:
        pass

@dp.message(Command("remove_moderator"))
async def remove_moderator(message: Message):
    """Снимает права модератора (только владелец)"""
    if not is_owner(message.from_user.id):
        await message.answer("⛔ Только владелец может удалять модераторов.")
        return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /remove_moderator user_id")
        return
    try:
        user_id = int(args[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("UPDATE users SET role='visitor' WHERE user_id=? AND role='moderator'", (user_id,))
    if cur.rowcount == 0:
        await message.answer("Пользователь не является модератором или не найден.")
    else:
        conn.commit()
        await message.answer(f"✅ У пользователя {user_id} отозваны права модератора.")
        try:
            await bot.send_message(user_id, "Ваши права модератора в боте парикмахерской были отозваны.")
        except:
            pass
    conn.close()

@dp.message(F.text == "📊 Статистика")
async def show_stats(message: Message):
    if not is_owner(message.from_user.id):
        await message.answer("Нет доступа.")
        return
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM appointments")
    total_apps = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM appointments WHERE status='pending'")
    pending = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM appointments WHERE status='confirmed'")
    confirmed = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM appointments WHERE status='cancelled'")
    cancelled = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM appointments WHERE status='done'")
    done = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM appointments WHERE appointment_date=?", (date.today().isoformat(),))
    today_apps = cur.fetchone()[0]
    conn.close()
    text = (f"📊 Статистика:\n"
            f"Всего пользователей: {total_users}\n"
            f"Всего записей: {total_apps}\n"
            f" - Ожидают: {pending}\n"
            f" - Подтверждено: {confirmed}\n"
            f" - Отменено: {cancelled}\n"
            f" - Выполнено: {done}\n"
            f"Записей на сегодня: {today_apps}")
    await message.answer(text)

@dp.message(F.text == "❌ Отмена")
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    role = get_user_role(message.from_user.id)
    await message.answer("Действие отменено.", reply_markup=main_menu_keyboard(role))

@dp.message()
async def unknown_message(message: Message):
    await message.answer("Извините, я не понимаю. Используйте кнопки меню.")

# ========== Background task for reminders ==========
async def reminder_scheduler():
    """Проверяет каждую минуту предстоящие записи и отправляет напоминания за час."""
    while True:
        try:
            appointments = get_upcoming_confirmed_appointments()
            for app_id, user_id, service_name, app_date, app_time in appointments:
                try:
                    await bot.send_message(
                        user_id,
                        f"⏰ Напоминание: через 1 час у вас запись на услугу «{service_name}»\n"
                        f"📅 {app_date} в {app_time}.\n"
                        f"Ждём вас!"
                    )
                    mark_appointment_reminded(app_id)
                    logging.info(f"Reminder sent for appointment {app_id}")
                except Exception as e:
                    logging.error(f"Failed to send reminder to {user_id}: {e}")
            await asyncio.sleep(60)  # проверка каждые 60 секунд
        except Exception as e:
            logging.error(f"Error in reminder_scheduler: {e}")
            await asyncio.sleep(60)

# ========== Запуск ==========
async def main():
    init_db()
    generate_slots(days_ahead=7)
    # Запускаем фоновую задачу напоминаний
    asyncio.create_task(reminder_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
