# bot.py
import logging
import os
import sqlite3
from contextlib import closing
from datetime import time

import pytz
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ===================== НАЛАШТУВАННЯ =====================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("Встанови змінну середовища TOKEN в Koyeb!")

KYIV_TZ = pytz.timezone("Europe/Kyiv")
DB_PATH = "/tmp/reminders.db"   # Koyeb-friendly

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ===================== БАЗА ДАНИХ =====================

def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    chat_id INTEGER PRIMARY KEY,
                    text TEXT NOT NULL,
                    days TEXT NOT NULL,      -- "0,2,4"
                    time_str TEXT NOT NULL   -- "17:45"
                )
            """)


def save_reminder(chat_id: int, text: str, days: tuple, time_str: str):
    days_str = ",".join(map(str, sorted(days)))
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            conn.execute("""
                INSERT INTO reminders (chat_id, text, days, time_str)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    text=excluded.text,
                    days=excluded.days,
                    time_str=excluded.time_str
            """, (chat_id, text, days_str, time_str))


def delete_reminder(chat_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            conn.execute("DELETE FROM reminders WHERE chat_id = ?", (chat_id,))


def get_all_reminders():
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM reminders").fetchall()
            return [
                {
                    "chat_id": row["chat_id"],
                    "text": row["text"],
                    "days": tuple(map(int, row["days"].split(","))),
                    "time": row["time_str"]
                }
                for row in rows
            ]
    except Exception as e:
        logger.error(f"DB error: {e}")
        return []


# Ініціалізуємо
init_db()


# ===================== ДНІ ТИЖНЯ =====================
DAYS_MAP = {
    "пн": 0, "понеділок": 0,
    "вт": 1, "вівторок": 1,
    "ср": 2, "середа": 2,
    "чт": 3, "четвер": 3,
    "пт": 4, "п'ятниця": 4, "пятниця": 4,
    "сб": 5, "субота": 5,
    "нд": 6, "неділя": 6,
    "щодня": (0,1,2,3,4,5,6),
    "кожен день": (0,1,2,3,4,5,6),
}


# ===================== КОМАНДИ =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! Я бот-нагадувач.\n\n"
        "Формат:\n"
        "`/set Текст | дні | 17:45`\n\n"
        "Приклади:\n"
        "• `/set Урок | пн ср пт | 17:45`\n"
        "• `/set Пити воду | щодня | 10:00`\n"
        "• `/stop` — вимкнути нагадування",
        parse_mode="Markdown"
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    delete_reminder(chat_id)

    # Видалити job-и цього чату
    for job in context.job_queue.jobs():
        if getattr(job, "data", None) == chat_id:
            job.schedule_removal()

    await update.message.reply_text("Нагадування вимкнено.")


async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text[len("/set"):].strip()

    if "|" not in text or text.count("|") != 2:
        await update.message.reply_text("Формат: `/set текст | дні | час`", parse_mode="Markdown")
        return

    msg_text, days_raw, time_raw = [p.strip() for p in text.split("|", 2)]

    # Час
    try:
        hh, mm = map(int, time_raw.split(":"))
    except:
        await update.message.reply_text("Час у форматі HH:MM, напр. 17:45")
        return

    # Дні
    days = []
    for word in days_raw.lower().replace(",", " ").split():
        if word in DAYS_MAP:
            val = DAYS_MAP[word]
            if isinstance(val, tuple):
                days = list(val)
                break
            if val not in days:
                days.append(val)

    if not days:
        await update.message.reply_text("Некоректні дні. Використовуй: пн вт ср чт пт сб нд щодня.")
        return

    days_tuple = tuple(sorted(days))

    # Запис у базу
    save_reminder(chat_id, msg_text, days_tuple, time_raw)

    # Видаляємо старі job
    for job in context.job_queue.jobs():
        if getattr(job, "data", None) == chat_id:
            job.schedule_removal()

    # Нове завдання
    job_time = time(hour=hh, minute=mm, tzinfo=KYIV_TZ)

    context.job_queue.run_daily(
        send_scheduled,
        time=job_time,
        days=days_tuple,
        data=chat_id,
        name=f"reminder_{chat_id}"
    )

    await update.message.reply_text(
        f"Нагадування встановлено!\n"
        f"• {msg_text}\n"
        f"• Дні: {days_raw}\n"
        f"• Час: {time_raw}"
    )


async def send_scheduled(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    reminders = [r for r in get_all_reminders() if r["chat_id"] == chat_id]
    if not reminders:
        return

    text = reminders[0]["text"]

    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"Помилка надсилання в {chat_id}: {e}")


# ===================== ВІДНОВЛЕННЯ Нагадувань =====================

async def post_init(application: Application):
    # На всяк випадок очищаємо webhook (щоб не було Conflict 409)
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    logger.info("Відновлюю нагадування...")

    for r in get_all_reminders():
        chat_id = r["chat_id"]
        hh, mm = map(int, r["time"].split(":"))
        job_time = time(hour=hh, minute=mm, tzinfo=KYIV_TZ)

        application.job_queue.run_daily(
            send_scheduled,
            time=job_time,
            days=r["days"],
            data=chat_id,
            name=f"reminder_{chat_id}"
        )

    logger.info("Нагадування відновлено.")


# ===================== ЗАПУСК =====================

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set", set_reminder))
    app.add_handler(CommandHandler("stop", stop))

    logger.info("Бот запущено – працює в усіх чатах")
    app.run_polling(drop_pending_updates=True)



if __name__ == "__main__":
    main()
