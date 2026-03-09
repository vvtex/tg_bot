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
API_TOKEN = os.getenv("API_TOKEN", "YOUR_BOT_TOKEN")
DATABASE = "barbershop.sqlt"

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
            status TEXT DEFAULT 'confirmed',  -- ← ИЗМЕНЕНО: теперь сразу confirmed
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

# ... (остальные функции без изменений, кроме create_appointment, где убран 'pending')
def create_appointment(user_id, service_id, date_str, time_str):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO appointments (user_id, service_id, appointment_date, appointment_time, status)
        VALUES (?,?,?,?, 'confirmed')   -- ← ИЗМЕНЕНО: теперь confirmed
    ''', (user_id, service_id, date_str, time_str))
    appointment_id = cur.lastrowid
    conn.commit()
    conn.close()
    book_slot(date_str, time_str)
    return appointment_id

# ... (остальные функции остаются как в предыдущей версии)

# ========== Клавиатуры ==========
def appointments_inline_keyboard(appointments):
    """Клавиатура для списка записей с кнопками отмены (исправлено)"""
    builder = InlineKeyboardBuilder()
    for app in appointments:
        app_id = app[0]
        status_emoji = {'pending':'🕒','confirmed':'✅','cancelled':'❌','done':'✔️'}.get(app[4], '❓')
        # Краткое описание записи
        app_text = f"{status_emoji} {app[1]} {app[2]} {app[3]}"
        
        if app[4] in ('pending', 'confirmed'):
            # Кнопка с иконкой отмены (без лишнего текста)
            builder.button(text=f"{app_text} ❌", callback_data=f"cancel_{app_id}")
        else:
            builder.button(text=f"{app_text} (нельзя отменить)", callback_data="ignore")
    builder.adjust(1)
    return builder.as_markup()

# ... (остальные клавиатуры без изменений)

# ========== Хэндлеры ==========
# ... (все хэндлеры остаются без изменений, кроме того, что в confirm_yes убрано лишнее сообщение про ожидание подтверждения)

@dp.callback_query(StateFilter(AppointmentFSM.confirming), F.data == "confirm_yes")
async def confirm_yes(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    service_id = data['service_id']
    date_str = data['date']
    time_str = data['time']
    appointment_id = create_appointment(user_id, service_id, date_str, time_str)
    await callback.message.edit_text("✅ Запись подтверждена! Мы ждём вас.")  # ← ИЗМЕНЕНО: убрано "Ожидайте подтверждения"
    await state.clear()
    await callback.message.answer(
        "Хотите получать напоминания о записи? Они будут приходить за час до визита.",
        reply_markup=notifications_inline_keyboard()
    )
    await callback.answer()

# ... (остальные хэндлеры без изменений)

# ========== Фоновые напоминания ==========
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

# ========== Запуск ==========
async def main():
    init_db()
    generate_slots(days_ahead=7)
    asyncio.create_task(reminder_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
