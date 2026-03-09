import asyncio
import logging
import smtplib
import os
import sqlite3
from datetime import datetime, timedelta, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, KeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

# ========== Конфигурация ==========
API_TOKEN = os.getenv("API_TOKEN")
if not API_TOKEN:
    raise ValueError("❌ Переменная окружения API_TOKEN не задана!")

EMAIL = os.getenv("EMAIL")  # может быть пустым
TG_ADMIN = os.getenv("TG_ADMIN")  # может быть пустым
if TG_ADMIN:
    try:
        TG_ADMIN = int(TG_ADMIN)
    except ValueError:
        logging.warning("TG_ADMIN должен быть числом (ID). Уведомления в Telegram отключены.")
        TG_ADMIN = None

DATABASE = "barbershop.sqlt"

# Настройки SMTP (опционально)
SMTP_SERVER = os.getenv("SMTP_SERVER", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", 25))
SMTP_LOGIN = os.getenv("SMTP_LOGIN")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

logging.basicConfig(level=logging.INFO)

# ========== Инициализация БД ==========
def init_db():
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            phone TEXT,
            notifications INTEGER DEFAULT 1,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            duration INTEGER DEFAULT 60,
            price INTEGER
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            service_id INTEGER,
            appointment_date DATE,
            appointment_time TIME,
            status TEXT DEFAULT 'confirmed',
            reminded INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(service_id) REFERENCES services(id)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_date DATE,
            slot_time TIME,
            is_available INTEGER DEFAULT 1
        )
    ''')
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

# ---------- Пользователи ----------
def register_user(user_id, username, full_name):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?,?,?)",
                (user_id, username, full_name))
    conn.commit()
    conn.close()

def update_user_contact(user_id, full_name, phone):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("UPDATE users SET full_name=?, phone=? WHERE user_id=?",
                (full_name, phone, user_id))
    conn.commit()
    conn.close()

def get_user_name(user_id):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT full_name, username FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        return row[0]
    elif row and row[1]:
        return f"@{row[1]}"
    return str(user_id)

def get_user_phone(user_id):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT phone FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def set_user_notifications(user_id, enabled: bool):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("UPDATE users SET notifications=? WHERE user_id=?", (1 if enabled else 0, user_id))
    conn.commit()
    conn.close()

def get_user_notifications(user_id):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT notifications FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 1

# ---------- Услуги ----------
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

# ---------- Слоты ----------
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

# ---------- Записи ----------
def create_appointment(user_id, service_id, date_str, time_str):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO appointments (user_id, service_id, appointment_date, appointment_time, status)
        VALUES (?,?,?,?, 'confirmed')
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

def get_appointment_by_id(appointment_id, user_id):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute('''
        SELECT id, user_id, service_id, appointment_date, appointment_time, status
        FROM appointments WHERE id=? AND user_id=?
    ''', (appointment_id, user_id))
    row = cur.fetchone()
    conn.close()
    return row

def get_appointment_details(appointment_id):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute('''
        SELECT a.id, u.full_name, u.phone, u.username, s.name, a.appointment_date, a.appointment_time, a.status
        FROM appointments a
        JOIN users u ON a.user_id = u.user_id
        JOIN services s ON a.service_id = s.id
        WHERE a.id=?
    ''', (appointment_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {
            'id': row[0],
            'client_name': row[1] or f"@{row[3]}" or str(row[0]),
            'client_phone': row[2] or 'не указан',
            'service': row[4],
            'date': row[5],
            'time': row[6],
            'status': row[7]
        }
    return None

def cancel_appointment(appointment_id):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT appointment_date, appointment_time FROM appointments WHERE id=?", (appointment_id,))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (appointment_id,))
        release_slot(row[0], row[1])
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

async def delete_expired_appointments():
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    now = datetime.now()
    expired = []
    cur.execute('''
        SELECT id, user_id, appointment_date, appointment_time, status
        FROM appointments
        WHERE status != 'cancelled' AND status != 'deleted'
    ''')
    for row in cur.fetchall():
        app_id, user_id, d_str, t_str, status = row
        app_dt = datetime.strptime(f"{d_str} {t_str}", "%Y-%m-%d %H:%M")
        if app_dt + timedelta(minutes=1) < now:
            expired.append((app_id, user_id, d_str, t_str))
    
    for app_id, user_id, d_str, t_str in expired:
        details = get_appointment_details(app_id)
        cur.execute("DELETE FROM appointments WHERE id=?", (app_id,))
        release_slot(d_str, t_str)
        conn.commit()
        if details:
            await send_admin_notification(
                f"❌ Запись автоматически удалена (время истекло)\n"
                f"Клиент: {details['client_name']}, тел: {details['client_phone']}\n"
                f"Услуга: {details['service']}\n"
                f"Дата: {details['date']} {details['time']}"
            )
    conn.close()

# ---------- Уведомления администратору ----------
async def send_email(to_addr, subject, body):
    if not to_addr:
        return
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_LOGIN or 'bot@localhost'
        msg['To'] = to_addr
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        loop = asyncio.get_event_loop()
        def send():
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            if SMTP_LOGIN and SMTP_PASSWORD:
                server.starttls()
                server.login(SMTP_LOGIN, SMTP_PASSWORD)
            server.send_message(msg)
            server.quit()
        await loop.run_in_executor(None, send)
        logging.info(f"Email sent to {to_addr}")
    except Exception as e:
        logging.error(f"Failed to send email: {e}")

async def send_telegram(chat_id, text):
    if not chat_id:
        return
    try:
        await bot.send_message(chat_id, text)
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")

async def send_admin_notification(text):
    if EMAIL:
        await send_email(EMAIL, "Уведомление от парикмахерской", text)
    if TG_ADMIN:
        await send_telegram(TG_ADMIN, text)

# ---------- Для напоминаний ----------
def get_upcoming_appointments_for_reminder():
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
        JOIN users u ON a.user_id = u.user_id
        WHERE a.status='confirmed' 
          AND a.reminded=0
          AND u.notifications=1
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

# ========== FSM состояния ==========
class AppointmentFSM(StatesGroup):
    choosing_service = State()
    choosing_date = State()
    choosing_time = State()
    asking_name = State()
    asking_phone = State()
    confirming = State()

class CancelFSM(StatesGroup):
    waiting_confirm = State()

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

def notifications_inline_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да", callback_data="notif_yes")
    builder.button(text="❌ Нет", callback_data="notif_no")
    return builder.as_markup()

def appointments_inline_keyboard(appointments):
    builder = InlineKeyboardBuilder()
    for app in appointments:
        app_id = app[0]
        status_emoji = {'pending':'🕒','confirmed':'✅','cancelled':'❌','done':'✔️'}.get(app[4], '❓')
        app_text = f"{status_emoji} {app[1]} {app[2]} {app[3]}"
        if app[4] in ('pending', 'confirmed'):
            builder.button(text=f"{app_text} ❌", callback_data=f"cancel_{app_id}")
        else:
            builder.button(text=f"{app_text} (нельзя отменить)", callback_data="ignore")
    builder.adjust(1)
    return builder.as_markup()

def confirm_cancel_inline_keyboard(appointment_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, отменить", callback_data=f"confirm_cancel_{appointment_id}")
    builder.button(text="❌ Нет", callback_data="cancel_cancel")
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
    # Переходим к запросу имени
    await state.set_state(AppointmentFSM.asking_name)
    await callback.message.edit_text("Введите ваше имя (как к вам обращаться):")
    await callback.message.answer("Пожалуйста, введите имя:", reply_markup=cancel_keyboard())
    await callback.answer()

@dp.message(StateFilter(AppointmentFSM.asking_name))
async def ask_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Имя не может быть пустым. Введите имя:")
        return
    await state.update_data(client_name=name)
    await state.set_state(AppointmentFSM.asking_phone)
    await message.answer("Введите ваш номер телефона для связи (например, +79991234567):", reply_markup=cancel_keyboard())

@dp.message(StateFilter(AppointmentFSM.asking_phone))
async def ask_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not phone:
        await message.answer("Телефон не может быть пустым. Введите номер:")
        return
    # Простейшая валидация (можно улучшить)
    if len(phone) < 5:
        await message.answer("Слишком короткий номер. Введите корректный номер:")
        return
    await state.update_data(client_phone=phone)
    # Переходим к подтверждению
    data = await state.get_data()
    service = get_service(data['service_id'])
    summary = (f"📌 Пожалуйста, проверьте данные:\n"
               f"Имя: {data['client_name']}\n"
               f"Телефон: {data['client_phone']}\n"
               f"Услуга: {service[1]}\n"
               f"Цена: {service[3]} руб.\n"
               f"Дата: {data['date']}\n"
               f"Время: {data['time']}\n\n"
               f"Всё верно?")
    await state.set_state(AppointmentFSM.confirming)
    await message.answer(summary, reply_markup=confirm_inline_keyboard())

@dp.callback_query(StateFilter(AppointmentFSM.confirming), F.data.in_(['confirm_yes']))
async def confirm_yes(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    service_id = data['service_id']
    date_str = data['date']
    time_str = data['time']
    client_name = data.get('client_name')
    client_phone = data.get('client_phone')
    
    # Обновляем контактные данные пользователя в БД
    if client_name and client_phone:
        update_user_contact(user_id, client_name, client_phone)
    
    appointment_id = create_appointment(user_id, service_id, date_str, time_str)
    
    service = get_service(service_id)
    await send_admin_notification(
        f"✅ Новая запись\n"
        f"Клиент: {client_name}\n"
        f"Телефон: {client_phone}\n"
        f"Услуга: {service[1]}\n"
        f"Дата: {date_str}\n"
        f"Время: {time_str}"
    )
    
    await callback.message.edit_text("✅ Запись подтверждена! Мы ждём вас.")
    await state.clear()
    await callback.message.answer(
        "Хотите получать напоминания о записи? Они будут приходить за час до визита.",
        reply_markup=notifications_inline_keyboard()
    )
    await callback.answer()

@dp.callback_query(StateFilter(AppointmentFSM.confirming), F.data.in_(['confirm_no']))
async def confirm_no(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Запись отменена. Можете начать заново.")
    await state.clear()
    await callback.answer()

# ---------- Настройка уведомлений ----------
@dp.callback_query(F.data.in_(['notif_yes', 'notif_no']))
async def set_notifications(callback: CallbackQuery):
    user_id = callback.from_user.id
    enabled = (callback.data == "notif_yes")
    set_user_notifications(user_id, enabled)
    if enabled:
        await callback.message.edit_text("✅ Вы будете получать напоминания о записях.")
    else:
        await callback.message.edit_text("❌ Напоминания отключены.")
    await callback.answer()

# ---------- Просмотр и отмена записей ----------
@dp.message(F.text == "📋 Мои записи")
async def my_appointments(message: Message):
    user_id = message.from_user.id
    apps = get_user_appointments(user_id)
    if not apps:
        await message.answer("У вас пока нет записей.")
        return
    await message.answer("Ваши записи:", reply_markup=appointments_inline_keyboard(apps))

@dp.callback_query(F.data.startswith("cancel_"))
async def start_cancel_appointment(callback: CallbackQuery, state: FSMContext):
    app_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    app = get_appointment_by_id(app_id, user_id)
    if not app:
        await callback.message.edit_text("Запись не найдена или уже удалена.")
        await callback.answer()
        return
    if app[5] not in ('pending', 'confirmed'):
        await callback.message.edit_text("Эту запись уже нельзя отменить (она выполнена или отменена).")
        await callback.answer()
        return
    await state.update_data(cancel_app_id=app_id)
    await state.set_state(CancelFSM.waiting_confirm)
    await callback.message.edit_text(
        f"❓ Вы уверены, что хотите отменить запись на {app[3]} в {app[4]}?",
        reply_markup=confirm_cancel_inline_keyboard(app_id)
    )
    await callback.answer()

@dp.callback_query(StateFilter(CancelFSM.waiting_confirm), F.data.startswith("confirm_cancel_"))
async def confirm_cancel(callback: CallbackQuery, state: FSMContext):
    app_id = int(callback.data.split("_")[2])
    data = await state.get_data()
    if data.get("cancel_app_id") != app_id:
        await callback.message.edit_text("Ошибка: данные устарели. Попробуйте снова.")
        await state.clear()
        await callback.answer()
        return
    
    details = get_appointment_details(app_id)
    if cancel_appointment(app_id):
        await callback.message.edit_text("✅ Запись успешно отменена.")
        if details:
            await send_admin_notification(
                f"❌ Запись отменена клиентом\n"
                f"Клиент: {details['client_name']}, тел: {details['client_phone']}\n"
                f"Услуга: {details['service']}\n"
                f"Дата: {details['date']} {details['time']}"
            )
    else:
        await callback.message.edit_text("❌ Не удалось отменить запись.")
    await state.clear()
    await callback.answer()

@dp.callback_query(StateFilter(CancelFSM.waiting_confirm), F.data.in_(['cancel_cancel']))
async def abort_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Отмена отмены (действие не выполнено).")
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data.in_(['ignore']))
async def ignore_callback(callback: CallbackQuery):
    await callback.answer("Это действие недоступно.", show_alert=True)

# ---------- Информационные разделы ----------
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

# ========== Фоновые задачи ==========
async def reminder_scheduler():
    while True:
        try:
            appointments = get_upcoming_appointments_for_reminder()
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

async def cleaner_scheduler():
    while True:
        try:
            await delete_expired_appointments()
            await asyncio.sleep(60)
        except Exception as e:
            logging.error(f"Cleaner error: {e}")
            await asyncio.sleep(60)

# ========== Запуск ==========
async def main():
    init_db()
    generate_slots(days_ahead=7)
    asyncio.create_task(reminder_scheduler())
    asyncio.create_task(cleaner_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
