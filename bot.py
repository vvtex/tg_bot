import asyncio
import logging
from datetime import datetime, timedelta, date
import os
import sqlite3

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, KeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

# ========== Конфигурация ==========
API_TOKEN = os.getenv("API_TOKEN", "YOUR_BOT_TOKEN")  # токен бота (можно вписать напрямую)
DATABASE = "barbershop.sqlt"

logging.basicConfig(level=logging.INFO)

# ========== Инициализация БД ==========
def init_db():
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    # Пользователи (роль больше не нужна, но оставим для совместимости)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            phone TEXT,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Услуги
    cur.execute('''
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            duration INTEGER DEFAULT 60,
            price INTEGER
        )
    ''')
    # Записи (статус теперь только для пользователя: pending, confirmed, cancelled, done)
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
    # Слоты
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

# ---------- Работа с пользователями ----------
def register_user(user_id, username, full_name):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?,?,?)",
                (user_id, username, full_name))
    conn.commit()
    conn.close()

# ---------- Работа с услугами ----------
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

# ---------- Работа со слотами ----------
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

# ---------- Работа с записями ----------
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

def cancel_appointment(appointment_id, user_id):
    """Отмена записи (только если принадлежит пользователю)"""
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    # Проверим, что запись принадлежит этому пользователю и не отменена/не выполнена
    cur.execute("SELECT user_id, appointment_date, appointment_time, status FROM appointments WHERE id=?", (appointment_id,))
    row = cur.fetchone()
    if not row or row[0] != user_id or row[3] in ('cancelled', 'done'):
        conn.close()
        return False
    cur.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (appointment_id,))
    release_slot(row[1], row[2])
    conn.commit()
    conn.close()
    return True

def get_upcoming_confirmed_appointments():
    """Для напоминаний (только подтверждённые)"""
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    now = datetime.now()
    target_time = now + timedelta(minutes=60)
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

# ========== FSM для записи ==========
class AppointmentFSM(StatesGroup):
    choosing_service = State()
    choosing_date = State()
    choosing_time = State()
    confirming = State()

# ========== Клавиатуры ==========
def main_menu_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📅 Записаться"))
    builder.add(KeyboardButton(text="📋 Мои записи"))
    builder.add(KeyboardButton(text="💇 Услуги и цены"))
    builder.add(KeyboardButton(text="📍 Контакты"))
    builder.add(KeyboardButton(text="🔥 Акции"))
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

def appointments_inline_keyboard(appointments):
    """Клавиатура для списка записей с кнопками отмены"""
    builder = InlineKeyboardBuilder()
    for app in appointments:
        app_id = app[0]
        status_emoji = {'pending':'🕒','confirmed':'✅','cancelled':'❌','done':'✔️'}.get(app[4], '❓')
        # Если статус позволяет отмену (pending или confirmed), добавляем кнопку
        if app[4] in ('pending', 'confirmed'):
            builder.button(text=f"{status_emoji} {app[1]} {app[2]} {app[3]} ❌ Отменить",
                           callback_data=f"cancel_{app_id}")
        else:
            builder.button(text=f"{status_emoji} {app[1]} {app[2]} {app[3]} (отменить нельзя)",
                           callback_data="ignore")
    builder.adjust(1)
    return builder.as_markup()

# ========== Хэндлеры ==========
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    register_user(user.id, user.username, user.full_name)
    welcome_text = (
        f"👋 Добро пожаловать в парикмахерскую 'Стиль', {user.full_name}!\n\n"
        "Мы предлагаем профессиональные услуги по стрижке и укладке.\n"
        "Запишитесь сейчас и получите скидку 10% на первое посещение! 🎁"
    )
    await message.answer(welcome_text, reply_markup=main_menu_keyboard())

@dp.message(F.text == "📅 Записаться")
async def book_appointment(message: Message, state: FSMContext):
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
    await callback.message.answer("Хотите получать напоминания о записи? Они будут приходить за час до визита.")
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
    # Отправляем список с инлайн-кнопками для отмены
    await message.answer("Ваши записи:", reply_markup=appointments_inline_keyboard(apps))

@dp.callback_query(F.data.startswith("cancel_"))
async def cancel_my_appointment(callback: CallbackQuery):
    app_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    if cancel_appointment(app_id, user_id):
        await callback.message.edit_text("✅ Запись успешно отменена.")
    else:
        await callback.message.edit_text("❌ Не удалось отменить запись (возможно, она уже отменена или выполнена).")
    await callback.answer()

@dp.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQuery):
    await callback.answer("Это действие недоступно.", show_alert=True)

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

@dp.message(F.text == "❌ Отмена")
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())

@dp.message()
async def unknown_message(message: Message):
    await message.answer("Извините, я не понимаю. Используйте кнопки меню.")

# ========== Фоновые напоминания ==========
async def reminder_scheduler():
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
                except Exception as e:
                    logging.error(f"Reminder error: {e}")
            await asyncio.sleep(60)
        except Exception as e:
            logging.error(f"Scheduler error: {e}")
            await asyncio.sleep(60)

# ========== Запуск ==========
async def main():
    init_db()
    generate_slots(days_ahead=7)
    asyncio.create_task(reminder_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
